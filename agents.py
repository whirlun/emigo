#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json # Added json import
import os
import re
import sys
import threading
import traceback
from typing import List, Dict, Optional, Tuple

from llm import LLMClient
from repomapper import RepoMapper # Used by list_repomap tool
from system_prompt import (
    MAIN_SYSTEM_PROMPT, TOOL_RESULT_SUCCESS, TOOL_RESULT_OUTPUT_PREFIX,
    TOOL_DENIED, TOOL_ERROR_PREFIX, TOOL_ERROR_SUFFIX, NO_TOOL_USED_ERROR,
    # Tool Names
    TOOL_EXECUTE_COMMAND, TOOL_READ_FILE, TOOL_WRITE_TO_FILE,
    TOOL_REPLACE_IN_FILE, TOOL_SEARCH_FILES, TOOL_LIST_FILES,
    TOOL_LIST_REPOMAP, TOOL_ASK_FOLLOWUP_QUESTION, TOOL_ATTEMPT_COMPLETION
)
from utils import (
    eval_in_emacs, message_emacs, get_command_result, get_os_name,
    get_emacs_var, get_emacs_vars, read_file_content, # Added read_file_content
    get_emacs_func_result # Added for potential future use
)
from config import IGNORED_DIRS # Import centralized list


class Agents:
    """Handles the agentic loop, interacting with the LLM and executing tools."""

    def __init__(self, session_path: str, llm_client: LLMClient, chat_files_ref: Dict[str, List[str]], verbose: bool = False):
        self.session_path = session_path # This is the root directory for the session
        self.llm_client = llm_client
        # self.emigo_instance = emigo_instance # Removed Emigo instance reference
        self.chat_files_ref = chat_files_ref # Reference to Emigo's chat_files dict
        self.verbose = verbose
        # Keep RepoMapper instance, but usage is restricted
        self.repo_mapper = RepoMapper(root_dir=self.session_path, verbose=self.verbose)
        self.current_tool_result: Optional[str] = None
        self.is_running = False
        self.lock = threading.Lock()
        # --- State for Environment Details ---
        self.chat_file_mtimes: Dict[str, float] = {} # Store mtimes {rel_path: mtime}
        self.chat_file_contents: Dict[str, str] = {} # Store content {rel_path: content}
        self.last_repomap_content: Optional[str] = None

    def _build_system_prompt(self) -> str:
        """Builds the system prompt, inserting dynamic info."""
        session_dir = self.session_path
        os_name = get_os_name()
        # Assuming get_emacs_var can fetch shell and homedir if needed
        shell = "/bin/bash" # Default shell
        homedir = os.path.expanduser("~")

        # Use .format() for clarity
        prompt = MAIN_SYSTEM_PROMPT.format(
            session_dir=session_dir.replace(os.sep, '/'), # Ensure POSIX paths for prompt consistency
            os_name=os_name,
            shell=shell,
            homedir=homedir.replace(os.sep, '/')
        )
        return prompt

    def _get_environment_details(self) -> str:
        """Fetches environment details: added file contents and repo map."""
        details = "<environment_details>\n"
        details += f"# Session Directory\n{self.session_path.replace(os.sep, '/')}\n\n" # Use POSIX path

        # --- Repository Map / Basic File Listing ---
        if self.last_repomap_content is not None: # Check if repomap has been generated at least once
            # Regenerate the map to ensure freshness
            try:
                print("Regenerating repository map for environment details...", file=sys.stderr)
                fresh_repomap_content = self.repo_mapper.generate_map()
                if not fresh_repomap_content:
                    fresh_repomap_content = "(No map content generated)"
                self.last_repomap_content = fresh_repomap_content # Update the cache
            except Exception as e:
                print(f"Error regenerating repomap for environment details: {e}", file=sys.stderr)
                # Keep the old content if regeneration fails, but add an error note
                details += f"# Error regenerating repository map: {e}\n"
                # Fall through to use potentially stale self.last_repomap_content

            # Add the (potentially updated) repomap content
            details += "# Repository Map (Refreshed)\n"
            # Ensure repomap content is also in a code block for clarity
            details += f"```\n{self.last_repomap_content}\n```\n\n"
        else:
            # If repomap hasn't been generated yet, show recursive directory listing
            details += "# File/Directory Structure (use list_repomap tool for full details)\n"
            try:
                structure_lines = []
                # Use IGNORED_DIRS from config
                for root, dirnames, filenames in os.walk(self.session_path, topdown=True):
                    # Filter ignored directories *before* processing
                    dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith('.')]
                    # Filter ignored/hidden files
                    filenames = [f for f in filenames if not f.startswith('.')] # Basic hidden file filter

                    rel_root = os.path.relpath(root, self.session_path)
                    if rel_root == '.':
                        rel_root = '' # Avoid './' prefix for root level
                    else:
                        rel_root = rel_root.replace(os.sep, '/') + '/' # Use POSIX path and add slash

                    # Calculate indentation level
                    level = rel_root.count('/')

                    # Add current directory to structure (if not root)
                    if rel_root:
                         indent = '  ' * (level -1) # Indent based on depth
                         structure_lines.append(f"{indent}- {os.path.basename(rel_root[:-1])}/") # Show dir name

                    # Add files in the current directory
                    file_indent = '  ' * level
                    for filename in sorted(filenames):
                        structure_lines.append(f"{file_indent}- {filename}")

                if structure_lines:
                    details += "```\n" # Use code block for structure
                    details += "\n".join(structure_lines)
                    details += "\n```\n\n"
                else:
                    details += "(No relevant files or directories found)\n\n"
            except Exception as e:
                details += f"# Error listing files/directories: {str(e)}\n\n"


        # --- Added Chat Files Content ---
        # Use the direct reference chat_files_ref for reading here
        chat_files_list = self.chat_files_ref.get(self.session_path, [])
        if chat_files_list:
            details += "# Added Files Contents\n"
            # Clean up stored mtimes/content for files no longer in chat_files_list
            current_chat_files_set = set(chat_files_list)
            for rel_path in list(self.chat_file_mtimes.keys()):
                if rel_path not in current_chat_files_set:
                    del self.chat_file_mtimes[rel_path]
                    if rel_path in self.chat_file_contents:
                        del self.chat_file_contents[rel_path]

            for rel_path in sorted(chat_files_list): # Sort for consistent order
                abs_path = os.path.abspath(os.path.join(self.session_path, rel_path))
                posix_rel_path = rel_path.replace(os.sep, '/')
                try:
                    # Access RepoMap's get_mtime via the RepoMapper instance
                    current_mtime = self.repo_mapper.repo_mapper.get_mtime(abs_path)
                    last_mtime = self.chat_file_mtimes.get(rel_path)

                    if current_mtime is None: # File might have been deleted
                        content = f"# Error: Could not get mtime for {posix_rel_path}\n"
                        if rel_path in self.chat_file_mtimes: del self.chat_file_mtimes[rel_path]
                        if rel_path in self.chat_file_contents: del self.chat_file_contents[rel_path]
                        self.chat_file_contents[rel_path] = content # Store error state
                    elif last_mtime is None or current_mtime != last_mtime:
                        # File is new or changed, read content
                        if self.verbose: print(f"Reading updated content for {posix_rel_path}", file=sys.stderr)
                        content = read_file_content(abs_path)
                        self.chat_file_mtimes[rel_path] = current_mtime
                        self.chat_file_contents[rel_path] = content # Update stored content
                    else:
                        # File unchanged, use cached content
                        content = self.chat_file_contents.get(rel_path, f"# Error: Content not cached for {posix_rel_path}\n") # Fallback

                    # Use markdown code block for file content
                    details += f"## File: {posix_rel_path}\n```\n{content}\n```\n\n"

                except Exception as e:
                    details += f"## File: {posix_rel_path}\n# Error reading file: {e}\n\n"
                    # Clean up potentially stale cache entries on error
                    if rel_path in self.chat_file_mtimes: del self.chat_file_mtimes[rel_path]
                    if rel_path in self.chat_file_contents: del self.chat_file_contents[rel_path]
            details += "\n" # Add separation


        # --- Other details (Emacs state, etc.) ---
        # TODO: Add other details like open files, running terminals by calling Emacs funcs
        # Example: open_files = get_emacs_func_result("emigo--get-open-files")
        # Example: running_terminals = get_emacs_func_result("emigo--get-running-terminals")

        details += "</environment_details>"
        return details

    def _parse_tool_use(self, response_text: str) -> List[Tuple[str, Dict[str, str]]]:
        """Parses the LLM response XML for *all* valid tool uses, ignoring thinking tags.

        Returns:
            A list of tuples, where each tuple is (tool_name, params_dict).
            Returns an empty list if no known tools are found.
        """
        parsed_tools = []
        try:
            # 1. Define known tools
            known_tools = {
                TOOL_EXECUTE_COMMAND, TOOL_READ_FILE, TOOL_WRITE_TO_FILE,
                TOOL_REPLACE_IN_FILE, TOOL_SEARCH_FILES, TOOL_LIST_FILES,
                TOOL_LIST_REPOMAP, TOOL_ASK_FOLLOWUP_QUESTION,
                TOOL_ATTEMPT_COMPLETION
            }

            # 2. Find *all* potential top-level XML-like blocks
            # Regex looks for <tag>content</tag> structure
            potential_blocks = re.findall(r"<([a-zA-Z0-9_]+)(?:\s+[^>]*)?>(.*?)</\1>", response_text, re.DOTALL)

            # 3. Iterate through blocks to find *all* known tools
            for tool_name, tool_content in potential_blocks:
                if tool_name in known_tools:
                    # Found a known tool, parse its parameters
                    params = {}
                    param_matches = re.findall(r"<([a-zA-Z0-9_]+)>(.*?)</\1>", tool_content, re.DOTALL)
                    for param_name, param_value in param_matches:
                        params[param_name] = param_value.strip() # Strip whitespace

                    print(f"Parsed tool use: {tool_name} with params: {params}", file=sys.stderr)
                    parsed_tools.append((tool_name, params))
                # else: Ignore non-tool blocks like <thinking>

            # 4. Return the list of found tools (could be empty)
            if not parsed_tools:
                print("No known tool use found in the response.", file=sys.stderr)
            return parsed_tools

        except Exception as e:
            print(f"Error parsing tool use: {e}\nText: {response_text}", file=sys.stderr)
            return [] # Return empty list on error

    def _format_tool_result(self, result_content: str) -> str:
        """Formats the tool result for the next LLM prompt."""
        # Keep it simple for now, just return the text
        return result_content

    def _format_tool_error(self, error_message: str) -> str:
        """Formats a tool error message."""
        # Ensure error message is properly escaped if needed, though likely fine
        return f"{TOOL_ERROR_PREFIX}{error_message}{TOOL_ERROR_SUFFIX}"

    # --- Tool Handlers ---

    def _handle_execute_command(self, params: Dict[str, str]) -> str:
        command = params.get("command")
        # requires_approval = params.get("requires_approval", "false").lower() == "true" # Approval handled before calling

        if not command:
            return self._format_tool_error(f"Missing required parameter 'command' for {TOOL_EXECUTE_COMMAND}")

        try:
            print(f"Executing command: {command} in {self.session_path}", file=sys.stderr)
            # Use synchronous call to Emacs to run command and get result
            # This allows Emacs to manage the process and capture output reliably
            output = get_emacs_func_result("execute-command-sync", self.session_path, command)
            return self._format_tool_result(f"{TOOL_RESULT_SUCCESS}\n{TOOL_RESULT_OUTPUT_PREFIX}{output}")
        except Exception as e:
            print(f"Error executing command '{command}' via Emacs: {e}", file=sys.stderr)
            return self._format_tool_error(f"Error executing command: {e}")

    def _handle_read_file(self, params: Dict[str, str]) -> str:
        rel_path = params.get("path")
        if not rel_path:
            return self._format_tool_error(f"Missing required parameter 'path' for {TOOL_READ_FILE}")

        # Use session_path as the base directory
        abs_path = os.path.abspath(os.path.join(self.session_path, rel_path))
        try:
            # TODO: Check .clineignore equivalent if implemented
            if not os.path.isfile(abs_path):
                 return self._format_tool_error(f"File not found: {rel_path}")

            # Use utils.read_file_content
            content = read_file_content(abs_path)
            # Use POSIX path in the response tag for consistency
            posix_rel_path = rel_path.replace(os.sep, '/')

            # --- Add file to context after successful read ---
            # Directly modify the chat_files list via the reference.
            # Note: This does NOT send a confirmation message back to Emacs,
            # unlike the previous approach using emigo_instance.add_files_to_context.
            try:
                session_files = self.chat_files_ref.setdefault(self.session_path, [])
                if rel_path not in session_files:
                    session_files.append(rel_path)
                    if self.verbose:
                        print(f"Added '{rel_path}' to internal chat context for session {os.path.basename(self.session_path)}.", file=sys.stderr)
            except Exception as add_err:
                # Log error but don't fail the read operation itself
                print(f"Warning: Failed to add '{rel_path}' to internal context after reading: {add_err}", file=sys.stderr)
            # ---

            # Return only success message; content will be in environment_details
            return self._format_tool_result(f"{TOOL_RESULT_SUCCESS}\nFile '{posix_rel_path}' read and added to context.")
        except Exception as e:
            print(f"Error reading file '{rel_path}': {e}", file=sys.stderr)
            return self._format_tool_error(f"Error reading file: {e}")

    def _handle_write_to_file(self, params: Dict[str, str]) -> str:
        rel_path = params.get("path")
        content = params.get("content")
        if not rel_path:
            return self._format_tool_error(f"Missing required parameter 'path' for {TOOL_WRITE_TO_FILE}")
        if content is None: # Allow empty content
            return self._format_tool_error(f"Missing required parameter 'content' for {TOOL_WRITE_TO_FILE}")

        abs_path = os.path.abspath(os.path.join(self.session_path, rel_path))
        posix_rel_path = rel_path.replace(os.sep, '/') # For response tag
        try:
            # TODO: Check .clineignore equivalent
            # Create directories if they don't exist
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)

            # Write directly (approval happened before calling this handler)
            with open(abs_path, 'w', encoding='utf-8') as f:
                f.write(content)

            # Inform Emacs about the change
            eval_in_emacs("emigo--file-written-externally", abs_path)

            # Read back the potentially auto-formatted content via Emacs
            # This requires a new synchronous Emacs function
            get_emacs_func_result("read-file-content-sync", abs_path)

            return self._format_tool_result(f"{TOOL_RESULT_SUCCESS}\nFile '{posix_rel_path}' written successfully.\n")
        except Exception as e:
            print(f"Error writing file '{rel_path}': {e}", file=sys.stderr)
            return self._format_tool_error(f"Error writing file: {e}")

    def _handle_replace_in_file(self, params: Dict[str, str]) -> str:
        rel_path = params.get("path")
        diff_str = params.get("diff") # The SEARCH/REPLACE block(s)
        if not rel_path:
            return self._format_tool_error(f"Missing required parameter 'path' for {TOOL_REPLACE_IN_FILE}")
        if not diff_str:
            return self._format_tool_error(f"Missing required parameter 'diff' for {TOOL_REPLACE_IN_FILE}")

        abs_path = os.path.abspath(os.path.join(self.session_path, rel_path))
        posix_rel_path = rel_path.replace(os.sep, '/') # For response tag
        try:
            # TODO: Check .clineignore equivalent
            if not os.path.isfile(abs_path):
                 return self._format_tool_error(f"File not found: {rel_path}")

            # Delegate the complex diff application to Emacs (synchronous call)
            result = get_emacs_func_result("apply-diff-sync", self.session_path, abs_path, diff_str)

            # Check the structure of the result from Emacs
            if isinstance(result, list) and len(result) > 0:
                 status = result[0]
                 if status == ':error' and len(result) > 1:
                     error_detail = result[1]
                     print(f"Error applying diff to '{rel_path}': {error_detail}", file=sys.stderr)
                     # Provide original content back to LLM on error
                     original_content = read_file_content(abs_path) # Read directly as fallback
                     return self._format_tool_error(
                         f"Error applying diff: {error_detail}\n\n"
                         f"This is likely because the SEARCH block content doesn't match exactly "
                         f"with what's in the file, or if you used multiple SEARCH/REPLACE blocks "
                         f"they may not have been in the order they appear in the file.\n\n"
                         f"The file was reverted to its original state:\n\n"
                         f"<file_content path=\"{posix_rel_path}\">\n{original_content}\n</file_content>\n\n"
                         f"Please use read_file to get the latest content and try the edit again."
                     )
                 # Compare the string representation of the status symbol
                 elif str(status) == ':final_content' and len(result) > 1:
                     # Success, Emacs returned the final content
                     return self._format_tool_result(f"{TOOL_RESULT_SUCCESS}\nFile '{posix_rel_path}' modified successfully.\n")

            # Unexpected result format from Emacs
            print(f"Unexpected result from emigo--apply-diff-sync for '{rel_path}': {result}", file=sys.stderr)
            return self._format_tool_error("Unexpected error applying diff in Emacs.")

        except Exception as e:
            print(f"Error calling Emacs for diff apply on '{rel_path}': {e}", file=sys.stderr)
            return self._format_tool_error(f"Error applying diff: {e}")

    def _handle_ask_followup_question(self, params: Dict[str, str]) -> str:
        question = params.get("question")
        options_str = params.get("options") # Optional: "[Option1, Option2]"
        if not question:
            return self._format_tool_error(f"Missing required parameter 'question' for {TOOL_ASK_FOLLOWUP_QUESTION}")

        try:
            # Ask Emacs to present the question and get the user's answer (synchronous)
            answer = get_emacs_func_result("ask-user-sync", self.session_path, question, options_str or "[]") # Pass empty list if no options
            if answer is None or answer == "": # Check for empty string too, might indicate cancellation
                return self._format_tool_result("User did not provide an answer.")
            else:
                return self._format_tool_result(f"<answer>\n{answer}\n</answer>")
        except Exception as e:
            print(f"Error asking followup question: {e}", file=sys.stderr)
            return self._format_tool_error(f"Error asking question: {e}")

    def _handle_attempt_completion(self, params: Dict[str, str]) -> str:
        result_text = params.get("result")
        command = params.get("command") # Optional command to demonstrate

        if not result_text:
            return self._format_tool_error(f"Missing required parameter 'result' for {TOOL_ATTEMPT_COMPLETION}")

        try:
            # Signal completion to Emacs (asynchronous is fine here)
            eval_in_emacs("emigo--signal-completion", self.session_path, result_text, command or "") # Pass empty string if no command
            # This tool use itself doesn't return content to the LLM, it ends the loop.
            return "COMPLETION_SIGNALLED"
        except Exception as e:
            print(f"Error signalling completion: {e}", file=sys.stderr)
            # If signalling fails, maybe return error to LLM?
            return self._format_tool_error(f"Error signalling completion: {e}")

    def _handle_list_repomap(self, params: Dict[str, str]) -> str:
        """Generates and returns the repository map for the specified path."""
        # Note: The 'path' parameter from the tool definition seems redundant
        # if the map is always generated for the agent's session_path (root).
        # We'll ignore the 'path' param for now and use self.session_path.
        # If needed later, we could adjust RepoMapper to map subdirectories.
        # path_param = params.get("path") # Currently ignored

        try:
            chat_files = self.chat_files_ref.get(self.session_path, [])
            # TODO: Get mentioned files/idents if needed by repomapper
            print(f"Generating repomap for {self.session_path} with chat files: {chat_files}", file=sys.stderr)
            repo_map_content = self.repo_mapper.generate_map()
            if not repo_map_content:
                repo_map_content = "(No map content generated)"

            # Store the generated map content for inclusion in environment details
            self.last_repomap_content = repo_map_content

            # Return only success message; map content will be in environment_details
            return self._format_tool_result(f"{TOOL_RESULT_SUCCESS}\nRepository map generated for {self.session_path.replace(os.sep, '/')}.")
        except Exception as e:
            print(f"Error generating repomap: {e}", file=sys.stderr)
            # Clear stored map on error
            self.last_repomap_content = None
            return self._format_tool_error(f"Error generating repository map: {e}")

    def _handle_list_files(self, params: Dict[str, str]) -> str:
        rel_path = params.get("path")
        recursive = params.get("recursive", "false").lower() == "true"
        if not rel_path:
            return self._format_tool_error("Missing 'path'")

        abs_path = os.path.abspath(os.path.join(self.session_path, rel_path))
        try:
            if not os.path.isdir(abs_path):
                return self._format_tool_error(f"Path is not a directory: {rel_path}")

            files = []
            if recursive:
                for root, _, filenames in os.walk(abs_path):
                    for f in filenames:
                        full_path = os.path.join(root, f)
                        rel_file = os.path.relpath(full_path, self.session_path)
                        files.append(rel_file.replace(os.sep, '/'))  # Use POSIX paths
            else:
                files = [f.replace(os.sep, '/') for f in os.listdir(abs_path)
                        if os.path.isfile(os.path.join(abs_path, f))]

            files = sorted(files)  # Sort alphabetically
            return self._format_tool_result(
                f"{TOOL_RESULT_SUCCESS}\nFiles in '{rel_path}' ({'recursive' if recursive else 'non-recursive'}):\n" +
                "\n".join(f"- {f}" for f in files)
            )
        except Exception as e:
            return self._format_tool_error(f"Error listing files: {e}")

    def _execute_tool(self, tool_name: str, params: Dict[str, str]) -> str:
        """Executes the appropriate tool handler after requesting Emacs approval."""
        handler_map = {
            TOOL_EXECUTE_COMMAND: self._handle_execute_command,
            TOOL_READ_FILE: self._handle_read_file,
            TOOL_WRITE_TO_FILE: self._handle_write_to_file,
            TOOL_REPLACE_IN_FILE: self._handle_replace_in_file,
            TOOL_ASK_FOLLOWUP_QUESTION: self._handle_ask_followup_question,
            TOOL_ATTEMPT_COMPLETION: self._handle_attempt_completion,
            TOOL_LIST_REPOMAP: self._handle_list_repomap,
            TOOL_LIST_FILES: self._handle_list_files,
            TOOL_SEARCH_FILES: self._handle_search_files,
        }

        handler = handler_map.get(tool_name)
        if not handler:
            print(f"Unknown tool requested: {tool_name}", file=sys.stderr)
            return self._format_tool_error(f"Unknown tool: {tool_name}")

        # Define tools that require explicit approval
        no_auto_approve_list = [TOOL_EXECUTE_COMMAND, TOOL_WRITE_TO_FILE]

        # Only request approval for tools in the no_auto_approve_list
        if tool_name in no_auto_approve_list:
            try:
                # Convert params dict to a plist string for Elisp
                params_plist_str = "(" + " ".join([f":{k} {json.dumps(v)}" for k, v in params.items()]) + ")"
                is_approved = get_emacs_func_result("request-tool-approval-sync", self.session_path, tool_name, params_plist_str)

                if not is_approved: # Emacs function should return t or nil
                    print(f"Tool use denied by user: {tool_name}", file=sys.stderr)
                    return self._format_tool_result(TOOL_DENIED)
            except Exception as e:
                print(f"Error requesting tool approval from Emacs: {e}", file=sys.stderr)
                return self._format_tool_error(f"Error requesting tool approval: {e}")

        # --- Execute if Approved ---
        print(f"Executing approved tool: {tool_name}", file=sys.stderr)
        try:
            return handler(params)
        except Exception as e:
             # Catch errors within the handler itself
             print(f"Error during execution of tool '{tool_name}': {e}\n{traceback.format_exc()}", file=sys.stderr)
             return self._format_tool_error(f"Error executing tool '{tool_name}': {e}")

    def run_interaction(self, initial_user_prompt: str):
        """Runs the main agent interaction loop."""
        with self.lock:
            if self.is_running:
                print("Agent interaction already running.", file=sys.stderr)
                eval_in_emacs("emigo--flush-buffer", self.session_path, "[Agent busy, please wait]", "error")
                return
            self.is_running = True

        try:
            system_prompt = self._build_system_prompt()

            # Add initial prompt directly to the persistent history
            self.llm_client.append_history({"role": "user", "content": initial_user_prompt})

            max_turns = 10 # Limit turns to prevent infinite loops
            for turn in range(max_turns):
                print(f"\n--- Agent Turn {turn + 1}/{max_turns} (Session: {os.path.basename(self.session_path)}) ---", file=sys.stderr)

                # --- Build Prompt ---
                # Start with system prompt and the *current* persistent history
                base_messages = [{"role": "system", "content": system_prompt}]
                # Extract only message dicts from history tuples
                history_dicts = [msg_dict for _, msg_dict in self.llm_client.get_history()]
                base_messages.extend(history_dicts) # Get latest history dictionaries

                # Create a temporary list of messages to send, including context
                messages_to_send = [msg.copy() for msg in base_messages] # Shallow copy is enough

                # Find the last user message *in the temporary list* to append context
                last_user_message_index = -1
                try:
                    # Find the index of the last message with role 'user'
                    last_user_message_index = next(i for i, msg in enumerate(reversed(messages_to_send)) if msg["role"] == "user")
                    last_user_message_index = len(messages_to_send) - 1 - last_user_message_index
                except StopIteration:
                     print("Error: No user message found in history to append context to.", file=sys.stderr)
                     eval_in_emacs("emigo--flush-buffer", self.session_path, "[Internal Error: History state invalid]", "error")
                     break # Exit loop

                # Prepare context to add (only environment details)
                # The previous tool result is already the last user message in current_history / base_messages
                environment_details = self._get_environment_details()
                context_to_add = f"\n\n{environment_details}" # Start with newline for separation

                # Modify the content of the last user message *only in the temporary list*
                messages_to_send[last_user_message_index]["content"] += context_to_add

                # Consume the tool result flag *after* it has been added to history in the previous turn
                self.current_tool_result = None

                # --- Send to LLM (Streaming) ---
                full_response = ""
                eval_in_emacs("emigo--flush-buffer", self.session_path, "\nAssistant:\n", "llm") # Signal start
                try:
                    # Send the temporary list with context included
                    response_stream = self.llm_client.send(messages_to_send, stream=True)
                    for chunk in response_stream:
                        eval_in_emacs("emigo--flush-buffer", self.session_path, str(chunk), "llm")
                        full_response += chunk

                except Exception as e:
                    error_message = f"[Error during LLM communication: {e}]"
                    print(f"\n{error_message}", file=sys.stderr)
                    eval_in_emacs("emigo--flush-buffer", self.session_path, str(error_message), "error")
                    # Add error to persistent history
                    self.llm_client.append_history({"role": "assistant", "content": error_message})
                    break # Exit loop on error

                # Add assistant's full response to persistent history *before* tool processing
                self.llm_client.append_history({"role": "assistant", "content": full_response})

                # --- Parse and Execute Tools ---
                tool_list = self._parse_tool_use(full_response)
                turn_tool_results = []
                completion_signalled = False
                tool_denied = False

                if tool_list:
                    print(f"Executing {len(tool_list)} tools for this turn...", file=sys.stderr)
                    for tool_name, params in tool_list:
                        tool_result = self._execute_tool(tool_name, params)
                        turn_tool_results.append(tool_result) # Collect result

                        if tool_result == "COMPLETION_SIGNALLED":
                            print("Completion signalled. Ending interaction after this tool.", file=sys.stderr)
                            completion_signalled = True
                            break # Stop processing more tools in this turn

                        if tool_result == self._format_tool_result(TOOL_DENIED):
                            print("Tool denied, ending interaction after this tool.", file=sys.stderr)
                            tool_denied = True
                            break # Stop processing more tools in this turn

                    # Combine results for the next LLM turn
                    if turn_tool_results:
                        combined_result_message = "\n\n".join(turn_tool_results)
                        # Store combined result for the next turn's prompt context (if needed, though env details might suffice)
                        self.current_tool_result = combined_result_message
                        # Add combined tool result message to persistent history for the LLM's next turn
                        self.llm_client.append_history({"role": "user", "content": combined_result_message})

                    # If completion was signalled or a tool was denied, exit the main loop
                    if completion_signalled or tool_denied:
                        break

                else:
                    # No tool use found in the response.
                    is_empty_or_whitespace = not full_response.strip()

                    if is_empty_or_whitespace:
                        # Empty response from LLM. Treat as end of interaction or potential error.
                        print("Empty response received from LLM, ending interaction.", file=sys.stderr)
                        break # Exit loop gracefully on empty response
                    else:
                        # Response has content but no valid tool.
                        # Assume this is the final answer from the LLM for this interaction,
                        # regardless of thinking tags.
                        print("No tool use found, assuming final response. Ending interaction.", file=sys.stderr)
                        # Don't add an error message. Just break the loop.
                        break # Exit loop gracefully
            else:
                # Loop finished due to max_turns
                print(f"Warning: Exceeded max turns ({max_turns}) for session {os.path.basename(self.session_path)}.", file=sys.stderr)
                eval_in_emacs("emigo--flush-buffer", self.session_path, "[Warning: Agent reached max interaction turns]", "warning")


        except Exception as e:
            print(f"Critical error in agent interaction loop: {e}\n{traceback.format_exc()}", file=sys.stderr)
            eval_in_emacs("emigo--flush-buffer", self.session_path, f"[Agent Critical Error: {e}]", "error")
        finally:
            with self.lock:
                self.is_running = False
            print(f"Agent interaction finished for session {os.path.basename(self.session_path)}.", file=sys.stderr)
            # Signal Emacs that the agent is done for this request
            eval_in_emacs("emigo--agent-finished", self.session_path)

    def _handle_search_files(self, params: Dict[str, str]) -> str:
        """Search files for text patterns using Python regex matching.

        Args:
            params: Dictionary containing:
                - path: Directory to search (defaults to current directory)
                - pattern: Python regex pattern to search for
                - case_sensitive: Whether search is case sensitive (default false)
                - max_matches: Maximum number of matches to return (default 20, max 100)

        Returns:
            Formatted string with matches or error message
        """
        rel_path = params.get("path", ".")  # Default to current directory
        pattern = params.get("pattern")
        case_sensitive = params.get("case_sensitive", "false").lower() == "true"
        max_matches = min(100, int(params.get("max_matches", "20")))  # Cap at 100 matches

        if not pattern:
            return self._format_tool_error(
                "Missing 'pattern' parameter. Provide a Python regex pattern.\n"
                "Example patterns:\n"
                "- 'def\\s+\\w+' to find function definitions\n"
                "- 'TODO|FIXME' to find todos\n"
                "- '\\bclass\\s+\\w+' to find class definitions\n"
                "See Python regex syntax: https://docs.python.org/3/library/re.html"
            )

        abs_path = os.path.abspath(os.path.join(self.session_path, rel_path))
        try:
            if not os.path.exists(abs_path):
                return self._format_tool_error(f"Path not found: {rel_path}")

            # Validate regex pattern first
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                compiled_pattern = re.compile(pattern, flags)
            except re.error as e:
                return self._format_tool_error(
                    f"Invalid regex pattern: {e}\n"
                    f"Pattern: {pattern}\n"
                    "See Python regex syntax: https://docs.python.org/3/library/re.html"
                )

            matches = []
            # Walk through files and search
            for root, _, filenames in os.walk(abs_path):
                for filename in filenames:
                    if len(matches) >= max_matches:
                        break

                    filepath = os.path.join(root, filename)
                    try:
                        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                            for i, line in enumerate(f, 1):
                                if compiled_pattern.search(line):
                                    rel_file = os.path.relpath(filepath, self.session_path)
                                    matches.append({
                                        'file': rel_file.replace(os.sep, '/'),
                                        'line': i,
                                        'content': line.strip()
                                    })
                                    if len(matches) >= max_matches:
                                        break
                    except (IOError, UnicodeDecodeError):
                        continue  # Skip unreadable files

                if len(matches) >= max_matches:
                    break

            if not matches:
                return self._format_tool_result(f"{TOOL_RESULT_SUCCESS}\nNo matches found for pattern: {pattern}")

            result = f"{TOOL_RESULT_SUCCESS}\nFound {len(matches)} matches for pattern '{pattern}':\n"
            for match in matches:
                result += f"\n{match['file']}:{match['line']}\n  {match['content']}\n"

            if len(matches) == max_matches:
                result += "\n[Note: Results truncated to first 100 matches]"

            return self._format_tool_result(result)

        except Exception as e:
            return self._format_tool_error(f"Error searching files: {e}\n{traceback.format_exc()}")
