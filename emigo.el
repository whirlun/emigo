;;; emigo.el --- Emigo  -*- lexical-binding: t -*-

;; Filename: emigo.el
;; Description: Emigo
;; Author: Andy Stewart <lazycat.manatee@gmail.com>
;; Maintainer: Andy Stewart <lazycat.manatee@gmail.com>
;; Copyright (C) 2018, Andy Stewart, all rights reserved.
;; Created: 2018-06-15 14:10:12
;; Version: 0.5
;; Last-Updated: 2023-08-12 21:08:48
;;           By: Andy Stewart
;; URL: https://github.com/manateelazycat/emigo
;; Keywords:
;; Compatibility: emacs-version >= 28
;; Package-Requires: ((emacs "28") (posframe "1.1.7") (markdown-mode "2.6"))
;;
;; Features that might be required by this library:
;;
;; Please check README
;;

;;; This file is NOT part of GNU Emacs

;;; License
;;
;; This program is free software; you can redistribute it and/or modify
;; it under the terms of the GNU General Public License as published by
;; the Free Software Foundation; either version 3, or (at your option)
;; any later version.

;; This program is distributed in the hope that it will be useful,
;; but WITHOUT ANY WARRANTY; without even the implied warranty of
;; MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
;; GNU General Public License for more details.

;; You should have received a copy of the GNU General Public License
;; along with this program; see the file COPYING.  If not, write to
;; the Free Software Foundation, Inc., 51 Franklin Street, Fifth
;; Floor, Boston, MA 02110-1301, USA.

;;; Commentary:
;;
;; Emigo
;;

;;; Installation:
;;
;; Please check README
;;

;;; Customize:
;;
;;
;;
;; All of the above can customize by:
;;      M-x customize-group RET emigo RET
;;

;;; Change log:
;;
;;

;;; Acknowledgements:
;;
;;
;;

;;; TODO
;;
;;
;;

;;; Code:
(require 'cl-lib)
(require 'json)
(require 'map)
(require 'seq)
(require 'subr-x)
(require 'emigo-epc)

(defgroup emigo nil
  "Emigo group."
  :group 'applications)

(defcustom emigo-get-project-path-by-filepath nil
  "Default use command 'git rev-parse --show-toplevel' get project path,
you can customize `emigo-get-project-path-by-filepath' to return project path by give file path.")

(defcustom emigo-model ""
  "Default AI model.")

(defcustom emigo-base-url ""
  "Base URL for AI model.")

(defcustom emigo-api-key ""
  "API key for AI model.")

(defvar emigo-server nil
  "The Emigo Server.")

(defvar emigo-python-file (expand-file-name "emigo.py" (if load-file-name
                                                           (file-name-directory load-file-name)
                                                         default-directory)))

(defvar emigo-server-port nil)

(defun emigo--start-epc-server ()
  "Function to start the EPC server."
  (unless (process-live-p emigo-server)
    (setq emigo-server
          (emigo-epc-server-start
           (lambda (mngr)
             (let ((mngr mngr))
               (emigo-epc-define-method mngr 'eval-in-emacs 'emigo--eval-in-emacs-func)
               (emigo-epc-define-method mngr 'get-emacs-var 'emigo--get-emacs-var-func)
               (emigo-epc-define-method mngr 'get-emacs-vars 'emigo--get-emacs-vars-func)
               (emigo-epc-define-method mngr 'get-user-emacs-directory 'emigo--user-emacs-directory)
               (emigo-epc-define-method mngr 'get-project-path 'emigo--get-project-path-func)
               ))))
    (if emigo-server
        (setq emigo-server-port (process-contact emigo-server :service))
      (error "[Emigo] emigo-server failed to start")))
  emigo-server)

(defun emigo--get-project-path-func (filename)
  "Get project root path, search order:

1. Follow the rule of `emigo-get-project-path-by-filepath'
2. Search up `.dir-locals.el'
3. Search up `.git'"
  (if emigo-get-project-path-by-filepath
      ;; Fetch project root path by `emigo-get-project-path-by-filepath' if it set by user.
      (funcall emigo-get-project-path-by-filepath filename)
    ;; Otherwise try to search up `.dir-locals.el' file
    (let* ((result (dir-locals-find-file filename))
           (dir (if (consp result) (car result) result)))
      (when dir (directory-file-name dir)))))

(defun emigo--eval-in-emacs-func (sexp-string)
  (eval (read sexp-string))
  ;; Return nil to avoid epc error `Got too many arguments in the reply'.
  nil)

(defun emigo--get-emacs-var-func (var-name)
  (let* ((var-symbol (intern var-name))
         (var-value (symbol-value var-symbol))
         ;; We need convert result of booleanp to string.
         ;; Otherwise, python-epc will convert all `nil' to [] at Python side.
         (var-is-bool (prin1-to-string (booleanp var-value))))
    (list var-value var-is-bool)))

(defun emigo--get-emacs-vars-func (&rest vars)
  (mapcar #'emigo--get-emacs-var-func vars))

(defvar emigo-project-buffers nil)

(defvar emigo-epc-process nil)

(defvar emigo-internal-process nil)
(defvar emigo-internal-process-prog nil)
(defvar emigo-internal-process-args nil)

(defcustom emigo-name "*emigo*"
  "Name of Emigo buffer."
  :type 'string)

(defcustom emigo-python-command (if (memq system-type '(cygwin windows-nt ms-dos)) "python.exe" "python3")
  "The Python interpreter used to run emigo.py."
  :type 'string)

(defcustom emigo-enable-debug nil
  "If you got segfault error, please turn this option.
Then Emigo will start by gdb, please send new issue with `*emigo*' buffer content when next crash."
  :type 'boolean)

(defcustom emigo-enable-log nil
  "Enable this option to print log message in `*emigo*' buffer, default only print message header."
  :type 'boolean)

(defcustom emigo-enable-profile nil
  "Enable this option to output performance data to ~/emigo.prof."
  :type 'boolean)

(defun emigo--user-emacs-directory ()
  "Get lang server with project path, file path or file extension."
  (expand-file-name user-emacs-directory))

(defun emigo-call-async (method &rest args)
  "Call Python EPC function METHOD and ARGS asynchronously."
  (if (emigo-epc-live-p emigo-epc-process)
      (emigo-deferred-chain
        (emigo-epc-call-deferred emigo-epc-process (read method) args))
    (setq emigo-first-call-method method)
    (setq emigo-first-call-args args)
    ))

(defun emigo-call--sync (method &rest args)
  "Call Python EPC function METHOD and ARGS synchronously."
  (if (emigo-epc-live-p emigo-epc-process)
      (emigo-epc-call-sync emigo-epc-process (read method) args)
    (message "emigo not started.")
    ))

(defvar emigo-first-call-method nil)
(defvar emigo-first-call-args nil)

(defun emigo-restart-process ()
  "Stop and restart Emigo process."
  (interactive)
  (emigo-kill-process)
  (emigo-start-process)
  (message "[Emigo] Process restarted."))

(defun emigo-start-process ()
  "Start Emigo process if it isn't started."
  (if (emigo-epc-live-p emigo-epc-process)
      (remove-hook 'post-command-hook #'emigo-start-process)
    ;; start epc server and set `emigo-server-port'
    (emigo--start-epc-server)
    (let* ((emigo-args (append
                        (list emigo-python-file)
                        (list (number-to-string emigo-server-port))
                        (when emigo-enable-profile
                          (list "profile"))
                        )))

      ;; Set process arguments.
      (if emigo-enable-debug
          (progn
            (setq emigo-internal-process-prog "gdb")
            (setq emigo-internal-process-args (append (list "-batch" "-ex" "run" "-ex" "bt" "--args" emigo-python-command) emigo-args)))
        (setq emigo-internal-process-prog emigo-python-command)
        (setq emigo-internal-process-args emigo-args))

      ;; Start python process.
      (let ((process-connection-type t))
        (setq emigo-internal-process
              (apply 'start-process
                     emigo-name emigo-name
                     emigo-internal-process-prog emigo-internal-process-args)))
      (set-process-query-on-exit-flag emigo-internal-process nil))))

(defvar emigo-stop-process-hook nil)

(defun emigo-kill-process ()
  "Stop Emigo process and kill all Emigo buffers."
  (interactive)
  ;; Kill project buffers.
  (save-excursion
    (cl-dolist (buffer emigo-project-buffers)
      (when (and buffer (buffer-live-p buffer))
        (kill-buffer buffer))))
  (setq emigo-project-buffers nil)

  ;; Close dedicated window.
  (emigo-dedicated-close)

  ;; Run stop process hooks.
  (run-hooks 'emigo-stop-process-hook)

  ;; Kill process after kill buffer, make application can save session data.
  (emigo--kill-python-process)
  )

(add-hook 'kill-emacs-hook #'emigo-kill-process)

(defun emigo--kill-python-process ()
  "Kill Emigo background python process."
  (when (emigo-epc-live-p emigo-epc-process)
    ;; Cleanup before exit Emigo server process.
    (emigo-call-async "cleanup")
    ;; Delete Emigo server process.
    (emigo-epc-stop-epc emigo-epc-process)
    ;; Kill *emigo* buffer.
    (when (get-buffer emigo-name)
      (kill-buffer emigo-name))
    (setq emigo-epc-process nil)
    (message "[Emigo] Process terminated.")))

(defun emigo--first-start (emigo-epc-port)
  "Call `emigo--open-internal' upon receiving `start_finish' signal from server."
  ;; Make EPC process.
  (setq emigo-epc-process (make-emigo-epc-manager
                           :server-process emigo-internal-process
                           :commands (cons emigo-internal-process-prog emigo-internal-process-args)
                           :title (mapconcat 'identity (cons emigo-internal-process-prog emigo-internal-process-args) " ")
                           :port emigo-epc-port
                           :connection (emigo-epc-connect "127.0.0.1" emigo-epc-port)
                           ))
  (emigo-epc-init-epc-layer emigo-epc-process)

  (when (and emigo-first-call-method
             emigo-first-call-args)
    (emigo-deferred-chain
      (emigo-epc-call-deferred emigo-epc-process
                               (read emigo-first-call-method)
                               emigo-first-call-args)
      (setq emigo-first-call-method nil)
      (setq emigo-first-call-args nil)
      )))

(defun emigo-enable ()
  (add-hook 'post-command-hook #'emigo-start-process))

(defun emigo-read-file-content (filepath)
  (with-temp-buffer
    (insert-file-contents filepath)
    (string-trim (buffer-string))))

(defun emigo (prompt)
  (interactive "sEmigo: ")
  (emigo-call-async "emigo" (buffer-file-name) prompt))

(defcustom emigo-dedicated-window-width 50
  "The height of `emigo' dedicated window."
  :type 'integer
  :group 'emigo)

(defvar emigo-dedicated-window nil
  "The dedicated `emigo' window.")

(defvar emigo-dedicated-buffer nil
  "The dedicated `emigo' buffer.")

(defun emigo-current-window-take-height (&optional window)
  "Return the height the `window' takes up.
Not the value of `window-width', it returns usable rows available for WINDOW.
If `window' is nil, get current window."
  (let ((edges (window-edges window)))
    (- (nth 3 edges) (nth 1 edges))))

(defun emigo-dedicated-exist-p ()
  (and (emigo-buffer-exist-p emigo-dedicated-buffer)
       (emigo-window-exist-p emigo-dedicated-window)
       ))

(defun emigo-window-exist-p (window)
  "Return `non-nil' if WINDOW exist.
Otherwise return nil."
  (and window (window-live-p window)))

(defun emigo-buffer-exist-p (buffer)
  "Return `non-nil' if `BUFFER' exist.
Otherwise return nil."
  (and buffer (buffer-live-p buffer)))

(defun emigo-dedicated-close ()
  "Close dedicated `emigo' window."
  (interactive)
  (if (emigo-dedicated-exist-p)
      (let ((current-window (selected-window)))
        ;; Remember height.
        (emigo-dedicated-select-window)
        (delete-window emigo-dedicated-window)
        (if (emigo-window-exist-p current-window)
            (select-window current-window)))
    (message "`EMIGO DEDICATED' window is not exist.")))

(defun emigo-dedicated-toggle ()
  "Toggle dedicated `emigo' window."
  (interactive)
  (if (emigo-dedicated-exist-p)
      (emigo-dedicated-close)
    (emigo-dedicated-open)))

(defun emigo-dedicated-open ()
  "Open dedicated `emigo' window."
  (interactive)
  (if (emigo-window-exist-p emigo-dedicated-window)
      (emigo-dedicated-select-window)
    (emigo-dedicated-pop-window)))

(defun emigo-dedicated-pop-window ()
  "Pop emigo dedicated window if it exists."
  (setq emigo-dedicated-window (display-buffer (current-buffer) `(display-buffer-in-side-window (side . right) (window-width . ,emigo-dedicated-window-width))))
  (select-window emigo-dedicated-window)
  (set-window-buffer emigo-dedicated-window emigo-dedicated-buffer)
  (set-window-dedicated-p (selected-window) t))

(defun emigo-dedicated-select-window ()
  "Select emigo dedicated window."
  (select-window emigo-dedicated-window)
  (set-window-dedicated-p (selected-window) t))

(defun emigo-get-ai-buffer (project-path)
  (let ((buffer (get-buffer-create (format " *emigo %s*" project-path))))
    (add-to-list 'emigo-project-buffers buffer)
    buffer))

(defun emigo-create-ai-window (project-path)
  (save-excursion
    (let ((ai-buffer (emigo-get-ai-buffer project-path)))
      (setq emigo-dedicated-buffer ai-buffer)
      (unless (emigo-window-exist-p emigo-dedicated-window)
        (setq emigo-dedicated-window
              (display-buffer (current-buffer)
                              `(display-buffer-in-side-window
                                (side . right)
                                (window-width . ,emigo-dedicated-window-width)))))
      (select-window emigo-dedicated-window)
      (set-window-buffer emigo-dedicated-window emigo-dedicated-buffer)
      (set-window-dedicated-p (selected-window) t))))

(defun emigo-flush-ai-buffer (project-path content)
  (save-excursion
    (let ((ai-buffer (emigo-get-ai-buffer project-path)))
      (with-current-buffer ai-buffer
        (goto-char (point-max))
        (insert content)))))

(defun emigo-dedicated-split-window ()
  "Split dedicated window at bottom of frame."
  ;; Select bottom window of frame.
  (ignore-errors
    (dotimes (i 50)
      (windmove-right)))
  ;; Split with dedicated window height.
  (split-window (selected-window) (- (emigo-current-window-take-height) emigo-dedicated-window-width) t)
  (other-window 1)
  (setq emigo-dedicated-window (selected-window)))

(defadvice delete-other-windows (around emigo-delete-other-window-advice activate)
  "This is advice to make `emigo' avoid dedicated window deleted.
Dedicated window can't deleted by command `delete-other-windows'."
  (unless (eq (selected-window) emigo-dedicated-window)
    (let ((emigo-dedicated-active-p (emigo-window-exist-p emigo-dedicated-window)))
      (if emigo-dedicated-active-p
          (let ((current-window (selected-window)))
            (cl-dolist (win (window-list))
              (when (and (window-live-p win)
                         (not (eq current-window win))
                         (not (window-dedicated-p win)))
                (delete-window win))))
        ad-do-it))))

(defadvice other-window (after emigo-dedicated-other-window-advice)
  "Default, can use `other-window' select window in cyclic ordering of windows.
But sometimes we don't want to select `sr-speedbar' window,
but use `other-window' and just make `emigo' dedicated
window as a viewable sidebar.

This advice can make `other-window' skip `emigo' dedicated window."
  (let ((count (or (ad-get-arg 0) 1)))
    (when (and (emigo-window-exist-p emigo-dedicated-window)
               (eq emigo-dedicated-window (selected-window)))
      (other-window count))))

(provide 'emigo)

;;; emigo.el ends here
