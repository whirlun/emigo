;;; emigo.el --- Emigo  -*- lexical-binding: t -*-

;; Filename: emigo.el
;; Description: Emigo
;; Authors: Mingde (Matthew) Zeng <matthewzmd@posteo.net>
;;          Andy Stewart <lazycat.manatee@gmail.com>
;; Maintainer: Mingde (Matthew) Zeng <matthewzmd@posteo.net>
;;             Andy Stewart <lazycat.manatee@gmail.com>
;; Copyright (C) 2025, Emigo, all rights reserved.
;; Created: 2025-03-29
;; Version: 0.5
;; Last-Updated: Sun Mar 30 23:29:03 2025 (-0400)
;;           By: Mingde (Matthew) Zeng
;; Package-Requires: ((emacs "26.1") (transient "0.3.0") (compat "30.0.2.0"))
;; Keywords: ai emacs llm aider ai-pair-programming tools
;; URL: https://github.com/MatthewZMD/emigo
;; SPDX-License-Identifier: Apache-2.0
;;

;;; This file is NOT part of GNU Emacs

;;; Commentary:

;; Emigo

;;; Code:
(require 'cl-lib)
(require 'json)
(require 'map)
(require 'seq)
(require 'subr-x)
(require 'vc-git)
(require 'emigo-epc)

(defgroup emigo nil
  "Emigo group."
  :group 'emigo)

(defcustom emigo-mode-hook '()
  "emigo mode hook."
  :type 'hook
  :group 'emigo)

(defcustom emigo-model ""
  "Default AI model.")

(defcustom emigo-base-url ""
  "Base URL for AI model.")

(defcustom emigo-api-key ""
  "API key for AI model.")

(defcustom emigo-config-location (expand-file-name (locate-user-emacs-file "emigo/"))
  "Directory where emigo will store configuration files."
  :type 'directory)

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
             (emigo-epc-define-method mngr 'eval-in-emacs 'emigo--eval-in-emacs-func)
             (emigo-epc-define-method mngr 'get-emacs-var 'emigo--get-emacs-var-func)
             (emigo-epc-define-method mngr 'get-emacs-vars 'emigo--get-emacs-vars-func)
             (emigo-epc-define-method mngr 'get-user-emacs-directory 'emigo--user-emacs-directory))))
    (if emigo-server
        (setq emigo-server-port (process-contact emigo-server :service))
      (error "[Emigo] emigo-server failed to start")))
  emigo-server)

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

(defun emigo--is-emigo-buffer-p (&optional buffer)
  "Return non-nil if BUFFER (defaults to current) is an Emigo buffer."
  (with-current-buffer (or buffer (current-buffer))
    (string-match-p "^\\*emigo:.*\\*$" (buffer-name))))

(defun emigo-project-root ()
  "Get the project root using VC-git, or fallback to file directory.
This function tries multiple methods to determine the project root."
  (or (vc-git-root default-directory)
      (when buffer-file-name
        (file-name-directory buffer-file-name))
      default-directory))

(defun emigo-select-buffer-name ()
  "Select an existing emigo session buffer.
If there is only one emigo buffer, return its name.
If there are multiple, prompt to select one interactively.
Returns nil if no emigo buffers exist.
This is used when you want to target an existing session."
  (let* ((buffers (seq-filter #'emigo--is-emigo-buffer-p (buffer-list)))
         (buffer-names (mapcar #'buffer-name buffers)))
    (pcase buffers
      (`() nil)
      (`(,name) (buffer-name name))
      (_ (completing-read "Select emigo session: " buffer-names nil t)))))

(defun emigo-get-buffer-name (&optional use-existing session-path)
  "Generate or find the emigo buffer name based on the session path.
If USE-EXISTING is non-nil, try to find an existing buffer.
SESSION-PATH defaults to the path determined by `emigo-project-root` or `default-directory`.
Searches parent directories for existing sessions."
  (let* ((current-session-path (file-truename (or session-path
                                                  (if current-prefix-arg ;; Check prefix arg for context
                                                      default-directory
                                                    (emigo-project-root)))))
         (target-buffer-name (format "*emigo:%s*" current-session-path)))
    (if use-existing
        (or (and (get-buffer target-buffer-name) target-buffer-name) ;; Exact match first
            ;; Search parent directories for existing sessions
            (let* ((emigo-buffers (seq-filter #'emigo--is-emigo-buffer-p (buffer-list)))
                   (buffer-session-paths
                    (mapcar (lambda (buf)
                              (when (string-match "^\\*emigo:\\(.*?\\)\\*$" (buffer-name buf))
                                (match-string 1 (buffer-name buf))))
                            emigo-buffers))
                   ;; Find closest parent directory that has an emigo session
                   (closest-parent-session-path
                    (car (sort (seq-filter (lambda (path)
                                             (and path
                                                  (file-in-directory-p current-session-path path)
                                                  (file-exists-p path)))
                                           buffer-session-paths)
                               (lambda (a b) (> (length a) (length b))))))) ;; Sort by length (deepest first)
              (when closest-parent-session-path
                (format "*emigo:%s*" closest-parent-session-path)))
            (emigo-select-buffer-name)) ;; Fallback to interactive selection if no match found
      ;; Not using existing, just return the calculated name
      target-buffer-name)))

;; --- End new functions ---

(defvar emigo-project-buffers nil)   ;; Keep track of buffer objects

(defvar-local emigo-session-path nil ;; Buffer-local session path
  "The session path (project root or current dir) associated with this Emigo buffer.")

(defvar-local emigo--llm-output "" ;; Buffer-local LLM output accumulator
  "Accumulates the LLM output stream for the current interaction.")
;; Removed emigo-project-root buffer-local variable

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

(defcustom emigo-prompt-string "Emigo> "
  "The prompt string used in Emigo buffers."
  :type 'string
  :group 'emigo)

(defun emigo--user-emacs-directory ()
  "Get lang server with project path, file path or file extension."
  (expand-file-name user-emacs-directory))

(defun emigo--ensure-session-path (session-path)
  "Ensure SESSION-PATH is valid, erroring if nil."
  (unless session-path
    (error "[Emigo] Could not determine session path"))
  session-path)

(defun emigo-call-async (method &rest args)
  "Call Python EPC function METHOD and ARGS asynchronously."
  (if (emigo-epc-live-p emigo-epc-process)
      (emigo-deferred-chain
        (emigo-epc-call-deferred emigo-epc-process (read method) args))
    ;; If process not live, queue the first call details
    (setq emigo-first-call-method method)
    (setq emigo-first-call-args args)
    (message "[Emigo] Process not started, queuing call: %s" method)
    (emigo-start-process)))

(defun emigo-call--sync (method &rest args)
  "Call Python EPC function METHOD and ARGS synchronously."
  (if (emigo-epc-live-p emigo-epc-process)
      (emigo-epc-call-sync emigo-epc-process (read method) args)
    (message "[Emigo] Process not started.")
    nil))

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
  (emigo--kill-python-process))

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
                               (read emigo-first-call-method) ;; Method name
                               emigo-first-call-args) ;; Args list (already includes session_path)
      (setq emigo-first-call-method nil)
      (setq emigo-first-call-args nil)
      )))

(defun emigo-enable ()
  (add-hook 'post-command-hook #'emigo-start-process))

(defun emigo-read-file-content (filepath)
  (with-temp-buffer
    (insert-file-contents filepath)
    (string-trim (buffer-string))))


(defun emigo-update-header-line (session-path)
  (setq header-line-format (concat
                            (propertize (format " Project [%s]" (emigo-format-session-path session-path)) 'face font-lock-constant-face))))

(defun emigo-shrink-dir-name (input-string)
  (let* ((words (split-string input-string "-"))
         (abbreviated-words (mapcar (lambda (word) (substring word 0 (min 1 (length word)))) words)))
    (mapconcat 'identity abbreviated-words "-")))

(defun emigo-format-session-path (session-path)
  (let* ((file-path (split-string session-path "/" t))
         (full-num 2)
         (show-name nil)
         shown-path)
    (setq show-path
          (if buffer-file-name
              (if show-name file-path (butlast file-path))
            file-path))
    (setq show-path (nthcdr (- (length show-path)
                               (if buffer-file-name
                                   (if show-name (1+ full-num) full-num)
                                 (1+ full-num)))
                            show-path))
    ;; Shrink parent directory name to save minibuffer space.
    (setq show-path
          (append (mapcar #'emigo-shrink-dir-name (butlast show-path))
                  (last show-path)))
    ;; Join paths.
    (setq show-path (mapconcat #'identity show-path "/"))
    show-path))

;; Define the main entry point command
;;;###autoload
(defun emigo ()
  "Start or switch to an Emigo session.
With no prefix arg, uses the project root (e.g., Git root) as the session path.
With a prefix arg (C-u), uses the current directory (`default-directory`)
as the session path."
  (interactive)
  (let* ((session-path (if current-prefix-arg
                           (file-truename default-directory)
                         (file-truename (emigo-project-root))))
         (buffer-name (emigo-get-buffer-name nil session-path))
         (buffer (get-buffer-create buffer-name))
         (prompt (read-string (format "Emigo Prompt (%s): "
                                      (if current-prefix-arg default-directory "project root")))))
    (setq prompt (substring-no-properties prompt))
    ;; Ensure EPC process is running or starting
    (unless (emigo-epc-live-p emigo-epc-process)
      (emigo-start-process))

    ;; Set buffer-local session path variable
    (with-current-buffer buffer
      (emigo-mode)

      (emigo-update-header-line session-path)
      (setq-local emigo-session-path session-path))

    ;; Add buffer to tracked list
    (add-to-list 'emigo-project-buffers buffer t) ;; Use t to avoid duplicates

    ;; Switch to or display the buffer
    (emigo-create-window buffer) ;; Use the specific buffer

    ;; Send prompt if provided
    (when (and prompt (not (string-empty-p prompt)))
      (emigo-call-async "emigo_session" session-path prompt))))

(defcustom emigo-dedicated-window-width 50
  "The height of `emigo' dedicated window."
  :type 'integer
  :group 'emigo)

(defvar emigo-dedicated-window nil
  "The dedicated `emigo' window.")

(defvar emigo-dedicated-buffer nil
  "The dedicated `emigo' buffer.")

(defvar emigo-saved-window-width nil
  "Saved width of emigo dedicated window before ediff.")

(defun emigo-save-window-width ()
  "Save the current width of emigo dedicated window before ediff."
  (when (and emigo-dedicated-window (window-live-p emigo-dedicated-window))
    (setq emigo-saved-window-width (window-width emigo-dedicated-window))))

(defun emigo-restore-window-width ()
  "Restore the saved width of emigo dedicated window after ediff."
  (when (and emigo-dedicated-window
             (window-live-p emigo-dedicated-window)
             emigo-saved-window-width)
    (let ((current-width (window-width emigo-dedicated-window)))
      (unless (= current-width emigo-saved-window-width)
        (window-resize emigo-dedicated-window
                       (- emigo-saved-window-width current-width)
                       t)))))

;; Add hooks for ediff
(add-hook 'ediff-before-setup-hook #'emigo-save-window-width)
(add-hook 'ediff-quit-hook #'emigo-restore-window-width)

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
  (set-window-dedicated-p (selected-window) t)
  ;; Save initial window width
  (setq emigo-saved-window-width (window-width emigo-dedicated-window)))

(defun emigo-dedicated-select-window ()
  "Select emigo dedicated window."
  (select-window emigo-dedicated-window)
  (set-window-dedicated-p (selected-window) t))

(defun emigo-create-window (buffer)
  "Display BUFFER in the dedicated Emigo window."
  (unless (bufferp buffer)
    (error "[Emigo] Invalid buffer provided to emigo-create-ai-window: %s" buffer))
  (setq emigo-dedicated-buffer buffer)
  (unless (emigo-window-exist-p emigo-dedicated-window)
    (setq emigo-dedicated-window
          (display-buffer buffer ;; Display the specific buffer
                          `(display-buffer-in-side-window
                            (side . right)
                            (window-width . ,emigo-dedicated-window-width)))))
  (select-window emigo-dedicated-window)
  (set-window-buffer emigo-dedicated-window emigo-dedicated-buffer) ;; Ensure correct buffer is shown
  (set-window-dedicated-p (selected-window) t))

(defvar emigo-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "C-a") #'emigo-beginning-of-line)
    (define-key map (kbd "C-m") #'emigo-send-prompt)
    (define-key map (kbd "C-c C-c") #'emigo-send-prompt)
    (define-key map (kbd "C-c C-r") #'emigo-restart-process)
    (define-key map (kbd "C-c C-f") #'emigo-remove-file-from-context)
    (define-key map (kbd "C-c C-l") #'emigo-list-context-files)
    (define-key map (kbd "S-<return>") #'emigo-send-newline)
    map)
  "Keymap used by `emigo-mode'.")

(define-derived-mode emigo-mode fundamental-mode "emigo"
  "Major mode for Emigo AI chat sessions.
\\{emigo-mode-map}"
  :group 'emigo
  (setq major-mode 'emigo-mode)
  (setq mode-name "emigo")
  (use-local-map emigo-mode-map)
  (run-hooks 'emigo-mode-hook))

(defun emigo-send-newline ()
  "Insert a newline character at point."
  (interactive)
  (insert "\n"))

(defun emigo-beginning-of-line ()
  "Move to the beginning of the current line or the prompt position."
  (interactive)
  (if (save-excursion
        (search-backward-regexp (concat "^" emigo-prompt-string) (line-beginning-position) t))
      (progn
        (goto-char (line-beginning-position))
        (forward-char (length emigo-prompt-string)))
    (goto-char (line-beginning-position))))

(defun emigo-send-prompt ()
  "Send the current prompt to the AI."
  (interactive)
  ;; Clear the LLM output accumulator for the new interaction
  (setq-local emigo--llm-output "")
  (let ((prompt (save-excursion
                  (goto-char (point-max))
                  (search-backward-regexp (concat "^" emigo-prompt-string) nil t)
                  (forward-char (length emigo-prompt-string))
                  (string-trim (buffer-substring-no-properties (point) (point-max)))
                  )))
    (if (string-empty-p prompt)
        (message "Please type prompt to send.")
      (search-backward-regexp (concat "^" emigo-prompt-string) nil t)
      (forward-char (length emigo-prompt-string))
      (delete-region (point) (point-max))
      (emigo-call-async "emigo_session" emigo-session-path prompt))))

(defun emigo-flush-buffer (session-path content role &rest init)
  "Flush CONTENT to the Emigo buffer associated with SESSION-PATH."
  (let ((buffer-name (emigo-get-buffer-name nil session-path))
        (buffer (get-buffer (emigo-get-buffer-name t session-path)))) ;; Find existing buffer
    (unless buffer
      (warn "[Emigo] Could not find buffer for session %s to flush content." session-path)
      (cl-return-from emigo-flush-buffer))

    (with-current-buffer buffer
      (when init
        (goto-char (point-min))
        (insert (propertize (concat "\n\n" emigo-prompt-string) 'face font-lock-keyword-face)))

      (save-excursion
        (let ((inhibit-read-only t)) ;; Allow modification even if buffer is read-only
          (goto-char (point-max))
          (search-backward-regexp (concat "^" emigo-prompt-string) nil t)
          (forward-line -2)
          (goto-char (line-end-position))

          (if (equal role "user")
              (insert (propertize content 'face font-lock-keyword-face))
            (insert content)
            (setq-local emigo--llm-output (concat emigo--llm-output content)))

          (goto-char (point-max))
          (search-backward-regexp (concat "^" emigo-prompt-string) nil t)
          (forward-char (1- (length emigo-prompt-string)))
          (emigo-lock-region (point-min) (point))
          )))))

(defun emigo-lock-region (beg end)
  "Super-lock the region from BEG to END."
  (interactive "r")
  (put-text-property beg end 'read-only t)
  (let ((overlay (make-overlay beg end)))
    (overlay-put overlay 'modification-hooks (list (lambda (&rest args))))
    (overlay-put overlay 'front-sticky t)
    (overlay-put overlay 'rear-nonsticky nil)))

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

(defun emigo-remove-file-from-context ()
  "Remove a file from the current project's Emigo chat context."
  (interactive)
  (unless (emigo-epc-live-p emigo-epc-process)
    (message "[Emigo] Process not running.")
    (emigo-start-process)            ; Attempt to start if not running
    (error "Emigo process was not running, please try again shortly."))

  (let ((buffer (emigo-get-buffer-name t)))
    (unless buffer
      (error "[Emigo] No Emigo buffer found"))

    (with-current-buffer buffer
      (unless emigo-session-path
        (error "[Emigo] Could not determine session path from buffer"))

      (let ((chat-files (emigo-call--sync "get_chat_files" emigo-session-path))
            (file-to-remove nil))

        (unless chat-files
          (message "[Emigo] No files currently in chat context for session: %s" emigo-session-path)
          (cl-return-from emigo-remove-file-from-context))

        (setq file-to-remove (completing-read "Remove file from context: " chat-files nil t))

        (when (and file-to-remove (member file-to-remove chat-files))
          (emigo-call-async emigo-session-path "remove_file_from_context" file-to-remove)
          ;; Message will be sent from Python side upon successful removal
          )))))

(defun emigo-list-context-files ()
  "List the files currently in the Emigo chat context for the current project."
  (interactive)
  (unless (emigo-epc-live-p emigo-epc-process)
    (message "[Emigo] Process not running.")
    (emigo-start-process)            ; Attempt to start if not running
    (error "Emigo process was not running, please try again shortly."))

  (let ((buffer (emigo-get-buffer-name t)))
    (unless buffer
      (error "[Emigo] No Emigo buffer found"))

    (with-current-buffer buffer
      (unless emigo-session-path
        (error "[Emigo] Could not determine session path from buffer"))

      (let ((chat-files (emigo-call--sync "get_chat_files" emigo-session-path)))
        (if chat-files
            (message "[Emigo] Files added in session %s: %s"
                     emigo-session-path
                     (mapconcat #'identity chat-files ", "))
          (message "[Emigo] No files currently in chat context for session: %s" emigo-session-path))))))

(provide 'emigo)

;;; emigo.el ends here
