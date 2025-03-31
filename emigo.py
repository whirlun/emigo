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

import os
import sys
import threading
import traceback
from typing import Dict, List, Optional # Added Optional

from epc.server import ThreadingEPCServer
from llm import LLMClient
from agents import Agents
from utils import *
import re

class Emigo:
    def __init__(self, args):
        # Init EPC client port.
        init_epc_client(int(args[0]))

        # Init vars.
        self.agent_dict: Dict[str, Agents] = {} # Key: session_path, Value: Agents instance
        self.chat_files: Dict[str, List[str]] = {} # Key: session_path, Value: list of relative file paths

        # Build EPC server.
        self.server = ThreadingEPCServer(('127.0.0.1', 0), log_traceback=True)
        # self.server.logger.setLevel(logging.DEBUG)
        self.server.allow_reuse_address = True

        # ch = logging.FileHandler(filename=os.path.join(emigo_config_dir, 'epc_log.txt'), mode='w')
        # formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(lineno)04d | %(message)s')
        # ch.setFormatter(formatter)
        # ch.setLevel(logging.DEBUG)
        # self.server.logger.addHandler(ch)
        # self.server.logger = logger # Keep logging setup if needed

        self.server.register_instance(self)  # register instance functions let elisp side call

        # Start EPC server with sub-thread.
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()

        # All Emacs request running in event_loop.
        # self.event_queue = queue.Queue()
        # Removed event_loop setup

        # Pass epc port to Emacs when first start emigo.
        eval_in_emacs('emigo--first-start', self.server.server_address[1])
        self.server_thread.join()

    def event_dispatcher(self):
        try:
            while True:
                message = self.event_queue.get(True)
                print("**** ", message)
                self.event_queue.task_done()
        except Exception as e:
            # Use standard logging if configured, otherwise print
            print(f"Error in event dispatcher (should not happen): {e}\n{traceback.format_exc()}", file=sys.stderr)

    def get_chat_files(self, session_path: str) -> List[str]:
        """Returns the list of files currently in the chat context for a session."""
        return self.chat_files.get(session_path, [])

    def remove_file_from_context(self, session_path: str, filename: str) -> bool:
        """Removes a specific file from the chat context for a session."""
        if session_path in self.chat_files:
            # Ensure filename is relative for comparison
            # Handle potential absolute path input from Emacs
            if os.path.isabs(filename):
                 try:
                     rel_filename = os.path.relpath(filename, session_path)
                 except ValueError: # filename might be on a different drive on Windows
                     message_emacs(f"Cannot remove file from different drive: {filename}")
                     return False
            else:
                 rel_filename = filename # Assume it's already relative

            if rel_filename in self.chat_files[session_path]:
                self.chat_files[session_path].remove(rel_filename)
                message_emacs(f"Removed '{rel_filename}' from chat context for session: {session_path}")
                return True
            else:
                message_emacs(f"File '{rel_filename}' not found in chat context for session: {session_path}")
                return False
        else:
            message_emacs(f"No chat context found for session: {session_path}")
            return False

    def emigo_send(self, session_path: str, prompt: str):
        """Handles a prompt for a specific session path by delegating to the Agents."""
        print(f"Received prompt for session: {session_path} (Path: {session_path})", file=sys.stderr)

        # Ensure session_path is valid directory
        try:
            if not os.path.isdir(session_path):
                 raise ValueError("Session path is not a valid directory")
        except Exception as e:
             print(f"ERROR: Invalid session path provided: {session_path} - {e}", file=sys.stderr)
             # Try to message Emacs even if path is bad, using a placeholder name
             eval_in_emacs("emigo--flush-buffer", f"invalid-session-{session_path}", f"[Error: Invalid session path '{session_path}']", "error", True)
             return

        # Flush the user prompt to the Emacs buffer first
        is_first_message = session_path not in self.agent_dict
        eval_in_emacs("emigo--flush-buffer", session_path, f"\n\nUser:\n{prompt}\n", "user", is_first_message)

        # --- Handle File Mentions (@file) ---
        mention_pattern = r'@(\S+)'
        mentioned_files_in_prompt = re.findall(mention_pattern, prompt)
        if mentioned_files_in_prompt:
            print(f"Found file mentions in prompt: {mentioned_files_in_prompt}", file=sys.stderr)
            # Add these files to context *before* starting the agents interaction
            self.add_files_to_context(session_path, mentioned_files_in_prompt)
        # ---

        # Get or create the agents for this session
        agent_instance = self.agent_dict.get(session_path)
        if not agent_instance:
            try:
                agent_instance = self._start_agent(session_path)
                if not agent_instance: # Check if agents creation failed
                    return # Error already messaged by _start_agent
            except Exception as e:
                print(f"Failed to start agents for {session_path}: {e}", file=sys.stderr)
                eval_in_emacs("emigo--flush-buffer", session_path, f"[Error starting agents: {e}]", "error")
                return

        # Run the agents interaction in a separate thread
        thread = threading.Thread(target=agent_instance.run_interaction, args=(prompt,))
        thread.daemon = True # Allow program to exit even if agents threads are running
        thread.start()

    def add_files_to_context(self, session_path: str, files_to_add: List[str]) -> List[str]:
        """
        Adds a list of files to the chat context for a given session.

        Handles validation against session_path, prevents duplicates,
        updates self.chat_files[session_path], and notifies Emacs.

        Args:
            session_path: The absolute path identifier and root path for the session context.
            files_to_add: A list of relative or absolute file paths to potentially add.

        Returns:
            A list of the relative file paths that were newly added to the context.
        """
        if not files_to_add:
            return []

        # Ensure the session list exists in chat_files
        chat_files_list = self.chat_files.setdefault(session_path, [])
        chat_files_set = set(chat_files_list) # Use set for efficient checking
        newly_added_rel_paths = []

        for file_path_input in files_to_add:
            # Try to resolve the path relative to the session_path
            # Handle if file_path_input is already absolute
            if os.path.isabs(file_path_input):
                abs_path = file_path_input
            else:
                abs_path = os.path.abspath(os.path.join(session_path, file_path_input))

            # Check if the file exists and is within the session_path (basic check)
            if os.path.isfile(abs_path) and abs_path.startswith(session_path):
                # Always store the relative path
                rel_path = os.path.relpath(abs_path, session_path)
                if rel_path not in chat_files_set:
                    chat_files_list.append(rel_path)
                    chat_files_set.add(rel_path)
                    newly_added_rel_paths.append(rel_path)
                # else: already in context
            else:
                # File not found or outside session path
                message_emacs(f"Warning: File '{file_path_input}' not found or invalid for session {session_path}.")
                print(f"Warning: File '{file_path_input}' (resolved to {abs_path}) not found or invalid.", file=sys.stderr)


        if newly_added_rel_paths:
            added_files_str = ', '.join(newly_added_rel_paths)
            message_emacs(f"Added files to context for session {session_path}: {added_files_str}")
            print(f"Added files to context for session {session_path}: {added_files_str}", file=sys.stderr)
            # Update the main chat_files dictionary (already done by modifying list in place)

        return newly_added_rel_paths # Return list of newly added relative paths

    def _start_agent(self, session_path: str) -> Optional[Agents]:
        """Starts a new LLM client and Agents for a session."""
        verbose = True # Or get from config
        # --- Get Model Config ---
        vars_result = get_emacs_vars(["emigo-model", "emigo-base-url", "emigo-api-key"])
        if not vars_result or len(vars_result) < 3:
             message_emacs(f"Error retrieving Emacs variables for session {session_path}.")
             return None
        model, base_url, api_key = vars_result

        if not model: # Check only essential model name
            message_emacs(f"Please set emigo-model before starting session {session_path}.")
            return None

        # --- Initialize Client & Agents ---
        try:
            print(f"Starting LLM Client & Agents for session: {session_path} (Path: {session_path})", file=sys.stderr)
            client = LLMClient(
                model_name=model,
                api_key=api_key if api_key else None, # Pass None if empty string
                base_url=base_url if base_url else None,
                verbose=verbose,
            )
            # Pass the chat_files dictionary by reference
            agent_instance = Agents(session_path, client, self.chat_files, verbose)
            self.agent_dict[session_path] = agent_instance

            # Ensure chat_files list exists for this new session
            self.chat_files.setdefault(session_path, [])

            return agent_instance
        except Exception as e:
             print(f"Error initializing LLMClient/Agents for {session_path}: {e}", file=sys.stderr)
             message_emacs(f"Error initializing agents for session {session_path}: {e}")
             return None

    def cleanup(self):
        """Do some cleanup before exit python process."""
        close_epc_client()

    def clear_history(self, session_path: str) -> bool:
        """Clear the chat history for the given session path."""
        agent_instance = self.agent_dict.get(session_path)
        print("clearing history", session_path, self.agent_dict)
        if agent_instance:
            agent_instance.llm_client.clear_history()
            # Also clear local buffer via Emacs side
            eval_in_emacs("emigo--clear-local-buffer", session_path)
            return True
        return False

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        import cProfile
        profiler = cProfile.Profile()
        profiler.run("Emigo(sys.argv[1:])")
    else:
        Emigo(sys.argv[1:])
