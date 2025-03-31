#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json # Added json import
import os
import re
import sys
import threading
import traceback
import xml.etree.ElementTree as ET # Not used currently, but kept for potential future XML parsing needs
from typing import List, Dict, Optional, Tuple, Union, Iterator # Added Union, Iterator

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

class Agents:
    """Handles the agentic loop, interacting with the LLM and executing tools."""

    def __init__(self, session_path: str, llm_client: LLMClient, chat_files_ref: Dict[str, List[str]], verbose: bool = False):
        self.session_path = session_path # This is the root directory for the session
        self.llm_client = llm_client
        self.chat_files_ref = chat_files_ref # Reference to Emigo's chat_files dict
        self.verbose = verbose
        # Keep RepoMapper instance, but usage is restricted
        self.repo_mapper = RepoMapper(root_dir=self.session_path, verbose=self.verbose)
        self.current_tool_result: Optional[str] = None
        self.is_running = False
        self.lock = threading.Lock()

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
        """Fetches environment details like repo map, open files, etc."""
        details = "<environment_details>\n"
        details += f"# Session Directory\n{self.session_path.replace(os.sep, '/')}\n\n" # Use POSIX path

        # List files in session directory
        try:
            files = []
            for f in os.listdir(self.session_path):
                full_path = os.path.join(self.session_path, f)
                if os.path.isfile(full_path):
                    files.append(f)

            if files:
                details += "# Files in Session Directory\n"
                details += "\n".join(f"- {f}" for f in sorted(files)) + "\n\n"
            else:
                details += "# No files found in session directory\n\n"

        except Exception as e:
            details += f"# Error listing files: {str(e)}\n\n"

        # TODO: Add other details like open files, running terminals by calling Emacs funcs
        # Example: open_files = get_emacs_func_result("emigo--get-open-files")
        # Example: running_terminals = get_emacs_func_result("emigo--get-running-terminals")

        details += "</environment_details>"
        return details

    def _parse_tool_use(self, response_text: str) -> Optional[Tuple[str, Dict[str, str]]]:
        """Parses the LLM response XML for the *first* valid tool use, ignoring thinking tags."""
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

            # 3. Iterate through blocks to find the first *known* tool
            for tool_name, tool_content in potential_blocks:
                if tool_name in known_tools:
                    # Found a valid tool, parse its parameters
                    params = {}
                    param_matches = re.findall(r"<([a-zA-Z0-9_]+)>(.*?)</\1>", tool_content, re.DOTALL)
                    for param_name, param_value in param_matches:
                        params[param_name] = param_value.strip() # Strip whitespace

                    print(f"Parsed tool use: {tool_name} with params: {params}", file=sys.stderr)
                    return tool_name, params
                else:
                    # This block is not a known tool (e.g., <thinking>), ignore it and continue searching
                    print(f"Ignoring non-tool XML block: <{tool_name}>", file=sys.stderr)
                    continue # Check the next potential block

            # 4. If loop completes without finding a known tool
            print("No known tool use found in the response.", file=sys.stderr)
            return None

        except Exception as e:
            print(f"Error parsing tool use: {e}\nText: {response_text}", file=sys.stderr)
            return None

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
            return self._format_tool_result(f"{TOOL_RESULT_SUCCESS}\n<file_content path=\"{posix_rel_path}\">\n{content}\n</file_content>")
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
            final_content = get_emacs_func_result("read-file-content-sync", abs_path)

            return self._format_tool_result(
                f"{TOOL_RESULT_SUCCESS}\nFile '{posix_rel_path}' written successfully.\n\n"
                f"<final_file_content path=\"{posix_rel_path}\">\n{final_content}\n</final_file_content>\n\n"
                f"IMPORTANT: For any future changes to this file, use the final_file_content shown above as your reference."
            )
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
                     final_content = result[1]
                     return self._format_tool_result(
                         f"{TOOL_RESULT_SUCCESS}\nFile '{posix_rel_path}' modified successfully.\n\n"
                         f"<final_file_content path=\"{posix_rel_path}\">\n{final_content}\n</final_file_content>\n\n"
                         f"IMPORTANT: For any future changes to this file, use the final_file_content shown above as your reference."
                     )

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
            repo_map_content = self.repo_mapper.generate_map(chat_files=chat_files)
            if not repo_map_content:
                repo_map_content = "(No map content generated)"
            return self._format_tool_result(f"{TOOL_RESULT_SUCCESS}\n# Repository Map for {self.session_path.replace(os.sep, '/')}\n{repo_map_content}")
        except Exception as e:
            print(f"Error generating repomap: {e}", file=sys.stderr)
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
            # Get history *copy* to modify for this interaction run
            current_history = list(self.llm_client.get_history())

            # Add initial prompt to history for this run
            current_history.append({"role": "user", "content": initial_user_prompt})

            max_turns = 10 # Limit turns to prevent infinite loops
            for turn in range(max_turns):
                print(f"\n--- Agent Turn {turn + 1}/{max_turns} (Session: {os.path.basename(self.session_path)}) ---", file=sys.stderr)

                # --- Build Prompt ---
                messages = [{"role": "system", "content": system_prompt}]
                messages.extend(current_history) # Use the history for this run

                # Add environment details and previous tool result to the *last* user message
                last_user_message_index = -1
                try:
                    # Find the index of the last message with role 'user'
                    last_user_message_index = next(i for i, msg in enumerate(reversed(messages)) if msg["role"] == "user")
                    last_user_message_index = len(messages) - 1 - last_user_message_index
                except StopIteration:
                     print("Error: No user message found in history to append context to.", file=sys.stderr)
                     # If this happens, something is wrong with history management
                     eval_in_emacs("emigo--flush-buffer", self.session_path, "[Internal Error: History state invalid]", "error")
                     break # Exit loop

                context_to_add = ""
                if self.current_tool_result:
                    # Append the result from the *previous* turn
                    context_to_add += f"\n\n{self.current_tool_result}"
                    self.current_tool_result = None # Consume the result

                # Add environment details (repo map, etc.)
                # TODO: Consider adding this only on the first turn or when context changes significantly?
                environment_details = self._get_environment_details()
                context_to_add += f"\n\n{environment_details}"

                # Modify the content of the last user message in the *local* messages list
                messages[last_user_message_index]["content"] += context_to_add

                # --- Send to LLM (Streaming) ---
                full_response = ""
                eval_in_emacs("emigo--flush-buffer", self.session_path, "\nAssistant:\n", "llm", True) # Signal start
                try:
                    # Use the locally built messages list
                    response_stream = self.llm_client.send(messages, stream=True)
                    for chunk in response_stream:
                        eval_in_emacs("emigo--flush-buffer", self.session_path, str(chunk), "llm", False)
                        full_response += chunk

                except Exception as e:
                    error_message = f"[Error during LLM communication: {e}]"
                    print(f"\n{error_message}", file=sys.stderr)
                    eval_in_emacs("emigo--flush-buffer", self.session_path, str(error_message), "error", False)
                    # Add error to local history for this run
                    current_history.append({"role": "assistant", "content": error_message})
                    break # Exit loop on error

                # Add assistant's full response to local history *before* tool processing
                current_history.append({"role": "assistant", "content": full_response})

                # --- Parse and Execute Tool ---
                tool_info = self._parse_tool_use(full_response)

                if tool_info:
                    tool_name, params = tool_info
                    tool_result = self._execute_tool(tool_name, params)

                    if tool_result == "COMPLETION_SIGNALLED":
                        print("Completion signalled. Ending interaction.", file=sys.stderr)
                        # Don't add completion signal to history, just break
                        break # Exit loop

                    # Store result for the next turn's prompt
                    self.current_tool_result = tool_result
                    # Add tool result message to local history for the LLM's next turn
                    current_history.append({"role": "user", "content": tool_result})

                    # If the tool was denied, stop the loop for this interaction
                    if tool_result == self._format_tool_result(TOOL_DENIED):
                        print("Tool denied, ending current interaction.", file=sys.stderr)
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

            # --- IMPORTANT: Update the persistent history in LLMClient ---
            # Only update if the interaction didn't end abruptly due to a critical error before history could be appended
            self.llm_client.set_history(current_history)

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
        """Search files for text patterns using Python's built-in capabilities."""
        rel_path = params.get("path", ".")  # Default to current directory
        pattern = params.get("pattern")
        case_sensitive = params.get("case_sensitive", "false").lower() == "true"
        max_matches = min(100, int(params.get("max_matches", "20")))  # Cap at 100 matches

        if not pattern:
            return self._format_tool_error("Missing 'pattern' parameter")

        abs_path = os.path.abspath(os.path.join(self.session_path, rel_path))
        try:
            if not os.path.exists(abs_path):
                return self._format_tool_error(f"Path not found: {rel_path}")

            matches = []
            flags = 0 if case_sensitive else re.IGNORECASE
            compiled_pattern = re.compile(pattern, flags)

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
            return self._format_tool_error(f"Error searching files: {e}")
