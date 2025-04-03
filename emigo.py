#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
The central orchestrator for the Emigo Python backend.

This module runs the Python-side EPC (Emacs Process Communication) server,
allowing Emacs Lisp code to call Python functions. It manages the lifecycle
of the `llm_worker.py` subprocess, which handles the intensive LLM interactions.

Key Responsibilities:
- Manages multiple user sessions (`session.py`), holding state like chat history,
  files in context, caches, and RepoMapper instances.
- Receives commands and requests from the Emacs frontend (e.g., send prompt,
  add/remove file, clear history).
- Starts, stops, and communicates with the `llm_worker.py` process for
  handling agentic interactions.
- Receives tool execution requests from the `llm_worker.py`.
- Handles tool approval logic by calling back to Emacs (`utils.py`) for
  user confirmation when necessary.
- Dispatches approved tool requests to the implementations in `tools.py`.
- Manages the overall lifecycle and cleanup of the Python backend.

Note: This module currently has a wide range of responsibilities and could
potentially be refactored for better separation of concerns in the future.
"""

# Copyright (C) 2025 Emigo
#
# Author: Mingde (Matthew) Zeng <matthewzmd@posteo.net>
#         Andy Stewart <lazycat.manatee@gmail.com>
# Maintainer: Mingde (Matthew) Zeng <matthewzmd@posteo.net>
#             Andy Stewart <lazycat.manatee@gmail.com>
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import threading
import traceback
import subprocess
import json
import queue
import time
from typing import Dict, List, Optional, Tuple
from system_prompt import (
    TOOL_DENIED,
    # Tool Names
    TOOL_EXECUTE_COMMAND, TOOL_READ_FILE, TOOL_WRITE_TO_FILE,
    TOOL_REPLACE_IN_FILE, TOOL_SEARCH_FILES, TOOL_LIST_FILES,
    TOOL_LIST_REPOMAP, TOOL_ASK_FOLLOWUP_QUESTION, TOOL_ATTEMPT_COMPLETION
)
from epc.server import ThreadingEPCServer
from utils import *
import re
import json # For parsing JSON params before sending to tools
from session import Session
# Import tool dispatcher
import tools

class Emigo:
    def __init__(self, args):
        print("Emigo __init__: Starting initialization...", file=sys.stderr, flush=True) # DEBUG + flush
        # Init EPC client port.
        print(f"Emigo __init__: Received args: {args}", file=sys.stderr, flush=True) # DEBUG + flush
        if not args:
            print("Emigo __init__: ERROR - No arguments received (expected EPC port). Exiting.", file=sys.stderr, flush=True)
            sys.exit(1)
        try:
            elisp_epc_port = int(args[0])
            print(f"Emigo __init__: Attempting to connect to Elisp EPC server on port {elisp_epc_port}...", file=sys.stderr, flush=True) # DEBUG + flush
            # Initialize the EPC client connection to Emacs (utils.py) *before* using it
            init_epc_client(elisp_epc_port)
            print(f"Emigo __init__: EPC client initialized for Elisp port {elisp_epc_port}", file=sys.stderr, flush=True) # DEBUG + flush
        except (IndexError, ValueError) as e:
            print(f"Emigo __init__: ERROR - Invalid or missing Elisp EPC port argument: {args}. Error: {e}", file=sys.stderr, flush=True) # DEBUG + flush
            sys.exit(1)
        except Exception as e:
            print(f"Emigo __init__: ERROR initializing/connecting EPC client to Elisp: {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True) # DEBUG + flush
            sys.exit(1) # Exit if we can't connect back to Emacs

        # Init vars.
        print("Emigo __init__: Initializing internal variables...", file=sys.stderr, flush=True) # DEBUG + flush
        # Replace individual state dicts with a single sessions dictionary
        self.sessions: Dict[str, Session] = {} # Key: session_path, Value: Session object

        # --- Worker Process Management ---
        self.llm_worker_process: Optional[subprocess.Popen] = None
        self.llm_worker_reader_thread: Optional[threading.Thread] = None # Initialize to None
        self.llm_worker_stderr_thread: Optional[threading.Thread] = None # Initialize to None
        self.llm_worker_lock = threading.Lock()
        self.worker_output_queue = queue.Queue() # Queue for messages from worker stdout
        self.pending_tool_requests: Dict[str, Dict] = {} # {request_id: original_tool_request_data}
        self.active_interaction_session: Optional[str] = None # Track which session is currently interacting
        # Removed repo_mappers and session_caches, now managed by Session objects


        # --- EPC Server Setup ---
        print("Emigo __init__: Setting up Python EPC server...", file=sys.stderr, flush=True) # DEBUG + flush
        try:
            self.server = ThreadingEPCServer(('127.0.0.1', 0), log_traceback=True)
            # self.server.logger.setLevel(logging.DEBUG)
            self.server.allow_reuse_address = True
            print(f"Emigo __init__: Python EPC server created. Will listen on port {self.server.server_address[1]}", file=sys.stderr, flush=True) # DEBUG + flush
        except Exception as e:
            print(f"Emigo __init__: ERROR creating Python EPC server: {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True) # DEBUG + flush
            sys.exit(1)

        # ch = logging.FileHandler(filename=os.path.join(emigo_config_dir, 'epc_log.txt'), mode='w')
        # formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(lineno)04d | %(message)s')
        # ch.setFormatter(formatter)
        # ch.setLevel(logging.DEBUG)
        # self.server.logger.addHandler(ch)
        # self.server.logger = logger # Keep logging setup if needed

        print("Emigo __init__: Registering instance methods with Python EPC server...", file=sys.stderr, flush=True) # DEBUG + flush
        self.server.register_instance(self)  # register instance functions let elisp side call
        print("Emigo __init__: Instance registered with Python EPC server.", file=sys.stderr, flush=True) # DEBUG + flush

        # Start Python EPC server with sub-thread.
        try:
            print("Emigo __init__: Starting Python EPC server thread...", file=sys.stderr, flush=True) # DEBUG + flush
            self.server_thread = threading.Thread(target=self.server.serve_forever, name="PythonEPCServerThread")
            self.server_thread.daemon = True # Allow main thread to exit even if this hangs
            self.server_thread.start()
            # Give the server a moment to bind the port
            time.sleep(0.1)
            if not self.server_thread.is_alive():
                print("Emigo __init__: ERROR - Python EPC server thread failed to start.", file=sys.stderr, flush=True)
                sys.exit(1)
                print(f"Emigo __init__: Python EPC server thread started. Listening on port {self.server.server_address[1]}", file=sys.stderr, flush=True) # DEBUG + flush
        except Exception as e:
            print(f"Emigo __init__: ERROR starting Python EPC server thread: {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True) # DEBUG + flush
            sys.exit(1) # Exit if server thread fails

        # Start the worker process
        print("Emigo __init__: Starting LLM worker process...", file=sys.stderr, flush=True) # DEBUG + flush
        self._start_llm_worker()
        # Check if worker started successfully
        worker_ok = False
        with self.llm_worker_lock: # Ensure check happens after potential start attempt
            if self.llm_worker_process and self.llm_worker_process.poll() is None:
                worker_ok = True

        if not worker_ok:
            print("Emigo __init__: ERROR - LLM worker process failed to start or exited immediately.", file=sys.stderr, flush=True)
            # Attempt to read stderr if process object exists
            if self.llm_worker_process and self.llm_worker_process.stderr:
                try:
                    stderr_output = self.llm_worker_process.stderr.read()
                    print(f"Emigo __init__: Worker stderr upon exit:\n{stderr_output}", file=sys.stderr, flush=True)
                except Exception as read_err:
                    print(f"Emigo __init__: Error reading worker stderr after exit: {read_err}", file=sys.stderr, flush=True)
                    sys.exit(1) # Exit if worker failed

        print("Emigo __init__: LLM worker process started successfully.", file=sys.stderr, flush=True) # DEBUG + flush


        self.worker_processor_thread = threading.Thread(target=self._process_worker_queue, name="WorkerQueueProcessorThread", daemon=True)
        self.worker_processor_thread.start()
        if not self.worker_processor_thread.is_alive():
            print("Emigo __init__: ERROR - Worker queue processor thread failed to start.", file=sys.stderr, flush=True)
            sys.exit(1)
            print("Emigo __init__: Worker queue processor thread started.", file=sys.stderr, flush=True) # DEBUG + flush

        # Pass Python epc port back to Emacs when first start emigo.
        try:
            python_epc_port = self.server.server_address[1]
            print(f"Emigo __init__: Sending emigo--first-start signal to Elisp for Python EPC port {python_epc_port}...", file=sys.stderr, flush=True) # DEBUG + flush
            eval_in_emacs('emigo--first-start', python_epc_port)
            print(f"Emigo __init__: Sent emigo--first-start signal for port {python_epc_port}", file=sys.stderr, flush=True) # DEBUG + flush
        except Exception as e:
            # This might happen if Emacs EPC server isn't ready yet or the connection failed earlier.
            print(f"Emigo __init__: ERROR sending emigo--first-start signal to Elisp: {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True) # DEBUG + flush
            # Don't exit here, maybe the connection will recover, but log clearly.

        # Initialization complete. The main thread will likely wait for EPC events or signals.
        print("Emigo __init__: Initialization sequence complete. Emigo should be running.", file=sys.stderr, flush=True) # DEBUG + flush

    # --- Worker Process Management ---

    def _start_llm_worker(self):
        """Starts the llm_worker.py subprocess."""
        with self.llm_worker_lock:
            if self.llm_worker_process and self.llm_worker_process.poll() is None:
                print("LLM worker process already running.", file=sys.stderr)
                return # Already running

            worker_script = os.path.join(os.path.dirname(__file__), "llm_worker.py")
            python_executable = sys.executable # Use the same python interpreter
            worker_script_path = os.path.abspath(worker_script)

            try:
                print(f"_start_llm_worker: Starting LLM worker process: {python_executable} {worker_script_path}", file=sys.stderr, flush=True) # DEBUG + flush
                self.llm_worker_process = subprocess.Popen(
                    [python_executable, worker_script_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, # Capture stderr
                    text=True, # Work with text streams
                    encoding='utf-8',
                    bufsize=1, # Line buffered
                    cwd=os.path.dirname(worker_script_path) # Set CWD to script's directory
                )
                # Brief pause to see if process exits immediately
                time.sleep(0.2)
                if self.llm_worker_process.poll() is not None:
                    print(f"_start_llm_worker: ERROR - LLM worker process exited immediately with code {self.llm_worker_process.poll()}.", file=sys.stderr, flush=True)
                    # Try reading stderr quickly
                    try:
                        stderr_output = self.llm_worker_process.stderr.read()
                        print(f"_start_llm_worker: Worker stderr upon exit:\n{stderr_output}", file=sys.stderr, flush=True)
                    except Exception as read_err:
                        print(f"_start_llm_worker: Error reading worker stderr after exit: {read_err}", file=sys.stderr, flush=True)
                        self.llm_worker_process = None
                        message_emacs(f"Error: LLM worker process failed to start (exit code {self.llm_worker_process.poll()}). Check *Messages* or Emigo process buffer.")
                    return # Exit the function

                print(f"_start_llm_worker: LLM worker started (PID: {self.llm_worker_process.pid}).", file=sys.stderr, flush=True) # DEBUG + flush

                # Create and start the stdout reader thread *after* process starts
                print("_start_llm_worker: Starting stdout reader thread...", file=sys.stderr, flush=True) # DEBUG + flush
                self.llm_worker_reader_thread = threading.Thread(target=self._read_worker_stdout, name="WorkerStdoutReader", daemon=True)
                self.llm_worker_reader_thread.start()
                if not self.llm_worker_reader_thread.is_alive():
                    print("_start_llm_worker: ERROR - stdout reader thread failed to start.", file=sys.stderr, flush=True)
                    # Attempt to stop worker if it's running
                    if self.llm_worker_process and self.llm_worker_process.poll() is None:
                        self.llm_worker_process.terminate()
                        self.llm_worker_process = None
                    return

                print("_start_llm_worker: Starting stderr reader thread...", file=sys.stderr, flush=True) # DEBUG + flush
                self.llm_worker_stderr_thread = threading.Thread(target=self._read_worker_stderr, name="WorkerStderrReader", daemon=True)
                self.llm_worker_stderr_thread.start()
                if not self.llm_worker_stderr_thread.is_alive():
                    print("_start_llm_worker: ERROR - stderr reader thread failed to start.", file=sys.stderr, flush=True)
                    # Attempt cleanup
                    if self.llm_worker_process and self.llm_worker_process.poll() is None:
                        self.llm_worker_process.terminate()
                        self.llm_worker_process = None
                    return

                print("_start_llm_worker: Worker process and reader threads seem to be started.", file=sys.stderr, flush=True) # DEBUG + flush

            except Exception as e:
                print(f"_start_llm_worker: Failed to start LLM worker: {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True) # DEBUG + flush
                self.llm_worker_process = None
                # Optionally notify Emacs of the failure
                message_emacs(f"Error: Failed to start LLM worker subprocess: {e}")

    def _get_environment_details_string(self, session_path: str) -> str:
        """Delegates fetching environment details to the Session object."""
        session = self._get_or_create_session(session_path)
        if session:
            return session.get_environment_details_string()
        else:
            # Should not happen if session_path is validated earlier
            return "<environment_details>\n# Error: Could not get/create session.\n</environment_details>"

    def _stop_llm_worker(self):
        """Stops the LLM worker subprocess and reader threads."""
        with self.llm_worker_lock:
            if self.llm_worker_process:
                print("Stopping LLM worker process...", file=sys.stderr)
                if self.llm_worker_process.poll() is None: # Check if still running
                    try:
                        # Try closing stdin first to signal worker
                        if self.llm_worker_process.stdin:
                            self.llm_worker_process.stdin.close()
                    except OSError:
                        pass # Ignore errors if already closed
                    try:
                        self.llm_worker_process.terminate() # Ask nicely first
                        self.llm_worker_process.wait(timeout=2) # Wait a bit
                    except subprocess.TimeoutExpired:
                        print("LLM worker did not terminate gracefully, killing.", file=sys.stderr)
                        self.llm_worker_process.kill() # Force kill
                    except Exception as e:
                        print(f"Error stopping LLM worker: {e}", file=sys.stderr)
                        self.llm_worker_process = None # Ensure process is marked as None
                        print("LLM worker process stopped.", file=sys.stderr)

            # Signal and wait for the queue processor thread to finish
            if hasattr(self, 'worker_processor_thread') and self.worker_processor_thread and self.worker_processor_thread.is_alive():
                print("Signaling worker queue processor thread to stop...", file=sys.stderr)
                self.worker_output_queue.put(None) # Signal loop to exit
                self.worker_processor_thread.join(timeout=2) # Wait for it
                if self.worker_processor_thread.is_alive():
                    print("Warning: Worker queue processor thread did not exit cleanly.", file=sys.stderr)
                    self.worker_processor_thread = None # Mark as stopped

    def _read_worker_stdout(self):
        """Reads stdout lines from the worker and puts them in a queue."""
        while self.llm_worker_process and self.llm_worker_process.stdout:
            try:
                line = self.llm_worker_process.stdout.readline()
                if not line:
                    print("LLM worker stdout stream ended.", file=sys.stderr)
                    break # End of stream
                self.worker_output_queue.put(line.strip())
            except Exception as e:
                # Handle exceptions during read, e.g., if process dies unexpectedly
                print(f"Error reading from LLM worker stdout: {e}", file=sys.stderr)
                break
            # Signal end of output (optional)
        self.worker_output_queue.put(None)

    def _read_worker_stderr(self):
        """Reads and prints stderr lines from the worker."""
        while self.llm_worker_process and self.llm_worker_process.stderr:
            try:
                line = self.llm_worker_process.stderr.readline()
                if not line:
                    print("LLM worker stderr stream ended.", file=sys.stderr)
                    break
                # Print worker errors clearly marked
                print(f"[WORKER_STDERR] {line.strip()}", file=sys.stderr)
            except Exception as e:
                print(f"Error reading from LLM worker stderr: {e}", file=sys.stderr)
                break

    def _send_to_worker(self, data: Dict):
        """Sends a JSON message to the worker's stdin."""
        with self.llm_worker_lock:
            if not self.llm_worker_process or self.llm_worker_process.poll() is not None:
                print("Cannot send to worker, process not running. Attempting restart...", file=sys.stderr)
                self._start_llm_worker() # Try restarting
                if not self.llm_worker_process:
                    print("Worker restart failed. Cannot send message.", file=sys.stderr)
                    # Notify Emacs about the failure
                    session = data.get("session", "unknown")
                    eval_in_emacs("emigo--flush-buffer", session, "[Error: LLM worker process is not running]", "error")
                    return

            if self.llm_worker_process and self.llm_worker_process.stdin:
                try:
                    json_str = json.dumps(data)
                    # print(f"Sending to worker: {json_str}", file=sys.stderr) # Debug
                    self.llm_worker_process.stdin.write(json_str + '\n')
                    self.llm_worker_process.stdin.flush()
                except (OSError, BrokenPipeError) as e:
                    print(f"Error sending to LLM worker (Pipe probably closed): {e}", file=sys.stderr)
                    # Worker might have crashed, try restarting on next send
                    self._stop_llm_worker()
                except Exception as e:
                    print(f"Error sending to LLM worker: {e}", file=sys.stderr)
            else:
                print("Cannot send to worker, stdin not available.", file=sys.stderr)


    def _process_worker_queue(self):
        """Processes messages received from the worker via the queue."""
        while True:
            line = self.worker_output_queue.get()
            if line is None:
                print("Worker output queue processing stopped.", file=sys.stderr)
                break # Sentinel value received

            try:
                message = json.loads(line)
                msg_type = message.get("type")
                session_path = message.get("session")

                if not session_path:
                    print(f"Worker message missing session path: {message}", file=sys.stderr)
                    continue

                # print(f"Processing worker message: {message}", file=sys.stderr) # Debug

                if msg_type == "stream":
                    role = message.get("role", "llm") # Default to llm role
                    # Ensure content is never None, default to empty string
                    content = message.get("content") or ""
                    eval_in_emacs("emigo--flush-buffer", session_path, content, role)
                    # Append streamed content to history immediately
                    # Need to handle partial messages vs full assistant response
                    # Let's append only when 'finished' message arrives for simplicity now.

                elif msg_type == "tool_request":
                    request_id = message.get("request_id")
                    tool_name = message.get("tool_name")
                    params = message.get("params")
                    if request_id and tool_name and params is not None:
                        # Store request data before executing
                        self.pending_tool_requests[request_id] = message
                        # Execute the tool (this might block if sync Emacs calls are needed)
                        # Run tool execution in a separate thread to avoid blocking queue processing?
                        # For now, run directly. If Emacs calls block too long, reconsider.
                        tool_result = self._handle_tool_request_from_worker(session_path, tool_name, params)
                        # Send result back to worker
                        self._send_to_worker({
                            "type": "tool_result",
                            "request_id": request_id,
                            "result": tool_result # Send the actual result string
                        })
                        # Clean up pending request
                        if request_id in self.pending_tool_requests:
                            del self.pending_tool_requests[request_id]
                    else:
                        print(f"Invalid tool request from worker: {message}", file=sys.stderr)

                elif msg_type == "finished":
                    status = message.get("status", "unknown")
                    finish_message = message.get("message", "")
                    print(f"Worker finished interaction for {session_path}. Status: {status}. Message: {finish_message}", file=sys.stderr)

                    # Clear active session *before* processing history or signaling Emacs
                    if self.active_interaction_session == session_path:
                        self.active_interaction_session = None # Mark session as no longer active
                        print(f"Cleared active interaction flag for session: {session_path}", file=sys.stderr) # Debug

                    # Append final assistant message to history here if needed
                    # If the interaction finished successfully, update the session history
                    if status in ["success", "max_turns_reached"]:
                        final_history = message.get("final_history")
                        if final_history and isinstance(final_history, list):
                            session = self._get_or_create_session(session_path)
                            if session:
                                print(f"Updating session history for {session_path} with {len(final_history)} messages.", file=sys.stderr)
                                session.set_history(final_history) # Use the new method
                            else:
                                print(f"Error: Could not find session {session_path} to update history.", file=sys.stderr)
                        else:
                            print(f"Warning: Worker finished successfully but did not provide final history for {session_path}.", file=sys.stderr)

                    # Signal Emacs regardless of history update success
                    eval_in_emacs("emigo--agent-finished", session_path)
                    # active_interaction_session is now cleared earlier

                elif msg_type == "error":
                    error_msg = message.get("message", "Unknown error from worker")
                    print(f"Error from worker ({session_path}): {error_msg}", file=sys.stderr)
                    eval_in_emacs("emigo--flush-buffer", session_path, f"[Worker Error: {error_msg}]", "error")
                    # If an error occurs, consider the interaction finished
                    if self.active_interaction_session == session_path:
                        self.active_interaction_session = None

                elif msg_type == "get_environment_details_request":
                    request_id = message.get("request_id")
                    if request_id:
                        print(f"Worker requested environment details for {session_path}", file=sys.stderr)
                        details = self._get_environment_details_string(session_path)
                        self._send_to_worker({
                            "type": "get_environment_details_response",
                            "request_id": request_id,
                            "session": session_path, # Include session for routing if needed
                            "details": details
                        })
                    else:
                        print(f"Invalid get_environment_details_request from worker (missing request_id): {message}", file=sys.stderr)


                # Handle other message types (status, pong, etc.) if needed
            except json.JSONDecodeError:
                print(f"Received invalid JSON from worker queue: {line}", file=sys.stderr)
            except Exception as e:
                print(f"Error processing worker queue message: {e}\n{traceback.format_exc()}", file=sys.stderr)

    def _handle_tool_request_from_worker(self, session_path: str, tool_name: str, params: Dict[str, str]) -> str:
        """Handles tool execution requested by the worker process by dispatching to tools.py."""
        print(f"Handling tool request from worker: {tool_name} for {session_path}", file=sys.stderr)

        # Get the session object
        session = self._get_or_create_session(session_path)
        if not session:
            return tools._format_tool_error(f"Could not find or create session for path: {session_path}") # Use tool's formatter

        # Define tools that require explicit approval from Emacs
        # Read, List, Search, ListRepomap are generally safe. Ask/Attempt interact directly.
        # Read, List, Search are generally safe. Ask/Attempt interact directly.
        # Replace approval might be handled within Elisp if needed, but let's require it here for safety.
        require_approval_list = [
            TOOL_EXECUTE_COMMAND,
            TOOL_WRITE_TO_FILE,
        ]

        # --- Request Approval from Emacs (Synchronous) ---
        if tool_name in require_approval_list:
            try:
                # Convert params dict to a plist string for Elisp
                # Use json.dumps for robust value representation
                params_plist_str = "(" + " ".join([f":{k} {json.dumps(v)}" for k, v in params.items()]) + ")"
                print(f"Requesting approval for {tool_name} with params: {params_plist_str}", file=sys.stderr) # Debug
                is_approved = get_emacs_func_result("request-tool-approval-sync", session_path, tool_name, params_plist_str)

                if not is_approved: # Emacs function should return t or nil
                    print(f"Tool use denied by user: {tool_name}", file=sys.stderr)
                    # Return the standard denial message for the worker to handle
                    return TOOL_DENIED
            except Exception as e:
                print(f"Error requesting tool approval from Emacs: {e}\n{traceback.format_exc()}", file=sys.stderr)
                return self._format_tool_error(f"Error requesting tool approval: {e}")

        # --- Execute Approved Tool ---
        print(f"Dispatching approved tool: {tool_name}", file=sys.stderr)
        # Call the dispatcher in tools.py, passing the session object
        tool_result = tools.dispatch_tool(session, tool_name, params)

        # --- Clear Active Session on Completion ---
        # If the completion tool was called successfully, clear the active session flag *now*
        # so that new prompts aren't rejected while waiting for the worker's 'finished' message.
        if tool_name == TOOL_ATTEMPT_COMPLETION and tool_result == "COMPLETION_SIGNALLED":
            if self.active_interaction_session == session_path:
                print(f"Completion signalled for {session_path}. Clearing active session flag immediately.", file=sys.stderr)
                self.active_interaction_session = None
            else:
                # This shouldn't happen if logic is correct, but log if it does
                 print(f"Warning: Completion signalled for {session_path}, but it wasn't the active session ({self.active_interaction_session}).", file=sys.stderr)

        return tool_result

    # --- Session Management ---

    def _get_or_create_session(self, session_path: str) -> Optional[Session]:
        """Gets the Session object for a path, creating it if necessary."""
        if not os.path.isdir(session_path):
            print(f"ERROR: Invalid session path (not a directory): {session_path}", file=sys.stderr)
            # Maybe notify Emacs here?
            eval_in_emacs("message", f"[Emigo Error] Invalid session path: {session_path}")
            return None

        if session_path not in self.sessions:
            print(f"Creating new session object for: {session_path}", file=sys.stderr)
            # TODO: Get verbose setting from config
            self.sessions[session_path] = Session(session_path=session_path, verbose=True)
        return self.sessions[session_path]

    # --- EPC Methods Called by Emacs ---

    def get_chat_files(self, session_path: str) -> List[str]:
        """EPC: Returns the list of files currently in the chat context for a session."""
        session = self._get_or_create_session(session_path)
        return session.get_chat_files() if session else []

    def get_history(self, session_path: str) -> List[Tuple[float, Dict]]:
        """EPC: Retrieves the chat history as list of (timestamp, message_dict) tuples."""
        session = self._get_or_create_session(session_path)
        return session.get_history() if session else []

    def add_file_to_context(self, session_path: str, filename: str) -> bool:
        """EPC: Adds a specific file to the chat context for a session."""
        session = self._get_or_create_session(session_path)
        if not session:
            message_emacs(f"Error: Could not establish session for {session_path}")
            return False

        success, msg = session.add_file_to_context(filename)
        message_emacs(msg) # Display message (success or error) in Emacs
        return success

    def remove_file_from_context(self, session_path: str, filename: str) -> bool:
        """EPC: Removes a specific file from the chat context for a session."""
        session = self._get_or_create_session(session_path)
        if not session:
            message_emacs(f"Error: No session found for {session_path}")
            return False

        success, msg = session.remove_file_from_context(filename)
        message_emacs(msg) # Display message (success or error) in Emacs
        return success

    def emigo_send_revised_history(self, session_path: str, revised_history: List[Dict]):
        """
        EPC: Handles sending a potentially modified history back to the LLM.

        Args:
            session_path: The path identifying the session.
            revised_history: A list of message dictionaries representing the
                            new history baseline.
        """
        print(f"Received revised history for session: {session_path}", file=sys.stderr)

        if not revised_history:
            message_emacs("[Emigo Error] Received empty revised history.")
            return

        # Check for active interaction (similar to emigo_send)
        if self.active_interaction_session:
            print(f"Interaction already active for session {self.active_interaction_session}. Asking user about new prompt for {session_path}.", file=sys.stderr)
            try:
                confirm_cancel = get_emacs_func_result("yes-or-no-p",
                                                       "Agent is currently running, do you want to stop it and re-run with the revised history?")
                if confirm_cancel:
                    print(f"User confirmed cancellation of {self.active_interaction_session}. Proceeding with revised history for {session_path}.", file=sys.stderr)
                    if not self.cancel_llm_interaction(self.active_interaction_session):
                        message_emacs("[Emigo Error] Failed to cancel previous interaction.")
                        return # Stop if cancellation failed
                else:
                    print(f"User declined cancellation. Ignoring revised history for {session_path}.", file=sys.stderr)
                    eval_in_emacs("message", f"[Emigo] Agent busy with {self.active_interaction_session}. Revised history ignored.")
                    return
            except Exception as e:
                print(f"Error during confirmation/cancellation: {e}\n{traceback.format_exc()}", file=sys.stderr)
                message_emacs(f"[Emigo Error] Failed to ask for cancellation confirmation: {e}")
                return

        # Mark session as active
        self.active_interaction_session = session_path

        session = self._get_or_create_session(session_path)
        if not session:
            eval_in_emacs("emigo--flush-buffer", f"invalid-session-{session_path}", f"[Error: Invalid session path '{session_path}']", "error")
            self.active_interaction_session = None # Clear flag on error
            return

        # Convert Elisp plist format (list of lists) to Python list of dicts
        history_dicts = []
        if isinstance(revised_history, list):
            for item in revised_history:
                if isinstance(item, list) and len(item) == 4 and item[0] == ':role' and item[2] == ':content':
                    history_dicts.append({'role': item[1], 'content': item[3]})
                else:
                    print(f"Warning: Skipping invalid item in revised_history: {item}", file=sys.stderr)
        else:
             message_emacs(f"[Emigo Error] Received revised history is not a list: {type(revised_history)}")
             self.active_interaction_session = None # Clear flag on error
             return


        # Replace the session's history with the *converted* list of dicts
        print(f"Replacing history for session {session_path} with {len(history_dicts)} revised messages.", file=sys.stderr)
        session.set_history(history_dicts) # Pass the converted list

        # --- Prepare data for worker ---
        # The 'prompt' is effectively the last message in the revised history (now dicts)
        last_message_content = history_dicts[-1].get("content", "") if history_dicts else ""

        # Get current state snapshot (history is now the revised one)
        session_history = session.get_history() # This now returns the revised history
        session_chat_files = session.get_chat_files()
        environment_details_str = session.get_environment_details_string()

        # Get model config (same as emigo_send)
        vars_result = get_emacs_vars(["emigo-model", "emigo-base-url", "emigo-api-key"])
        if not vars_result or len(vars_result) < 3:
            message_emacs(f"Error retrieving Emacs variables for session {session_path}.")
            self.active_interaction_session = None
            return
        model, base_url, api_key = vars_result

        if not model:
            message_emacs(f"Please set emigo-model before starting session {session.session_path}.")
            self.active_interaction_session = None
            return

        worker_config = {
            "model": model,
            "api_key": api_key if api_key else None,
            "base_url": base_url if base_url else None,
            "verbose": session.verbose
        }

        request_data = {
            "session_path": session.session_path,
            "prompt": last_message_content, # Use last message as nominal prompt
            "history": session_history, # Pass the revised history snapshot
            "config": worker_config,
            "chat_files": session_chat_files,
            "environment_details": environment_details_str,
        }

       # --- Send request to worker ---
        print(f"Sending revised interaction request to worker for session {session.session_path}", file=sys.stderr)
        self._send_to_worker({
            "type": "interaction_request",
            "data": request_data
        })
        # Response handling happens asynchronously

    def emigo_send(self, session_path: str, prompt: str):
        """EPC: Handles a user prompt by initiating an interaction with the LLM worker."""
        print(f"Received prompt for session: {session_path}: {prompt}", file=sys.stderr)

        # Check if another interaction is already running
        if self.active_interaction_session:
            print(f"Interaction already active for session {self.active_interaction_session}. Asking user about new prompt for {session_path}.", file=sys.stderr)
            try:
                # Ask user in Emacs if they want to cancel the active session and proceed
                confirm_cancel = get_emacs_func_result("yes-or-no-p",
                                                       "Agent is currently running, do you want to stop it and re-run with your new prompt?")

                if confirm_cancel:
                    print(f"User confirmed cancellation of {self.active_interaction_session}. Proceeding with {session_path}.", file=sys.stderr)
                    # Cancel the currently active interaction. This also resets self.active_interaction_session.
                    self.cancel_llm_interaction(self.active_interaction_session)
                else:
                    # User declined, ignore the new prompt
                    print(f"User declined cancellation. Ignoring new prompt for {session_path}.", file=sys.stderr)
                    eval_in_emacs("message", f"[Emigo] Agent busy with {self.active_interaction_session}. New prompt ignored.")
                    return # Stop processing the new prompt

            except Exception as e:
                print(f"Error during confirmation/cancellation: {e}\n{traceback.format_exc()}", file=sys.stderr)
                message_emacs(f"[Emigo Error] Failed to ask for cancellation confirmation: {e}")
                return # Stop processing on error

        # If we reach here, either no interaction was active, or the user confirmed cancellation.
        # Mark the *new* session as active.
        self.active_interaction_session = session_path

        # Get or create the session object
        session = self._get_or_create_session(session_path)
        if not session:
            # Error already logged by _get_or_create_session
            eval_in_emacs("emigo--flush-buffer", f"invalid-session-{session_path}", f"[Error: Invalid session path '{session_path}']", "error")
            return

        # Flush the user prompt to the Emacs buffer first
        eval_in_emacs("emigo--flush-buffer", session.session_path, f"\n\nUser:\n{prompt}\n", "user")
        # Append user prompt dictionary to the session's history
        session.append_history({"role": "user", "content": prompt})

        # --- Handle File Mentions (@file) ---
        mention_pattern = r'@(\S+)'
        mentioned_files_in_prompt = re.findall(mention_pattern, prompt)
        # Use the session object's method to add files
        if mentioned_files_in_prompt:
            print(f"Found file mentions in prompt: {mentioned_files_in_prompt}", file=sys.stderr)
            for file in mentioned_files_in_prompt:
                success, msg = session.add_file_to_context(file)
                if success:
                    message_emacs(msg) # Notify Emacs only on successful add

        # --- Prepare data for worker ---
        # Get current state snapshot from the session object
        session_history = session.get_history()
        session_chat_files = session.get_chat_files()
        # Generate environment details string using the session object
        environment_details_str = session.get_environment_details_string()

        # Get model config from Emacs vars
        vars_result = get_emacs_vars(["emigo-model", "emigo-base-url", "emigo-api-key"])
        if not vars_result or len(vars_result) < 3:
            message_emacs(f"Error retrieving Emacs variables for session {session_path}.")
            self.active_interaction_session = None # Unset active session
            return
        model, base_url, api_key = vars_result

        if not model:
            message_emacs(f"Please set emigo-model before starting session {session.session_path}.")
            self.active_interaction_session = None # Unset active session
            return

        worker_config = {
            "model": model,
            "api_key": api_key if api_key else None,
            "base_url": base_url if base_url else None,
            "verbose": session.verbose # Use session's verbose setting
        }

        # Prepare the state snapshot for the worker
        request_data = {
            "session_path": session.session_path, # Use absolute path from session
            "prompt": prompt, # Still useful for context, though history is primary
            "history": session_history, # Pass history snapshot
            "config": worker_config,
            "chat_files": session_chat_files, # Pass chat files snapshot
            "environment_details": environment_details_str, # Pass generated details
        }

        # --- Send request to worker ---
        print(f"Sending interaction request to worker for session {session.session_path}", file=sys.stderr)
        self._send_to_worker({
            "type": "interaction_request",
            "data": request_data
        })
        # The response handling happens asynchronously in _process_worker_queue

    def cancel_llm_interaction(self, session_path: str):
        """Cancels the current LLM interaction by killing and restarting the worker."""
        print(f"Received request to cancel interaction for session: {session_path}", file=sys.stderr)
        # Check if the cancellation request is for the currently active session
        if self.active_interaction_session != session_path:
            message_emacs(f"No active interaction found for session {session_path} to cancel.")
            return

        print("Stopping and restarting LLM worker due to cancellation request...", file=sys.stderr)
        self._stop_llm_worker()

        # Drain the queue to discard messages from the stopped worker
        print("Draining worker output queue...", file=sys.stderr)
        drained_count = 0
        while not self.worker_output_queue.empty():
            try:
                stale_msg = self.worker_output_queue.get_nowait()
                # print(f"Discarding stale message: {stale_msg}", file=sys.stderr) # Optional: very verbose
                drained_count += 1
            except queue.Empty:
                break
            except Exception as e:
                print(f"Error draining queue: {e}", file=sys.stderr)
                break # Stop draining on error
            print(f"Worker output queue drained ({drained_count} messages discarded).", file=sys.stderr)

        self._start_llm_worker()
        # Check if worker restart was successful before proceeding
        worker_restarted_ok = False
        with self.llm_worker_lock:
            if self.llm_worker_process and self.llm_worker_process.poll() is None:
                worker_restarted_ok = True

        if not worker_restarted_ok:
            print("ERROR: Failed to restart LLM worker after cancellation.", file=sys.stderr)
            message_emacs("[Emigo Error] Failed to restart LLM worker after cancellation.")
            # Clear active session state even on failure
            self.active_interaction_session = None
            self.pending_tool_requests.clear()
            return False # Indicate failure

        print("LLM worker restarted successfully.", file=sys.stderr)

        # --- Restart the worker queue processor thread ---
        print("Restarting worker queue processor thread...", file=sys.stderr)
        self.worker_processor_thread = threading.Thread(target=self._process_worker_queue, name="WorkerQueueProcessorThread", daemon=True)
        self.worker_processor_thread.start()
        if not self.worker_processor_thread.is_alive():
            print("ERROR: Failed to restart worker queue processor thread.", file=sys.stderr)
            message_emacs("[Emigo Error] Failed to restart worker queue processor thread.")
            # Stop the worker again if the processor fails
            self._stop_llm_worker()
            self.active_interaction_session = None
            self.pending_tool_requests.clear()
            return False # Indicate failure
        print("Worker queue processor thread restarted.", file=sys.stderr)
        # --- End restart queue processor ---


        # Remove the last user message (the cancelled prompt) from history
        session = self.sessions.get(session_path)
        if session and session.history:
            # History is stored as (timestamp, message_dict)
            last_timestamp, last_message = session.history[-1]
            if last_message.get("role") == "user":
                print(f"Removing cancelled user prompt from history for {session_path}", file=sys.stderr)
                session.history.pop()
            else:
                print(f"Warning: Last message in history for cancelled session {session_path} was not from user.", file=sys.stderr)

        # Clear active session state
        self.active_interaction_session = None
        # Clear any pending tool requests that belonged to the killed worker's task
        self.pending_tool_requests.clear()

        # Invalidate the cache for the cancelled session to ensure fresh context next time
        if session:
            print(f"Invalidating cache for cancelled session: {session_path}", file=sys.stderr)
            session.invalidate_cache()
        else:
            print(f"Warning: Could not find session {session_path} to invalidate cache after cancellation.", file=sys.stderr)

        # Notify Emacs buffer
        eval_in_emacs("emigo--flush-buffer", session_path, "\n[Interaction cancelled by user.]\n", "warning")
        return True # Indicate success

    def cleanup(self):
        """Do some cleanup before exit python process."""
        print("Running Emigo cleanup...", file=sys.stderr)
        self._stop_llm_worker()
        close_epc_client()
        print("Emigo cleanup finished.", file=sys.stderr)

    def clear_history(self, session_path: str) -> bool:
        """EPC: Clear the chat history for the given session path."""
        print(f"Clearing history for session: {session_path}", file=sys.stderr)
        session = self._get_or_create_session(session_path)
        if session:
            session.clear_history()
            # Also clear local buffer via Emacs side
            eval_in_emacs("emigo--clear-local-buffer", session.session_path)
            message_emacs(f"Cleared history for session: {session.session_path}")
            return True
        else:
            message_emacs(f"No session found to clear history for: {session_path}")
            return False


if __name__ == "__main__":
    print("emigo.py starting execution...", file=sys.stderr, flush=True) # DEBUG + flush
    if len(sys.argv) < 2:
        print("ERROR: Missing EPC server port argument.", file=sys.stderr, flush=True)
        sys.exit(1)
    try:
        print("Initializing Emigo class...", file=sys.stderr, flush=True) # DEBUG + flush
        emigo = Emigo(sys.argv[1:])
        print("Emigo class initialized.", file=sys.stderr, flush=True) # DEBUG + flush

        # Keep the main thread alive. Instead of joining the server thread (which might exit),
        # just wait indefinitely or until interrupted.
        print("Main thread entering wait loop (Ctrl+C to exit)...", file=sys.stderr, flush=True) # DEBUG + flush
        while True:
            time.sleep(3600) # Sleep for a long time, wake up periodically if needed

    except KeyboardInterrupt:
        print("\nKeyboardInterrupt received, cleaning up...", file=sys.stderr, flush=True)
        if 'emigo' in locals() and emigo:
            emigo.cleanup()
    except Exception as e:
        print(f"\nFATAL ERROR in main execution block: {e}", file=sys.stderr, flush=True)
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        # Attempt cleanup even on fatal error
        if 'emigo' in locals() and emigo:
            try:
                emigo.cleanup()
            except Exception as cleanup_err:
                print(f"Error during cleanup: {cleanup_err}", file=sys.stderr, flush=True)
                sys.exit(1) # Exit with error code
    finally:
        print("emigo.py main execution finished.", file=sys.stderr, flush=True) # DEBUG + flush
