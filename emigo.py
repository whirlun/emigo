#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (C) 2022 Andy Stewart
#
# Author:     Andy Stewart <lazycat.manatee@gmail.com>
# Maintainer: Andy Stewart <lazycat.manatee@gmail.com>
#
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

from epc.server import ThreadingEPCServer
from llm import LLMClient
from prompt_builder import PromptBuilder
from utils import *
import json
import os
import re
import sys
import threading
import traceback

class Emigo:
    def __init__(self, args):
        # Init EPC client port.
        init_epc_client(int(args[0]))

        # Init vars.
        self.llm_client_dict = {} # Key: session_path
        self.chat_files = {} # Key: session_path, Value: list of relative file paths
        self.thread_queue = []

        # Build EPC server.
        self.server = ThreadingEPCServer(('127.0.0.1', 0), log_traceback=True)
        # self.server.logger.setLevel(logging.DEBUG)
        self.server.allow_reuse_address = True

        # ch = logging.FileHandler(filename=os.path.join(emigo_config_dir, 'epc_log.txt'), mode='w')
        # formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(lineno)04d | %(message)s')
        # ch.setFormatter(formatter)
        # ch.setLevel(logging.DEBUG)
        # self.server.logger.addHandler(ch)
        # self.server.logger = logger

        self.server.register_instance(self)  # register instance functions let elisp side call
        # Register functions callable from Elisp. Note the first arg is implicitly session_path.
        self.server.register_function(self.emigo_session)
        self.server.register_function(self.get_chat_files)
        self.server.register_function(self.remove_file_from_context)

        # Start EPC server with sub-thread, avoid block Qt main loop.
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()

        # All Emacs request running in event_loop.
        # self.event_queue = queue.Queue()
        # self.event_loop = threading.Thread(target=self.event_dispatcher)
        # self.event_loop.start()

        # Pass epc port and webengine codec information to Emacs when first start emigo.
        eval_in_emacs('emigo--first-start', self.server.server_address[1])

        # event_loop never exit, simulation event loop.
        # self.event_loop.join()
        self.server_thread.join()

    def event_dispatcher(self):
        try:
            while True:
                message = self.event_queue.get(True)
                print("**** ", message)
                self.event_queue.task_done()
        except:
            logger.error(traceback.format_exc())

    def get_chat_files(self, session_path):
        """Returns the list of files currently in the chat context for a session."""
        return self.chat_files.get(session_path, [])

    def remove_file_from_context(self, session_path, filename):
        """Removes a specific file from the chat context for a session."""
        if session_path in self.chat_files:
            if filename in self.chat_files[session_path]:
                self.chat_files[session_path].remove(filename)
                message_emacs(f"Removed '{filename}' from chat context for session: {os.path.basename(session_path)}")
                return True
            else:
                message_emacs(f"File '{filename}' not found in chat context for session: {os.path.basename(session_path)}")
                return False
        else:
            message_emacs(f"No chat context found for session: {os.path.basename(session_path)}")
            return False

    # Removed emigo(filename, prompt) as entry point is now emigo_session

    def emigo_session(self, session_path, prompt):
        """Handles a prompt for a specific session path."""
        print(f"Starting session with path: {session_path}", file=sys.stderr)
        # First print the prompt to buffer
        eval_in_emacs("emigo-flush-buffer", session_path, "\n\nUser:\n{}\n\n".format(prompt), "user")

        # --- Add mentioned files to context ---
        # Use session_path for validation
        mentioned_files = self._extract_and_validate_mentions(session_path, prompt)
        if mentioned_files:
            print(f"Found file mentions in prompt: {mentioned_files}", file=sys.stderr)
        # --- End Add mentioned files ---

        if session_path in self.llm_client_dict:
            # Subsequent message: Update history and send
            thread = threading.Thread(target=lambda: self.send_llm_message(session_path, prompt))
            thread.start()
            self.thread_queue.append(thread)
        else:
            # First message: Start client and send
            thread = threading.Thread(target=lambda: self.start_llm_client(session_path, prompt))
            thread.start()
            self.thread_queue.append(thread)

    def _extract_and_validate_mentions(self, session_path, text):
        """Extracts @file mentions and validates they exist relative to session_path."""
        # Validate session_path first
        if not session_path or not os.path.isdir(session_path):
            print(f"ERROR: Invalid session path: {session_path}", file=sys.stderr)
            return []

        pattern = r'@(\S+)' # Find @ followed by non-whitespace characters
        matches = re.findall(pattern, text)

        # Use add_files_to_context to handle validation and adding
        return self.add_files_to_context(session_path, matches)

    def _parse_llm_for_file_requests(self, session_path, response_text):
        """
        Parses the LLM response for file requests.
        Returns a list of file paths if found, otherwise None.
        Let add_files_to_context handle validation.
        """
        # Look for the specific action phrase and file list structure
        action_marker = "Action: add_files_to_context"
        files_marker = "Files:"
        if action_marker in response_text:
            try:
                # Find the start of the file list
                files_section = response_text.split(files_marker, 1)[1]
                requested_files = []
                # Extract file paths, stripping whitespace and ignoring empty lines
                for line in files_section.strip().splitlines():
                    file_path = line.strip()
                    if file_path:
                        requested_files.append(file_path)

                if requested_files:
                    return requested_files
            except IndexError:
                # files_marker wasn't found after action_marker
                print("Warning: LLM response contained 'Action: add_files_to_context' but no 'Files:' section.", file=sys.stderr)
            except Exception as e:
                print(f"Error parsing LLM file request: {e}", file=sys.stderr)

        return None # No file request found

    def _execute_llm_interaction_loop(self, session_path, client, initial_user_prompt=None):
        """
        Handles the core interaction loop for a session, including automatic file adding.

        Uses session_path for both context tracking and file operations.

        This method implements a key innovation that allows the LLM to request additional
        files during a single user interaction. The flow is:

        1. User sends initial prompt (may include some @mentioned files)
        2. LLM receives prompt with current context (repo map + mentioned files)
        3. LLM may respond with:
           - A final answer (loop ends)
           - A request for more files in format:
             "Action: add_files_to_context\nFiles:\nfile1\nfile2"
        4. If files are requested:
           - System validates and adds files to context
           - Loop repeats with same user prompt but expanded context
           - Max retries (3) prevents infinite loops

        This solves the "context gap" problem where LLM needs more files than initially
        provided to properly answer, without requiring manual user intervention between
        the request and final response.

        Args:
            session_path: Identifier for the current session context.
            client: LLMClient instance for this session.
            initial_user_prompt: The original user message that started this interaction.
        """
        verbose = True # Or get from config/client
        no_shell = True # Or get from config/client
        map_tokens = 4096 # Or get from config/client
        tokenizer = "cl100k_base" # Or get from config/client
        max_retries = 3 # Limit retries for adding files to prevent infinite loops
        current_user_prompt = initial_user_prompt # Keep track of the prompt for this interaction loop

        for attempt in range(max_retries):
            # Get the current list of chat files for this session
            chat_files = self.chat_files.get(session_path, [])

            print(f"\n--- LLM Interaction Loop (Session: {os.path.basename(session_path)}, Attempt {attempt + 1}/{max_retries}) ---", file=sys.stderr)
            print(f"Session Path: {session_path}", file=sys.stderr)
            print(f"Using chat files: {chat_files}", file=sys.stderr)

            # --- 1. Build Prompt ---
            try:
                # PromptBuilder needs the actual project root for repo mapping and file access.
                # History comes from the client associated with the session_path.
                builder = PromptBuilder(
                    root_dir=session_path, # Use session_path as root
                    user_message=current_user_prompt,
                    chat_files=chat_files,
                    read_only_files=[], # TODO: Implement if needed
                    map_tokens=map_tokens,
                    tokenizer=tokenizer,
                    verbose=verbose,
                    no_shell=no_shell,
                )
                messages = builder.build_prompt_messages(current_history=client.get_history())

            except Exception as e:
                print(f"Error building prompt in loop (Session: {os.path.basename(session_path)}): {e}", file=sys.stderr)
                eval_in_emacs("emigo-flush-buffer", session_path, f"[Error building prompt: {e}]", "error")
                return # Exit loop on build error

            # --- 2. Send to LLM (Streaming) ---
            full_response = ""
            # Flush to the correct session buffer
            eval_in_emacs("emigo-flush-buffer", session_path, "\nAssistant:\n", "llm")
            try:
                response_stream = client.send(messages, stream=True)
                for chunk in response_stream:
                    # Flush chunks to the correct session buffer
                    eval_in_emacs("emigo-flush-buffer", session_path, chunk, "llm")
                    full_response += chunk
                # print() # Ensure a newline in terminal if needed

            except Exception as e:
                print(f"\nError during LLM communication in loop (Session: {os.path.basename(session_path)}): {e}", file=sys.stderr)
                error_message = f"[Error during LLM communication: {e}]"
                # Flush error to the correct session buffer
                eval_in_emacs("emigo-flush-buffer", session_path, error_message, "error")
                # Add the user prompt and error message to history before returning
                if current_user_prompt: # Use the prompt for this loop iteration
                    client.append_history({"role": "user", "content": current_user_prompt})
                client.append_history({"role": "assistant", "content": error_message})
                return # Exit loop on communication error

            # --- 3. Parse Full Response for File Requests ---
            # Validate requested files against the session_path
            requested_files = self._parse_llm_for_file_requests(session_path, full_response)

            if requested_files:
                print(f"LLM requested files for session {os.path.basename(session_path)}: {requested_files}", file=sys.stderr)
                # Add files to the specific session_path context
                newly_added = self.add_files_to_context(session_path, requested_files)

                if newly_added:
                    # Files were successfully added, continue the loop with the *same* user prompt
                    # current_user_prompt remains initial_user_prompt
                    print(f"Added {newly_added}, continuing loop for session {os.path.basename(session_path)}.", file=sys.stderr)
                    continue
                else:
                    # LLM requested files, but they were already in context or invalid.
                    # This might indicate a loop or misunderstanding.
                    # Add the assistant's response (which requested files again/invalid files) to history.
                    client.append_history({"role": "assistant", "content": full_response})
                    # The response was already streamed. Break the loop.
                    print(f"Warning: LLM requested files already in context or invalid for session {os.path.basename(session_path)}. Breaking loop.", file=sys.stderr)
                    break # Exit loop

            else:
                # No file request detected, this is the final response for this interaction.
                print(f"LLM did not request files for session {os.path.basename(session_path)}. Finalizing interaction.", file=sys.stderr)

                # Add the user prompt for this interaction to history
                if current_user_prompt:
                     client.append_history({"role": "user", "content": current_user_prompt})

                # Add the final assistant response (already streamed) to history
                client.append_history({"role": "assistant", "content": full_response})

                # The response was already streamed to Emacs.
                break # Exit loop

        else:
            # Loop finished due to max_retries
            print(f"Error: Exceeded max retries ({max_retries}) for adding files in session {os.path.basename(session_path)}.", file=sys.stderr)
            error_message = f"[Error: Exceeded max retries ({max_retries}) for adding files. Check LLM response.]"
            eval_in_emacs("emigo-flush-buffer", session_path, error_message, "error")
            # The last response (which likely requested files again) was already streamed.
            # Add the user prompt and the final assistant response to history.
            if current_user_prompt:
                 client.append_history({"role": "user", "content": current_user_prompt})
            if full_response: # Add the last assistant response before giving up
                 client.append_history({"role": "assistant", "content": full_response})


    def send_llm_message(self, session_path, prompt):
        """Sends a subsequent message for a session, triggering the interaction loop."""
        if session_path in self.llm_client_dict:
            client = self.llm_client_dict[session_path]
            # Call the interaction loop. It will fetch context internally using session_path.
            self._execute_llm_interaction_loop(session_path, client, initial_user_prompt=prompt)
        else:
            print(f"EMIGO ERROR: LLM client not found for session path {session_path} in send_llm_message.")
            eval_in_emacs("emigo-flush-buffer", session_path, "[Internal Error: LLM Client not found]", "error")


    def add_files_to_context(self, session_path, files_to_add):
        """
        Adds a list of files to the chat context for a given session.

        Handles validation against session_path, prevents duplicates,
        updates self.chat_files[session_path], and notifies Emacs.

        Args:
            session_path: The identifier and root path for the session context.
            files_to_add: A list of relative file paths to potentially add.

        Returns:
            A list of the relative file paths that were newly added to the context.
        """
        if not files_to_add:
            return []

        # Ensure the session list exists in chat_files
        chat_files_list = self.chat_files.setdefault(session_path, [])
        chat_files_set = set(chat_files_list) # Use set for efficient checking
        newly_added = []

        for file_rel_path in files_to_add:
            if file_rel_path in chat_files_set:
                # print(f"Debug: File '{file_rel_path}' already in context for session {os.path.basename(session_path)}.", file=sys.stderr)
                continue # Skip duplicates

            # Validate against session_path
            abs_path = os.path.abspath(os.path.join(session_path, file_rel_path))

            if os.path.isfile(abs_path):
                chat_files_list.append(file_rel_path) # Add relative path to the list
                chat_files_set.add(file_rel_path)   # Add relative path to the set
                newly_added.append(file_rel_path)

        if newly_added:
            added_files_str = ', '.join(newly_added)
            message_emacs(f"Added files to context for session {os.path.basename(session_path)}: {added_files_str}")
            print(f"Added files to context for session {os.path.basename(session_path)}: {added_files_str}", file=sys.stderr)
            # Update the main chat_files dictionary
            self.chat_files[session_path] = chat_files_list

        return newly_added # Return list of newly added relative paths

    def start_llm_client(self, session_path, prompt):
        """Starts a new LLM client for a session and triggers the interaction loop."""
        verbose = True # Or get from config
        # --- Get Model Config ---
        # Use get_emacs_vars which handles boolean conversion correctly
        vars_result = get_emacs_vars(["emigo-model", "emigo-base-url", "emigo-api-key"])
        if not vars_result or len(vars_result) < 3:
             message_emacs("Error retrieving Emacs variables.")
             return
        model, base_url, api_key = vars_result

        if not model: # Check only essential model name
            message_emacs("Please set emigo-model before calling emigo.")
            return

        # --- Initialize Client ---
        print(f"Starting LLM Client for session: {os.path.basename(session_path)} (Path: {session_path})", file=sys.stderr)
        client = LLMClient(
            model_name=model,
            api_key=api_key if api_key else None, # Pass None if empty string
            base_url=base_url if base_url else None,
            verbose=verbose,
        )
        self.llm_client_dict[session_path] = client

        # --- Initialize chat files for this session ---
        # Mentions from the first prompt were already added by emigo_session calling add_files_to_context
        self.chat_files.setdefault(session_path, []) # Ensure list exists

        # --- Start Interaction Loop ---
        # Pass session_path to the loop.
        self._execute_llm_interaction_loop(session_path, client, initial_user_prompt=prompt)


    def cleanup(self):
        """Do some cleanup before exit python process."""
        close_epc_client()

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        import cProfile
        profiler = cProfile.Profile()
        profiler.run("Emigo(sys.argv[1:])")
    else:
        Emigo(sys.argv[1:])
