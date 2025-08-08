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
;; Last-Updated: Mon Apr  7 17:55:18 2025 (-0400)
;;           By: Mingde (Matthew) Zeng
;; Package-Requires: ((emacs "26.1") (transient "0.3.0") (compat "30.0.2.0") (markdown-mode "2.6"))
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

(defcustom emigo-extra-headers ""
  "Custom header for AI model API requests.")

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
             (emigo-epc-define-method mngr 'get-user-emacs-directory 'emigo--user-emacs-directory)
             (emigo-epc-define-method mngr 'request-tool-approval-sync 'emigo--request-tool-approval-sync)
             (emigo-epc-define-method mngr 'ask-user-sync 'emigo--ask-user-sync)
             (emigo-epc-define-method mngr 'signal-completion 'emigo--signal-completion)
             (emigo-epc-define-method mngr 'replace-regions-sync 'emigo--replace-regions-sync)
             (emigo-epc-define-method mngr 'file-written-externally 'emigo--file-written-externally)
             (emigo-epc-define-method mngr 'agent-finished 'emigo--agent-finished)
             (emigo-epc-define-method mngr 'execute-command-sync 'emigo--execute-command-sync)
             (emigo-epc-define-method mngr 'list-files-sync 'emigo--list-files-sync)
             (emigo-epc-define-method mngr 'search-files-sync 'emigo--search-files-sync)
             ;; Update flush-buffer signature to accept optional tool_id and tool_name
             (emigo-epc-define-method mngr 'flush-buffer 'emigo--flush-buffer '((session-path string) (content string) (role string) &optional tool-id tool-name))
             (emigo-epc-define-method mngr 'yes-or-no-p 'yes-or-no-p))))
    (if emigo-server
        (setq emigo-server-port (process-contact emigo-server :service))
      (error "[Emigo] emigo-server failed to start")))
  emigo-server)

(defun emigo--eval-in-emacs-func (sexp-string)
  (eval (read sexp-string))
  ;; Return nil to avoid epc error `Got too many parameters in the reply'.
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

(defvar emigo-project-buffers nil) ;; Keep track of buffer objects

(defvar-local emigo-session-path nil ;; Buffer-local session path
  "The session path (project root or current dir) associated with this Emigo buffer.")

(defvar-local emigo--llm-output "" ;; Buffer-local LLM output accumulator
  "Accumulates the LLM output stream for the current interaction.")

(defvar emigo--tool-json-block ""
  "Tracks current fragments of a tool call JSON being inserted.")

(defvar-local emigo-chat-file-info nil
  "String displaying info about files in chat context (e.g., '3 files [1234 tokens]').")

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
Then Emigo will start by gdb, please send new issue with `emigo-name' buffer content when next crash."
  :type 'boolean)

(defcustom emigo-enable-log nil
  "Enable this option to print log message in `emigo-name' buffer, default only print message header."
  :type 'boolean)

(defcustom emigo-enable-profile nil
  "Enable this option to output performance data to ~/emigo.prof."
  :type 'boolean)

(defcustom emigo-prompt-symbol "Emigo> "
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
       ;; Pass args as a list directly
       (emigo-epc-call-deferred emigo-epc-process (read method) args))
    ;; If process not live, queue the first call details
    (setq emigo-first-call-method method)
    (setq emigo-first-call-args args) ;; Store args as a list
    (message "[Emigo] Process not started, queuing call: %s" method)
    (emigo-start-process)))

(defun emigo-call--sync (method &rest args)
  "Call Python EPC function METHOD and ARGS synchronously."
  (if (emigo-epc-live-p emigo-epc-process)
      ;; Pass args as a list directly
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

      ;; Set process parameters.
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

  ;; Close dedicated window and cancel timer.
  (emigo-close)
  (emigo--cancel-dedicated-window-timer)

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

  (when (and emigo-first-call-method emigo-first-call-args)
    ;; If first call details exist, execute the deferred call
    (emigo-deferred-chain
     (emigo-epc-call-deferred emigo-epc-process
                              (read emigo-first-call-method) ;; Method name
                              emigo-first-call-args) ;; Pass the stored args list
     (setq emigo-first-call-method nil)
     (setq emigo-first-call-args nil))))

(defun emigo-enable ()
  (add-hook 'post-command-hook #'emigo-start-process))

(defun emigo-update-header-line (session-path)
  (setq header-line-format (concat
                            (propertize (format " Project [%s]" (emigo-format-session-path session-path)) 'face font-lock-constant-face)
                            (when emigo-chat-file-info
                              (propertize (format " | %s" emigo-chat-file-info) 'face font-lock-constant-face)))))

(defun emigo-update-chat-files-info (session-path chat-files)
  (let ((buffer (get-buffer (emigo-get-buffer-name t session-path)))) ;; Find existing buffer
    (with-current-buffer buffer
      (setq-local emigo-chat-file-info chat-files)))
  (emigo-update-header-line session-path))

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
  (if (emigo-buffer-exist-p emigo-buffer)
      (emigo-open)
    (let* ((session-path (if current-prefix-arg
                             (file-truename default-directory)
                           (file-truename (emigo-project-root))))
           (buffer-name (emigo-get-buffer-name nil session-path))
           (buffer (get-buffer-create buffer-name)))
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

      (advice-add 'delete-other-windows :around #'emigo--advice-delete-other-windows)
      (advice-add 'other-windows :around #'emigo--advice-other-window)

      ;; Switch to or display the buffer
      (emigo-create-window buffer) ;; Use the specific buffer

      ;; Insert prompt.
      (insert (propertize (concat "\n\n" emigo-prompt-symbol) 'face font-lock-keyword-face)))))

;; --- Dedicated Window Width Enforcement ---

(defcustom emigo-window-width 50
  "The width of `emigo' dedicated window."
  :type 'integer
  :group 'emigo)

(defvar emigo-window nil
  "The dedicated `emigo' window.")

(defvar emigo-buffer nil
  "The dedicated `emigo' buffer.")

(defvar emigo-window-resize-timer nil
  "Timer to periodically enforce the dedicated window width.")

(defun emigo-ensure-window-width ()
  "Restore the saved width of emigo dedicated window."
  (when (and
         emigo-window-width
         (emigo-exist-p)
         (window-live-p emigo-window)
         (not (= (window-width emigo-window) emigo-window-width)))
    (window-resize emigo-window
                   (- emigo-window-width (window-width emigo-window))
                   t)))

(defun emigo--start-dedicated-window-timer ()
  "Start the timer to enforce the dedicated window width."
  (emigo--cancel-dedicated-window-timer) ;; Cancel existing timer first
  (setq emigo-window-resize-timer
        (run-with-timer 1 1 #'emigo-ensure-window-width))) ;; Check every 1 second

(defun emigo--cancel-dedicated-window-timer ()
  "Cancel the timer that enforces the dedicated window width."
  (when (timerp emigo-window-resize-timer)
    (cancel-timer emigo-window-resize-timer))
  (setq emigo-window-resize-timer nil))

;; --- End Dedicated Window Width Enforcement ---

(defun emigo-current-window-take-height (&optional window)
  "Return the height the `window' takes up.
Not the value of `window-width', it returns usable rows available for WINDOW.
If `window' is nil, get current window."
  (let ((edges (window-edges window)))
    (- (nth 3 edges) (nth 1 edges))))

(defun emigo-exist-p ()
  (and (emigo-buffer-exist-p emigo-buffer)
       (emigo-window-exist-p emigo-window)))

(defun emigo-window-exist-p (window)
  "Return `non-nil' if WINDOW exist.
Otherwise return nil."
  (and window (window-live-p window)))

(defun emigo-buffer-exist-p (buffer)
  "Return `non-nil' if `BUFFER' exist.
Otherwise return nil."
  (and buffer (buffer-live-p buffer)))

(defun emigo-close ()
  "Close dedicated `emigo' window."
  (interactive)
  (if (emigo-exist-p)
      (let ((current-window (selected-window)))
        ;; Cancel the resize timer first
        (emigo--cancel-dedicated-window-timer)
        ;; Remember height.
        (emigo-select-window)
        (delete-window emigo-window)
        (setq emigo-window nil) ;; Clear the variable
        (if (emigo-window-exist-p current-window)
            (select-window current-window)))
    (message "`EMIGO DEDICATED' window does not exist.")))

(defun emigo-toggle ()
  "Toggle dedicated `emigo' window."
  (interactive)
  (if (emigo-exist-p)
      (emigo-close)
    (emigo-open)))

(defun emigo-open ()
  "Open dedicated `emigo' window."
  (interactive)
  (if (emigo-window-exist-p emigo-window)
      (emigo-select-window)
    (emigo-pop-window)))

(defun emigo-pop-window ()
  "Pop emigo dedicated window if it exists."
  (setq emigo-window (display-buffer (current-buffer) `(display-buffer-in-side-window (side . right) (window-width . ,emigo-window-width))))
  (select-window emigo-window)
  (set-window-buffer emigo-window emigo-buffer)
  (set-window-dedicated-p (selected-window) t)
  ;; Start the enforcement timer
  (emigo--start-dedicated-window-timer))

(defun emigo-select-window ()
  "Select emigo dedicated window."
  (select-window emigo-window)
  (set-window-dedicated-p (selected-window) t))

(defun emigo-create-window (buffer)
  "Display BUFFER in the dedicated Emigo window."
  (unless (bufferp buffer)
    (error "[Emigo] Invalid buffer provided to emigo-create-ai-window: %s" buffer))
  (setq emigo-buffer buffer)
  (unless (emigo-window-exist-p emigo-window)
    (setq emigo-window
          (display-buffer buffer ;; Display the specific buffer
                          `(display-buffer-in-side-window
                            (side . right)
                            (window-width . ,emigo-window-width)))))
  (select-window emigo-window)
  (set-window-buffer emigo-window emigo-buffer) ;; Ensure correct buffer is shown
  (set-window-dedicated-p (selected-window) t)
  ;; Start the enforcement timer
  (emigo--start-dedicated-window-timer))

(defvar emigo-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "C-a") #'emigo-beginning-of-line)
    (define-key map (kbd "C-k") #'emigo-kill-line)
    (define-key map (kbd "C-m") #'emigo-send-prompt)
    (define-key map (kbd "C-c C-c") #'emigo-send-prompt)
    (define-key map (kbd "C-c r") #'emigo-restart-process)
    (define-key map (kbd "C-c j") #'emigo-drop-file-from-context)
    (define-key map (kbd "C-c f") #'emigo-add-file-to-context)
    (define-key map (kbd "C-c l") #'emigo-ls-files-in-context)
    (define-key map (kbd "C-c H") #'emigo-clear-history)
    (define-key map (kbd "C-c h") #'emigo-show-history)
    (define-key map (kbd "C-c p") #'emigo-show-proc-buffer)
    (define-key map (kbd "C-c C-c") #'emigo-stop-call)
    (define-key map (kbd "S-<return>") #'emigo-send-newline)
    (define-key map (kbd "M-p") #'emigo-previous-prompt)
    (define-key map (kbd "M-n") #'emigo-next-prompt)
    (define-key map (kbd "<backspace>") #'emigo-backward-delete-char)
    (define-key map (kbd "DEL") #'emigo-backward-delete-char)
    map)
  "Keymap used by `emigo-mode'.")

(defvar-local emigo--prompt-history '("")
  "History of previous prompts in Emigo sessions.")

(defvar emigo-prompt-history-index 0
  "Current index in the prompt history.")

(defun emigo--cycle-prompt-history (direction)
  "Cycle through prompt history in DIRECTION (1 for older, -1 for newer).
Index 0 always corresponds to an empty prompt string."
  (when (> (length emigo--prompt-history) 1) ;; Only cycle if there's more than just ""
    (let ((hist-len (length emigo--prompt-history)))
      (setq emigo-prompt-history-index (+ emigo-prompt-history-index direction))
      ;; Wrap around logic
      (cond
       ((>= emigo-prompt-history-index hist-len) ;; Went past oldest, wrap to empty ""
        (setq emigo-prompt-history-index 0))
       ((< emigo-prompt-history-index 0) ;; Went past newest, wrap to oldest
        (setq emigo-prompt-history-index (1- hist-len))))

      ;; Go to the end, find the start of the current prompt text, delete it, and insert history
      (goto-char (point-max))
      (when (search-backward-regexp (concat "^" (regexp-quote emigo-prompt-symbol)) nil t)
        (progn ;; Ensure both actions happen only if search succeeds
          (forward-char (length emigo-prompt-symbol))
          (delete-region (point) (point-max))))
      (insert (nth emigo-prompt-history-index emigo--prompt-history)))))

(defun emigo-previous-prompt ()
  "Navigate to previous prompt in history."
  (interactive)
  (let ((inhibit-read-only t))
    ;; Cycle towards older prompts (higher index in newest-first list)
    (emigo--cycle-prompt-history 1)))

(defun emigo-next-prompt ()
  "Navigate to next prompt in history."
  (interactive)
  (let ((inhibit-read-only t))
    ;; Cycle towards newer prompts (lower index in newest-first list)
    (emigo--cycle-prompt-history -1)))

(defun emigo--protect-prompt-line-p ()
  "Return non-nil if current line contains the prompt and should be protected."
  (save-excursion
    (beginning-of-line)
    (and (eq major-mode 'emigo-mode)
         (looking-at-p (concat "^" (regexp-quote emigo-prompt-symbol))))))

(define-derived-mode emigo-mode fundamental-mode "emigo"
  "Major mode for Emigo AI chat sessions.
\\{emigo-mode-map}"
  :group 'emigo
  (setq major-mode 'emigo-mode)
  (setq mode-name "emigo")
  (use-local-map emigo-mode-map)
  (setq-local emigo--prompt-history '(""))
  (setq-local emigo-prompt-history-index 0)
  (run-hooks 'emigo-mode-hook))

(defun emigo-send-newline ()
  "Insert a newline character at point."
  (interactive)
  (insert "\n"))

(defun emigo-beginning-of-line ()
  "Move to the beginning of the current line or the prompt position."
  (interactive)
  (if (emigo--protect-prompt-line-p)
      (progn
        (goto-char (line-beginning-position))
        (forward-char (length emigo-prompt-symbol)))
    (goto-char (line-beginning-position))))

(defun emigo-backward-delete-char ()
  "Delete the character before point, unless at the prompt boundary.
This is similar to `backward-delete-char' but protects the prompt line."
  (interactive)
  (let ((prompt-start (save-excursion
                        (goto-char (line-beginning-position))
                        (when (looking-at (regexp-quote emigo-prompt-symbol))
                          (point)))))
    (if (and prompt-start
             (<= (point) (+ prompt-start (length emigo-prompt-symbol))))
        (ding)
      (backward-delete-char 1))))

(defun emigo-kill-line ()
  "Kill the line in Emigo buffer with special handling for prompt lines.
If on a prompt line:
- Fails if point is within the prompt string
- Kills from point to end of line if after prompt
- Kills entire line if at end of line after prompt"
  (interactive)
  (when (emigo--protect-prompt-line-p)
    (let* ((line-start (line-beginning-position)))
      (if (< (point) (+ line-start (length emigo-prompt-symbol)))
          (ding)
        ;; We're after the prompt
        (let ((inhibit-read-only t))
          (if (eolp)
              ;; At end of line - kill whole line including newline
              (kill-region (+ line-start (length emigo-prompt-symbol)) (line-end-position))
            ;; Not at end - kill from point to end
            (kill-region (point) (line-end-position))))))))

(defun emigo-send-prompt ()
  "Send the current prompt to the AI."
  (interactive)
  ;; Clear the LLM output accumulator for the new interaction
  (setq-local emigo--llm-output "")
  (let ((prompt (save-excursion
                  (goto-char (point-max))
                  (search-backward-regexp (concat "^" emigo-prompt-symbol) nil t)
                  (forward-char (length emigo-prompt-symbol))
                  (string-trim (buffer-substring-no-properties (point) (point-max)))
                  )))
    (if (string-empty-p prompt)
        (ding)
      ;; Add prompt after the initial "" (at index 1)
      (setcdr emigo--prompt-history (cons prompt (cdr emigo--prompt-history)))
      ;; Reset index to 0 (pointing to the empty string)
      (setq emigo-prompt-history-index 0)
      ;; Send prompt
      (goto-char (point-max)) ;; Go to end before searching back
      (when (search-backward-regexp (concat "^" (regexp-quote emigo-prompt-symbol)) nil t)
        (forward-char (length emigo-prompt-symbol))
        (delete-region (point) (point-max)))
      (emigo-call-async "emigo_send" emigo-session-path prompt))))

(defun emigo--flush-buffer (session-path content &optional role tool-id tool-name)
  "Flush CONTENT to the Emigo buffer associated with SESSION-PATH.
ROLE indicates the type of content (e.g., 'user', 'llm', 'tool_json', 'tool_json_args').
TOOL-ID is used for streaming tool JSON fragments.
TOOL-NAME is provided explicitly when ROLE is 'tool_json'."
  (let ((buffer (get-buffer (emigo-get-buffer-name t session-path)))) ;; Find existing buffer
    (unless buffer
      (warn "[Emigo] Could not find buffer for session %s to flush content: %s" session-path content)
      (cl-return-from emigo--flush-buffer))

    (with-current-buffer buffer
      (save-excursion
        (let ((inhibit-read-only t)) ;; Allow modification
          ;; Go to the end of the buffer before the prompt
          (goto-char (point-max))
          (when (search-backward-regexp (concat "^" emigo-prompt-symbol) nil t)
            (forward-line -2)
            (goto-char (line-end-position)))

          ;; --- Insert new content based on role ---
          (cond
           ((equal role "user")
            (insert (propertize content 'face font-lock-keyword-face)))

           ((equal role "tool_json") ;; Start of a new tool call block
            (let ((display-name (or tool-name "(unknown tool)")))
              (insert (propertize (format "\n--- Tool Call: %s ---\n" display-name) 'face 'font-lock-comment-face))
              (insert emigo--tool-json-block)))

           ((equal role "tool_json_args") ;; Middle part (arguments)
            (setq emigo--tool-json-block (concat emigo--tool-json-block content))
            (insert content)
            (when (string-suffix-p "\\n" emigo--tool-json-block)
              (insert "\n")))

           ((equal role "tool_json_end") ;; Explicit end marker from Python
            (unless (looking-back "\\n" 1) (insert "\n")) ;; Ensure newline before end marker
            (insert (propertize "\n--- End Tool Call ---\n" 'face 'font-lock-comment-face))
            (setq emigo--tool-json-block ""))

           ((equal role "llm")
            (unless (string-empty-p emigo--tool-json-block)
              (setq emigo--tool-json-block ""))
            (setq-local emigo--llm-output (concat emigo--llm-output content))
            (insert content))

           (t (insert content)))

          ;; --- Update read-only region ---
          (goto-char (point-max))
          (when (search-backward-regexp (concat "^" (regexp-quote emigo-prompt-symbol)) nil t)
            (forward-char (1- (length emigo-prompt-symbol)))
            (emigo-lock-region (point-min) (point))))))))

(defun emigo-lock-region (beg end)
  "Super-lock the region from BEG to END."
  (interactive "r")
  (put-text-property beg end 'read-only t)
  (let ((overlay (make-overlay beg end)))
    (overlay-put overlay 'modification-hooks (list (lambda (&rest args))))
    (overlay-put overlay 'front-sticky t)
    (overlay-put overlay 'rear-nonsticky nil)))

;; --- Window Management Advice ---

(defun emigo--advice-delete-other-windows (orig-fun &rest args)
  "Around advice for `delete-other-windows'.
Prevent deleting the dedicated Emigo window."
  (if (and (emigo-window-exist-p emigo-window)
           (not (eq (selected-window) emigo-window)))
      ;; If Emigo window exists and is not the selected one,
      ;; delete all other non-dedicated windows except the current one.
      (let ((current-window (selected-window)))
        (dolist (win (window-list))
          (when (and (window-live-p win)
                     (not (eq current-window win))
                     (not (eq emigo-window win)) ; Don't delete emigo window
                     (not (window-dedicated-p win))) ; Don't delete other dedicated windows
            (delete-window win)))
        nil) ; Indicate deletion happened
    ;; Otherwise (Emigo window doesn't exist or is selected), run original.
    (apply orig-fun args)))

(defun emigo--advice-other-window (orig-fun &rest args)
  "Around advice for `other-window'.
Skip the dedicated Emigo window when cycling."
  (let ((target-window (apply orig-fun args))) ; Call original first
    (if (and (emigo-window-exist-p emigo-window)
             (eq target-window emigo-window)
             ;; Check if we are trying to move *away* from emigo-window
             ;; or if the original call landed us there unintentionally.
             ;; This logic might need refinement depending on exact desired behavior.
             (not (eq (selected-window) emigo-window)))
        ;; If the original call selected the Emigo window, and it wasn't
        ;; the starting window, call other-window again with the same args
        ;; to skip over it.
        (apply orig-fun args)
      ;; Otherwise, return the window selected by the original call.
      target-window)))

(defun emigo--filter-return-other-window (window)
  "Filter return value of `other-window' to skip Emigo window."
  (if (and (emigo-window-exist-p emigo-window)
           (eq window emigo-window))
      ;; If the returned window is the emigo window, try again
      ;; This assumes the original `other-window` was called with count=1
      ;; A more robust solution would need access to the original args.
      (other-window 1)
    window))

;; --- End Window Management Advice ---


(defun emigo-add-file-to-context ()
  "Interactively add a file to the current project's Emigo chat context.
The file path is relative to the session directory."
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

      (let* ((default-directory emigo-session-path)
             (file-to-add (read-file-name "Add file to context: " default-directory)))
        (when file-to-add
          (emigo-call-async "add_file_to_context" emigo-session-path file-to-add))))))

(defun emigo-drop-file-from-context ()
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
          (cl-return-from emigo-drop-file-from-context))

        (setq file-to-remove (completing-read "Remove file from context: " chat-files nil t))

        (when (and file-to-remove (member file-to-remove chat-files))
          ;; Corrected: Pass method name first, then session path and file
          (emigo-call-async "remove_file_from_context" emigo-session-path file-to-remove)
          ;; Message will be sent from Python side upon successful removal
          )))))

;; --- Tool Execution & Interaction Handlers (Called from Python) ---

(defun emigo--request-tool-approval-sync (session-path tool-name params-json-string)
  "Ask the user for approval to execute TOOL-NAME with PARAMS-JSON-STRING.
Return t if approved, nil otherwise. Called synchronously by the agent.
PARAMS-JSON-STRING is expected to be a JSON string representing the parameters dictionary."
  (interactive) ;; For testing, remove later if only called programmatically
  (let* ((param-alist (ignore-errors (json-parse-string params-json-string :object-type 'alist))) ;; Parse JSON string into an alist
         (prompt-message
          (format "[Emigo Approval] Allow tool '%s' for session '%s'?\nParams:\n%s\nApprove? (y or n) "
                  tool-name
                  session-path
                  (if (listp param-alist) ;; Check if parsing succeeded and resulted in a list (alist)
                      (mapconcat (lambda (pair) (format "- %s: %S" (car pair) (cdr pair))) param-alist "\n")
                    (format "Invalid JSON parameters received: %s" params-json-string))))) ;; Show raw string if JSON parsing failed
    ;; Only proceed if parsing was successful
    (if (listp param-alist)
        (y-or-n-p prompt-message)
      ;; If parsing failed, display error and deny automatically
      (message "%s" prompt-message)
      (ding)
      nil)))

(defun emigo--ask-user-sync (session-path question options-json-string)
  "Ask the user QUESTION in the context of SESSION-PATH.
OPTIONS-JSON-STRING is a JSON array string like \"[\\\"Opt1\\\", \\\"Opt2\\\"]\" or \"[]\".
Returns the user's input string, or nil if cancelled/empty."
  (interactive) ;; For testing
  (let* ((options (ignore-errors (json-parse-string options-json-string)))
         (prompt (format "[Emigo Question] (%s)\n%s\n%sAnswer: "
                         session-path
                         question
                         (if (and (listp options) (> (length options) 0))
                             (concat (mapconcat (lambda (opt) (format "- %s" opt)) options "\n") "\n")
                           "")))
         (answer
          (if (and (listp options) (> (length options) 0))
              ;; Use completing-read if options are provided
              (completing-read prompt options nil t nil nil)
            ;; Otherwise, use read-string
            (read-string prompt))))
    (if (string-empty-p answer) nil answer)))

(defun emigo--signal-completion (session-path result-text command-string)
  "Signal that the agent has attempted completion for SESSION-PATH.
Display RESULT-TEXT and optionally offer to run COMMAND-STRING."
  (let ((buffer (get-buffer (emigo-get-buffer-name t session-path))))
    (when buffer
      (with-current-buffer buffer
        (let ((inhibit-read-only t))
          ;; Go to the end of the buffer
          (goto-char (point-max))
          ;; Search backwards for the last prompt
          (if (search-backward-regexp (concat "^" (regexp-quote emigo-prompt-symbol)) nil t)
              ;; If found, insert before the prompt
              (progn
                (insert (propertize "\n--- Completion Attempt ---\n" 'face 'font-lock-comment-face))
                (insert result-text)
                (insert (propertize "\n--- End Completion ---\n\n" 'face 'font-lock-comment-face))) ;; Add newline before prompt
            ;; If no prompt found (shouldn't happen in normal flow), insert at end
            (goto-char (point-max))
            (insert (propertize "\n--- Completion Attempt ---\n" 'face 'font-lock-comment-face))
            (insert result-text)
            (insert (propertize "\n--- End Completion ---\n" 'face 'font-lock-comment-face)))))
      (message "[Emigo] Task completed by agent for session: %s" session-path)
      (when (and command-string (not (string-empty-p command-string)))
        (if (y-or-n-p (format "Run demonstration command? `%s`" command-string))
            (emigo--execute-command-sync session-path command-string))))))

(defun emigo--replace-regions-sync (abs-path replacements-json-string)
  "Replace multiple regions in ABS-PATH based on data in REPLACEMENTS-JSON-STRING.
REPLACEMENTS-JSON-STRING is a JSON array of [start_line, end_line, replace_text] lists.
Lines are 1-based. End line is exclusive. Applies changes from end to start.
Returns t on success, error string on failure."
  (message "[Emigo] Starting multi-replace for %s" abs-path)
  (unless (file-writable-p abs-path)
    (error "File is not writable: %s" abs-path))

  ;; Parse JSON - handle both array and vector formats
  (let* ((json-array-type 'list) ;; Ensure JSON arrays become lists
         (replacements (json-read-from-string replacements-json-string))
         ;; Sort replacements by start line in descending order
         (sorted-replacements (sort (copy-sequence replacements)
                                    (lambda (a b) (> (nth 0 a) (nth 0 b)))))
         (buffer (find-file-noselect abs-path))
         (modified nil))
    (message "[Emigo] Parsed %d replacements" (length replacements))
    (unless buffer
      (error "Could not find or open buffer for %s" abs-path))

    (with-current-buffer buffer
      (let ((inhibit-read-only t)) ;; Ensure we can modify
        ;; Apply replacements from end to start
        (dolist (replacement sorted-replacements) ;; replacement is a list [start end text]
          (let ((start-line (nth 0 replacement))
                (end-line (nth 1 replacement))
                (replace-text (nth 2 replacement)))
            (message "[Emigo] Applying replacement: lines %d-%d (%d chars)"
                     start-line end-line (length replace-text))
            ;; Go to the start position (beginning of start-line)
            (goto-char (point-min))
            (forward-line (1- start-line)) ;; 0-based movement
            (let ((start-point (point)))

              (goto-char (point-min))
              (forward-line (1- end-line)) ;; Move to beginning of end-line
              (let ((end-point (point)))
                (message "[Emigo] Deleting region: %d-%d" start-point end-point)
                ;; Delete the region
                (delete-region start-point end-point)
                ;; Insert the replacement text at the start position
                (goto-char start-point)
                (message "[Emigo] Inserting %d chars" (length replace-text))
                (insert replace-text)
                ;; Mark buffer as modified for saving
                (set-buffer-modified-p t)
                (setq modified t))))))
      ;; Save the buffer if modified
      (when modified
        (message "[Emigo] Saving buffer...")
        (save-buffer buffer)
        ;; Inform Emacs about the change (e.g., revert other buffers visiting this file)
        (emigo--file-written-externally abs-path)
        (message "[Emigo] Save successful")
        t))))

(defun emigo--file-written-externally (abs-path)
  "Inform Emacs that the file at ABS-PATH was modified externally.
If the file is visited in a buffer, offer to revert it."
  (let ((buffer (find-buffer-visiting abs-path)))
    (when (and buffer (buffer-modified-p buffer))
      ;; If buffer is modified, ask user before reverting
      (when (y-or-n-p (format "File %s changed on disk; revert buffer?" (buffer-name buffer)))
        (with-current-buffer buffer
          (revert-buffer :ignore-auto :noconfirm))))
    (when (and buffer (not (buffer-modified-p buffer)))
      ;; If buffer is not modified, revert automatically
      (with-current-buffer buffer
        (revert-buffer :ignore-auto :noconfirm)))))

(defun emigo--agent-finished (session-path)
  "Callback function when the agent finishes its interaction for SESSION-PATH."
  ;; TODO: Maybe update a mode-line indicator?
  (message "[Emigo] Agent finished for session: %s" session-path)
  nil)

(defun emigo--execute-command-sync (session-path command-string)
  "Execute COMMAND-STRING synchronously in SESSION-PATH and return its output.
Handles potential errors and captures stdout/stderr."
  (unless (file-directory-p session-path)
    (error "Invalid session path for command execution: %s" session-path))
  (let ((default-directory session-path) ;; Execute in the session path
        (output-buffer (generate-new-buffer "*emigo-cmd-output*"))
        (error-output "")
        (exit-code nil))
    (unwind-protect
        (progn
          ;; Use call-process-shell-command to capture output
          (setq exit-code (call-process-shell-command command-string nil output-buffer t))
          (with-current-buffer output-buffer
            (buffer-string)))
      ;; Cleanup: kill the temporary buffer
      (when (buffer-live-p output-buffer)
        (kill-buffer output-buffer)))
    ;; Check exit code - simplistic error handling for now
    (unless (eq exit-code 0)
      (error "Command failed with exit code %s: %s" exit-code command-string))
    ;; Return the captured output (already done by progn)
    ))

(defun emigo--list-files-sync (abs-path recursive-p)
  "List files in ABS-PATH, optionally RECURSIVE-P. Returns a newline-separated string."
  (unless (file-directory-p abs-path)
    (error "Not a directory: %s" abs-path))
  (let ((files (if recursive-p
                   ;; directory-files-recursively needs careful handling of args
                   ;; Let's use find-lisp-find-files for simplicity if available,
                   ;; otherwise basic directory-files
                   (if (fboundp 'find-lisp-find-files) ;; Check if find-lisp is available
                       (find-lisp-find-files abs-path ".") ;; Simple recursive find
                     (directory-files abs-path t nil t)) ;; Basic recursive, might include ., ..
                 (directory-files abs-path t)))) ;; Non-recursive
    ;; Filter out . and .. if present from basic directory-files
    (setq files (seq-remove (lambda (f) (member (file-name-nondirectory f) '("." ".."))) files))
    ;; Return relative paths for consistency? No, agent expects paths relative to CWD.
    ;; Let Python handle making them relative if needed. Return full paths for now.
    (mapconcat #'identity files "\n")))

(defun emigo--search-files-sync (abs-path pattern &optional case-sensitive max-matches)
  "Search for PATTERN within files in ABS-PATH using grep.
Returns results in a format similar to grep output (file:line:match).
CASE-SENSITIVE defaults to t. If nil, performs case-insensitive search.
MAX-MATCHES limits the number of matches per file (requires GNU grep >= 2.5.1)."
  (unless (file-directory-p abs-path)
    (error "Not a directory: %s" abs-path))
  (let* ((default-directory abs-path) ;; Set CWD for grep
         (case-option (unless case-sensitive "-i")) ;; -i if case-sensitive is nil
         (max-option (when (and max-matches (> max-matches 0)) (format "-m%d" max-matches))) ;; Format as -mN
         ;; Build the argument list for call-process
         ;; Pass exclude-dir multiple times
         (grep-args (list "-nH" "-R"
                          "--exclude-dir=.git" "--exclude-dir=.hg" "--exclude-dir=.svn"
                          "--exclude-dir=node_modules" "--exclude-dir=venv" "--exclude-dir=.venv"
                          "--exclude-dir=dist" "--exclude-dir=build"))
         (grep-args (if case-option (cons case-option grep-args) grep-args))
         (grep-args (if max-option (cons max-option grep-args) grep-args))
         ;; Add the pattern and target directory (.)
         (grep-args (append grep-args (list "-e" pattern ".")))
         (output-buffer (generate-new-buffer "*emigo-grep-output*"))
         (results ""))
    (message "[Emigo] Running search command: grep %s" (mapconcat #'shell-quote-argument grep-args " ")) ;; Log the command being run
    (unwind-protect
        (progn
          ;; Use call-process, passing args as a list
          (let ((exit-code (apply #'call-process "grep" nil output-buffer t grep-args)))
            ;; Grep returns 1 if no lines selected, 0 if successful, >1 on error.
            ;; We ignore exit status 1 as it just means no matches.
            (unless (or (eq exit-code 0) (eq exit-code 1))
              (error "grep command failed with exit code %s: grep %s" exit-code (mapconcat #'shell-quote-argument grep-args " "))))
          (with-current-buffer output-buffer
            (setq results (string-trim (buffer-string)))))
      (when (buffer-live-p output-buffer)
        (kill-buffer output-buffer)))
    ;; Return the raw grep output string (empty if no matches)
    results))

(defun emigo--clear-local-buffer (session-path)
  "Clear the local Emacs buffer content and history for SESSION-PATH.
Preserves the prompt history for convenience."
  (let ((buffer (get-buffer (emigo-get-buffer-name t session-path))))
    (when buffer
      (with-current-buffer buffer
        ;; Save current prompt history before clearing
        (let ((saved-prompt-history emigo--prompt-history))
          ;; Clear local buffer output accumulator
          (setq-local emigo--llm-output "")
          ;; History is managed on the Python side, no local history to clear
          ;; Erase buffer content and reset prompt
          (let ((inhibit-read-only t))
            (erase-buffer)
            (insert (propertize (concat "\n\n" emigo-prompt-symbol) 'face font-lock-keyword-face))
            (goto-char (point-max)))
          ;; Restore the prompt history after clearing
          (setq-local emigo--prompt-history saved-prompt-history)
          (message "Local buffer cleared for session: %s" session-path))))))

(defun emigo-clear-history ()
  "Clear the chat history (both remote LLM and local buffer) for the current Emigo session.
Preserves the prompt history for convenience."
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

      ;; Save current prompt history before clearing
      (let ((saved-prompt-history emigo--prompt-history))
        ;; Call Python side to clear LLM history and trigger local buffer clear
        (if (emigo-call--sync "clear_history" emigo-session-path)
            (progn
              ;; Restore the prompt history after clearing
              (setq-local emigo--prompt-history saved-prompt-history)
              (message "Cleared history for session: %s" emigo-session-path))
          (message "Failed to clear history for session: %s" emigo-session-path))))))

(defun emigo-show-history ()
  "Display the chat history for the current Emigo session in a new Org buffer."
  (interactive)
  (unless (emigo--is-emigo-buffer-p)
    (error "Not in an Emigo buffer"))

  (let* ((history-buffer-name (format "*emigo-history:%s*" emigo-session-path))
         (buf (get-buffer-create history-buffer-name))
         (session-path emigo-session-path)
         (history (emigo-call--sync "get_history" emigo-session-path)))

    ;; Check if history is a list (basic validation)
    (unless (or history (listp history))
      (message "Received invalid history format from Python for session: %s" emigo-session-path)
      (when (get-buffer buf) (kill-buffer buf))
      (cl-return-from emigo-show-history))

    (with-current-buffer buf
      (let ((inhibit-read-only t)) ;; Allow modification during setup
        (erase-buffer)) ;; Erase first
      ;; Set modes *after* erasing and outside inhibit-read-only
      (org-mode)
      (setq buffer-read-only nil) ;; Make buffer writable
      (display-line-numbers-mode 1)
      ;; Store original session path for resending
      (setq-local emigo-session-path session-path)
      ;; Keybindings for the history buffer
      (local-set-key (kbd "q") (lambda () (interactive) (kill-this-buffer)))
      (local-set-key (kbd "C-c C-c") #'emigo-send-revised-history) ; New binding

      ;; Re-enable inhibit-read-only just for insertion if needed, though likely not necessary now
      (let ((inhibit-read-only t))
        ;; Keybindings for the history buffer
        (local-set-key (kbd "q") (lambda () (interactive) (kill-this-buffer)))
        (local-set-key (kbd "C-c C-c") #'emigo-send-revised-history) ; New binding

        ;; Iterate through history (list of (timestamp plist) lists from Python)
        (dolist (entry history)
          (let* ((timestamp-float (car entry)) ;; Timestamp is the first element
                 (message-plist (cadr entry)) ;; Message plist is the second element
                 (role (or (plist-get message-plist :role) "unknown"))
                 (content (or (plist-get message-plist :content) ""))
                 ;; Format the float timestamp
                 (timestamp-str (format-time-string "%F %T" timestamp-float)))
            ;; Insert Org heading for role/timestamp and source block for content
            (insert (format "* [%s] %s\n#+BEGIN_SRC %s\n%s\n#+END_SRC\n\n"
                            timestamp-str ;; Use the formatted timestamp string
                            (capitalize role) ;; Capitalize role (User, Assistant)
                            (if (equal role "user") "text" "markdown") ;; Basic language hint
                            (string-trim content)))))))
    (switch-to-buffer-other-window buf)))

(defun emigo--parse-history-buffer ()
  "Parse the current Org-mode history buffer into a list of message plists.
Returns a list suitable for sending back to Python: '((:role \"user\" :content \"...\") ...)."
  (unless (string-match-p "^\\*emigo-history:.*\\*$" (buffer-name))
    (error "Not in an Emigo history buffer"))
  (save-excursion
    (goto-char (point-min))
    (let ((history-list '()))
      (while (re-search-forward "^\\* \\[.*\\] \\(.*?\\)\n#\\+BEGIN_SRC.*?\\n\\(\\(?:.\\|\n\\)*?\\)#\\+END_SRC" nil t)
        (let* ((role-str (downcase (match-string 1))) ;; user, assistant, tool, etc.
               ;; Extract content *without* properties between BEGIN_SRC and END_SRC
               (content-start (match-beginning 2))
               (content-end (match-end 2))
               ;; Ensure start/end are valid before extracting
               (content (if (and content-start content-end)
                            (buffer-substring-no-properties content-start content-end)
                          ""))) ;; Default to empty string if match fails
          ;; Construct the plist for this message, ensuring content is trimmed
          (push `(:role ,role-str :content ,(string-trim content)) history-list)))
      ;; Reverse the list to maintain original order
      (nreverse history-list))))

(defun emigo-send-revised-history ()
  "Parse the current history buffer and send it to start a new interaction."
  (interactive)
  (unless (string-match-p "^\\*emigo-history:.*\\*$" (buffer-name))
    (error "Not in an Emigo history buffer"))
  (unless (emigo-epc-live-p emigo-epc-process)
    (message "[Emigo] Process not running.")
    (emigo-start-process)
    (error "Emigo process was not running, please try again shortly."))
  (unless emigo-session-path
    (error "[Emigo] Could not determine original session path from history buffer"))

  (let ((revised-history (emigo--parse-history-buffer)))
    (if (null revised-history)
        (message "[Emigo] History buffer is empty or could not be parsed.")
      (progn
        (message "[Emigo] Sending revised history to agent for session: %s" emigo-session-path)
        ;; Call the new Python EPC method
        (emigo-call-async "emigo_send_revised_history" emigo-session-path revised-history)
        ;; Optionally switch back to the main emigo buffer automatically
        (let ((main-buffer-name (format "*emigo:%s*" emigo-session-path)))
          (when (get-buffer main-buffer-name)
            (switch-to-buffer-other-window main-buffer-name)))))))

(defun emigo-stop-call ()
  "Cancel the currently running LLM interaction for this session."
  (interactive)
  (unless (emigo--is-emigo-buffer-p)
    (error "Not in an Emigo buffer"))
  (unless (emigo-epc-live-p emigo-epc-process)
    (message "[Emigo] Process not running.")
    (emigo-start-process)
    (error "Emigo process was not running, please try again shortly."))

  (unless emigo-session-path
    (error "[Emigo] Could not determine session path from buffer"))

  (message "[Emigo] Requesting cancellation for session: %s" emigo-session-path)
  ;; Call the new Python EPC method asynchronously
  (emigo-call-async "cancel_llm_interaction" emigo-session-path))

(defun emigo-ls-files-in-context ()
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

(defun emigo-show-proc-buffer ()
  "Open and switch to the Emigo process buffer `emigo-name'."
  (interactive)
  (let ((buf (get-buffer emigo-name)))
    (if buf
        (switch-to-buffer-other-window buf)
      (message "No Emigo process buffer found"))))

(provide 'emigo)
;;; emigo.el ends here
