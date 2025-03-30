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
        self.llm_client_dict = {}
        self.project_chat_files = {} # Tracks files in context per project_path
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

    def emigo(self, filename, prompt):
        project_path = get_project_path(filename)
        if isinstance(project_path, str):
            self.emigo_project(project_path, prompt)
        else:
            print("EMIGO ERROR: parse project path of '{}' failed".format(filename))

    def emigo_project(self, project_path, prompt):
        eval_in_emacs("emigo-create-ai-window", project_path)

        # First print the prompt to buffer
        eval_in_emacs("emigo-flush-ai-buffer", project_path, "\n\n{}\n\n".format(prompt), "user")

        # --- Manage Chat Files ---
        if project_path not in self.project_chat_files:
            self.project_chat_files[project_path] = [] # Initialize if new project

        current_chat_files = self.project_chat_files[project_path]
        mentioned_in_prompt = self._extract_and_validate_mentions(project_path, prompt)
        newly_added_files = []
        for fname in mentioned_in_prompt:
            if fname not in current_chat_files:
                current_chat_files.append(fname)
                newly_added_files.append(fname)

        if newly_added_files:
             message_emacs(f"Added mentioned files to context: {', '.join(newly_added_files)}")
        # --- End Manage Chat Files ---


        if project_path in self.llm_client_dict:
            # Subsequent message: Update history and send
            thread = threading.Thread(target=lambda: self.send_llm_message(project_path, prompt, current_chat_files))
            thread.start()
            self.thread_queue.append(thread)
        else:
            # First message: Start client and send
            thread = threading.Thread(target=lambda: self.start_llm_client(project_path, prompt, current_chat_files))
            thread.start()
            self.thread_queue.append(thread)

    def _extract_and_validate_mentions(self, project_path, text):
        """Extracts @file mentions and validates they exist."""
        validated_files = []
        pattern = r'@(\S+)' # Find @ followed by non-whitespace characters
        matches = re.findall(pattern, text)
        if matches:
            # print(f"Found potential @-mentions: {matches}", file=sys.stderr) # Optional debug
            for potential_file in matches:
                # Strip trailing punctuation that might be attached
                potential_file = potential_file.rstrip('.,;:!?')
                # Resolve relative to project_path
                abs_path = os.path.abspath(os.path.join(project_path, potential_file))
                # Check if it exists *within* the project dir and is a file
                if os.path.commonpath([project_path, abs_path]) == project_path and os.path.isfile(abs_path):
                    # Store the relative path as used in the mention
                    validated_files.append(potential_file)
                # else: # Optional debug
                #     print(f"  Ignoring mention '{potential_file}': File not found or not a file at {abs_path}", file=sys.stderr)
        return validated_files

    def _parse_llm_for_file_requests(self, project_path, response_text):
        """
        Parses the LLM response to check if it's requesting files to be added.
        Returns a list of requested file paths if found, otherwise None.
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
                        # Basic validation: check if it's within the project
                        abs_path = os.path.abspath(os.path.join(project_path, file_path))
                        if os.path.commonpath([project_path, abs_path]) == project_path and os.path.isfile(abs_path):
                             requested_files.append(file_path)
                        else:
                             print(f"Warning: LLM requested invalid or non-existent file: {file_path}", file=sys.stderr)
                             # Optionally inform the user via Emacs message
                             # eval_in_emacs("message", f"Emigo: LLM requested invalid file '{file_path}', ignoring.")

                if requested_files:
                    return requested_files
            except IndexError:
                # files_marker wasn't found after action_marker
                print("Warning: LLM response contained 'Action: add_files_to_context' but no 'Files:' section.", file=sys.stderr)
            except Exception as e:
                print(f"Error parsing LLM file request: {e}", file=sys.stderr)

        return None # No valid file request found

    def _execute_llm_interaction_loop(self, project_path, client, initial_user_prompt=None):
        """
        Handles the core interaction loop with the LLM, including automatic file adding.
        """
        verbose = True # Or get from config/client
        no_shell = True # Or get from config/client
        map_tokens = 4096 # Or get from config/client
        tokenizer = "cl100k_base" # Or get from config/client
        max_retries = 3 # Limit retries for adding files to prevent infinite loops
        current_user_prompt = initial_user_prompt # Keep track of the prompt for this interaction

        for attempt in range(max_retries):
            current_chat_files = self.project_chat_files.get(project_path, [])
            print(f"\n--- LLM Interaction Loop (Attempt {attempt + 1}/{max_retries}) ---", file=sys.stderr)
            print(f"Current chat files: {current_chat_files}", file=sys.stderr)

            # --- 1. Build Prompt ---
            try:
                # Pass the specific user prompt for this turn to the builder.
                # The history passed to build_prompt_messages does NOT include this prompt yet.
                builder = PromptBuilder(
                    root_dir=project_path,
                    user_message=current_user_prompt, # Use the prompt for this specific interaction
                    chat_files=current_chat_files,
                    read_only_files=[], # Load if needed
                    map_tokens=map_tokens,
                    tokenizer=tokenizer,
                    verbose=verbose,
                    no_shell=no_shell,
                )
                messages = builder.build_prompt_messages(current_history=client.get_history())

            except Exception as e:
                print(f"Error building prompt in loop: {e}", file=sys.stderr)
                eval_in_emacs("emigo-flush-ai-buffer", project_path, f"[Error building prompt: {e}]", "error")
                return # Exit loop on build error

            # --- 2. Send to LLM (Streaming) ---
            full_response = ""
            eval_in_emacs("emigo-flush-ai-buffer", project_path, "\nAssistant:\n", "llm") # Add header before streaming
            try:
                response_stream = client.send(messages, stream=True)
                for chunk in response_stream:
                    eval_in_emacs("emigo-flush-ai-buffer", project_path, chunk, "llm")
                    full_response += chunk
                # print() # Ensure a newline in terminal if needed, Emacs buffer handles it

            except Exception as e:
                print(f"\nError during LLM communication in loop: {e}", file=sys.stderr)
                error_message = f"[Error during LLM communication: {e}]"
                eval_in_emacs("emigo-flush-ai-buffer", project_path, error_message, "error")
                # Add the user prompt and error message to history before returning
                if initial_user_prompt:
                    client.append_history({"role": "user", "content": initial_user_prompt})
                client.append_history({"role": "assistant", "content": error_message})
                return # Exit loop on communication error

            # --- 3. Parse Full Response (Post-Streaming) for File Requests ---
            requested_files = self._parse_llm_for_file_requests(project_path, full_response)

            if requested_files:
                print(f"LLM requested files: {requested_files}", file=sys.stderr)
                newly_added_files = []
                current_chat_files_set = set(current_chat_files)
                for fname in requested_files:
                    if fname not in current_chat_files_set:
                        current_chat_files.append(fname)
                        newly_added_files.append(fname)
                        current_chat_files_set.add(fname) # Keep set updated

                if newly_added_files:
                    self.project_chat_files[project_path] = current_chat_files # Update the main list
                    added_files_str = ', '.join(newly_added_files)
                    message_emacs(f"LLM requested files. Added to context: {added_files_str}")
                    print(f"Added files to context: {added_files_str}", file=sys.stderr)

                    # Add LLM's request to history
                    client.append_history({"role": "assistant", "content": full_response})

                    # Prepare the system message for the next loop iteration
                    system_follow_up = f"Files in Context: {self.project_chat_files}. Please proceed with the original request."
                    # Set this as the 'user prompt' for the *next* iteration
                    current_user_prompt = system_follow_up
                    # Continue to the next iteration of the loop
                    continue
                else:
                    # LLM requested files, but they were already in context.
                    # This might indicate a loop or misunderstanding. Break and show response.
                    print("Warning: LLM requested files that are already in context. Breaking loop.", file=sys.stderr)
                    # LLM requested files, but they were already in context.
                    # This might indicate a loop or misunderstanding.
                    # Add the assistant's response (which requested files again) to history.
                    client.append_history({"role": "assistant", "content": full_response})
                    # The response was already streamed. Break the loop.
                    print("Warning: LLM requested files that are already in context. Breaking loop.", file=sys.stderr)
                    break

            else:
                # No file request detected, this is the final response for this interaction.
                print("LLM did not request files. Finalizing interaction.", file=sys.stderr)

                # Add the original user prompt that started this interaction to history
                if initial_user_prompt: # Ensure we have the initial prompt
                     client.append_history({"role": "user", "content": initial_user_prompt})
                     # We don't need to clear initial_user_prompt as the loop is ending

                # Add the final assistant response (already streamed) to history
                client.append_history({"role": "assistant", "content": full_response})

                # The response was already streamed to Emacs.
                break # Exit loop

        else:
            # Loop finished due to max_retries
            print(f"Error: Exceeded max retries ({max_retries}) for adding files.", file=sys.stderr)
            error_message = f"[Error: Exceeded max retries ({max_retries}) for adding files. Check LLM response.]"
            eval_in_emacs("emigo-flush-ai-buffer", project_path, error_message, "error")
            # The last response (which likely requested files again) was already streamed.
            # Add the user prompt (if available) and the final assistant response to history.
            if initial_user_prompt:
                 client.append_history({"role": "user", "content": initial_user_prompt})
            if full_response: # Add the last assistant response before giving up
                 client.append_history({"role": "assistant", "content": full_response})


    def send_llm_message(self, project_path, prompt, chat_files):
        """Sends a subsequent message, triggering the interaction loop."""
        if project_path in self.llm_client_dict:
            client = self.llm_client_dict[project_path]
            # Pass the new user prompt to the interaction loop.
            # The loop itself will handle adding it to history at the right time.
            self._execute_llm_interaction_loop(project_path, client, initial_user_prompt=prompt)
        else:
            print(f"EMIGO ERROR: LLM client not found for project path {project_path} in send_llm_message.")


    def start_llm_client(self, project_path, prompt, chat_files):
        """Starts a new LLM client and triggers the interaction loop."""
        verbose = True # Or get from config
        # --- Get Model Config ---
        [model, base_url, api_key] = get_emacs_vars(["emigo-model", "emigo-base-url", "emigo-api-key"])
        if not model: # Check only essential model name
            message_emacs("Please set emigo-model before calling emigo.")
            return

        # --- Initialize Client ---
        client = LLMClient(
            model_name=model,
            api_key=api_key if api_key else None, # Pass None if empty string
            base_url=base_url if base_url else None, # Pass None if empty string
            verbose=verbose,
        )
        self.llm_client_dict[project_path] = client
        self.project_chat_files[project_path] = chat_files # Ensure chat_files are stored

        # --- Start Interaction Loop ---
        # Pass the initial prompt to the loop function
        self._execute_llm_interaction_loop(project_path, client, initial_user_prompt=prompt)


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
