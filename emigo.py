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
import threading
import traceback
import sys
from epc.server import ThreadingEPCServer
from utils import *

import json
import os
import re
# Removed subprocess import
from datetime import datetime
# Assuming llm.py and prompt_builder.py are in the same directory or Python path
from llm import LLMClient
from prompt_builder import PromptBuilder # Import the new class

class Emigo:
    def __init__(self, args):
        # Init EPC client port.
        init_epc_client(int(args[0]))

        # Init vars.
        self.llm_client_dict = {}

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
            eval_in_emacs("emigo-create-ai-window", project_path)

            if project_path in self.llm_client_dict:
                print("****** ", project_path, prompt)
            else:
                self.start_llm_client(project_path, prompt)
        else:
            print("EMIGO ERROR: parse project path of '{}' failed".format(filename))

    def start_llm_client(self, project_path, prompt):
        verbose = True
        no_shell = True
        print_prompt = True
        map_tokens = 4096
        chat_files = []
        read_only_files = []
        tokenizer = "cl100k_base"
        history_file = ".emigo_history.md"

        # --- Pre-process: Find and add @-mentioned files ---
        mentioned_in_prompt = set()
        pattern = r'@(\S+)' # Find @ followed by non-whitespace characters
        matches = re.findall(pattern, prompt)
        if matches:
            if verbose:
                print(f"Found potential @-mentions: {matches}", file=sys.stderr)
            for potential_file in matches:
                # Strip trailing punctuation that might be attached
                potential_file = potential_file.rstrip('.,;:!?')
                abs_path = os.path.abspath(os.path.join(project_path, potential_file))
                if os.path.isfile(abs_path):
                    # Use the relative path as provided in the mention
                    mentioned_in_prompt.add(potential_file)
                    if verbose:
                        print(f"  Validated and adding to chat_files: {potential_file}", file=sys.stderr)
                elif verbose:
                    print(f"  Ignoring mention '{potential_file}': File not found or not a file at {abs_path}", file=sys.stderr)

        # Combine CLI args with prompt mentions, ensuring uniqueness
        original_chat_files = set(chat_files)
        updated_chat_files = sorted(list(original_chat_files.union(mentioned_in_prompt)))

        if verbose and updated_chat_files != chat_files:
            print(f"Updated chat_files list: {updated_chat_files}", file=sys.stderr)
        chat_files = updated_chat_files # Update args object

        # --- 1. Build the Prompt using imported PromptBuilder ---
        if verbose:
            print("\n--- Building prompt using PromptBuilder ---", file=sys.stderr)

        try:
            builder = PromptBuilder(
                root_dir=project_path,
                user_message=prompt,
                chat_files=chat_files,
                read_only_files=read_only_files,
                map_tokens=map_tokens,
                tokenizer=tokenizer,
                verbose=verbose,
                no_shell=no_shell,
                history_file=history_file, # Pass history file path
                # Assuming default fences '```' are okay, add args if needed
            )
            messages = builder.build_prompt_messages()

            if verbose:
                 print("--- PromptBuilder output (messages) ---", file=sys.stderr)
                 # Avoid printing full base64 images if any were included
                 printable_messages = []
                 for msg in messages:
                     if isinstance(msg.get("content"), list): # Handle image messages
                         new_content = []
                         for item in msg["content"]:
                             if isinstance(item, dict) and item.get("type") == "image_url":
                                  img_url = item.get("image_url", {}).get("url", "")
                                  if isinstance(img_url, str) and img_url.startswith("data:"):
                                      new_content.append({"type": "image_url", "image_url": {"url": img_url[:50] + "..."}})
                                  else:
                                      new_content.append(item)
                             else:
                                 new_content.append(item)
                         printable_messages.append({"role": msg["role"], "content": new_content})
                     else:
                         printable_messages.append(msg)
                 print(json.dumps(printable_messages, indent=2), file=sys.stderr)
                 print("--- End PromptBuilder output ---", file=sys.stderr)

        except Exception as e:
            print(f"Error during prompt building: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            sys.exit(1)

        # --- Optional: Print the full prompt before sending ---
        if print_prompt:
            print("\n--- Full Prompt to LLM ---", file=sys.stderr)
            # Use the same printable logic as verbose output for messages
            printable_messages = []
            for msg in messages:
                if isinstance(msg.get("content"), list): # Handle image messages
                    new_content = []
                    for item in msg["content"]:
                        if isinstance(item, dict) and item.get("type") == "image_url":
                             img_url = item.get("image_url", {}).get("url", "")
                             if isinstance(img_url, str) and img_url.startswith("data:"):
                                 new_content.append({"type": "image_url", "image_url": {"url": img_url[:50] + "..."}})
                             else:
                                 new_content.append(item)
                        else:
                            new_content.append(item)
                    printable_messages.append({"role": msg["role"], "content": new_content})
                else:
                    printable_messages.append(msg)
            print(json.dumps(printable_messages, indent=2), file=sys.stderr)
            print("--- End Full Prompt ---", file=sys.stderr)


        # --- 2. Interact with LLM ---
        [model, base_url, api_key] = get_emacs_vars(["emigo-model", "emigo-base-url", "emigo-api-key"])
        if model == "" or base_url == "" or api_key == "":
            message_emacs("Please set emigo-model, emigo-base-url and emigo-api-key before call emigo.")
            return

        eval_in_emacs("emigo-flush-ai-buffer", project_path, "{}\n\n".format(prompt))

        client = LLMClient(
            model_name=model,
            api_key=api_key,
            base_url=base_url,
            verbose=verbose,
        )
        self.llm_client_dict[project_path] = client

        print("\nAssistant:") # Header for the output
        full_response = ""
        try:
            # Send the messages generated by prompt_builder directly
            response_stream = client.send(messages, stream=True)
            for chunk in response_stream:
                eval_in_emacs("emigo-flush-ai-buffer", project_path, chunk)
                full_response += chunk
            print() # Ensure a newline after the stream

        except Exception as e:
            print(f"\nError during LLM communication: {e}", file=sys.stderr)
            # Decide if you want to exit or just log the error
            # For now, we'll log and continue to history writing if possible
            full_response = f"[Error during LLM communication: {e}]"


        # --- 3. Log History ---
        if history_file:
            self.append_to_history(history_file, prompt, full_response)
            if verbose:
                print(f"\nInteraction logged to {history_file}", file=sys.stderr)

    def cleanup(self):
        """Do some cleanup before exit python process."""
        close_epc_client()

    def append_to_history(self, history_file: str, user_prompt: str, assistant_response: str):
        """Appends the user prompt and assistant response to the history file."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(history_file, "a", encoding="utf-8") as f:
                f.write(f"#### User @ {timestamp}\n")
                f.write(f"{user_prompt.strip()}\n\n")
                f.write(f"#### Assistant @ {timestamp}\n")
                f.write(f"{assistant_response.strip()}\n\n")
        except IOError as e:
            print(f"Warning: Could not write to history file {history_file}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"Warning: An unexpected error occurred writing to history file: {e}", file=sys.stderr)

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        import cProfile
        profiler = cProfile.Profile()
        profiler.run("Emigo(sys.argv[1:])")
    else:
        Emigo(sys.argv[1:])
