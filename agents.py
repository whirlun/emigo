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
    TOOL_LIST_REPOMAP, TOOL_ASK_FOLLOWUP_QUESTION, TOOL_ATTEMPT_COMPLETION,
    TOOL_FIND_DEFINITION, TOOL_FIND_REFERENCES # Added new tool names
)
import tiktoken # For token counting in history truncation
import difflib # For fuzzy matching SEARCH blocks

from utils import (
    eval_in_emacs, message_emacs, get_command_result, get_os_name,
    get_emacs_var, get_emacs_vars, read_file_content,
    get_emacs_func_result
)
from config import IGNORED_DIRS


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
        # History truncation settings
        self.max_history_tokens = 8000  # Target max tokens for history (leaves room for response)
        self.min_history_messages = 3   # Always keep at least this many messages
        # Tokenizer for history management
        try:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")
            # Test the tokenizer works
            test_tokens = self.tokenizer.encode("test")
            if not test_tokens:
                raise ValueError("Tokenizer returned empty tokens")
        except Exception as e:
            print(f"Warning: Could not initialize tokenizer. Using simple character count fallback. Error: {e}", file=sys.stderr)
            self.tokenizer = None

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
        """Fetches environment details: repo map OR file listing. NO file contents."""
        details = "<environment_details>\n"
        details += f"# Session Directory\n{self.session_path.replace(os.sep, '/')}\n\n" # Use POSIX path

        # --- Repository Map / Basic File Listing ---
        # Regenerate the map if it exists to ensure freshness, otherwise show structure
        if self.last_repomap_content is not None:
            if self.verbose: print("Regenerating repository map for environment details...", file=sys.stderr)
            fresh_repomap_content = self.repo_mapper.generate_map()
            if not fresh_repomap_content:
                fresh_repomap_content = "(No map content generated)"
            self.last_repomap_content = fresh_repomap_content # Update the cache
            details += "# Repository Map (Refreshed)\n"
            details += f"```\n{self.last_repomap_content}\n```\n\n"
        else:
            # If repomap hasn't been generated yet, show recursive directory listing
            details += "# File/Directory Structure (use list_repomap tool for code summary)\n"
            try:
                structure_lines = []
                for root, dirnames, filenames in os.walk(self.session_path, topdown=True):
                    dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith('.')]
                    filenames = [f for f in filenames if not f.startswith('.')]

                    rel_root = os.path.relpath(root, self.session_path)
                    if rel_root == '.': rel_root = ''
                    else: rel_root = rel_root.replace(os.sep, '/') + '/'

                    level = rel_root.count('/')
                    if rel_root:
                         indent = '  ' * (level -1)
                         structure_lines.append(f"{indent}- {os.path.basename(rel_root[:-1])}/")

                    file_indent = '  ' * level
                    for filename in sorted(filenames):
                        structure_lines.append(f"{file_indent}- {filename}")

                if structure_lines:
                    details += "```\n" + "\n".join(structure_lines) + "\n```\n\n"
                else:
                    details += "(No relevant files or directories found)\n\n"
            except Exception as e:
                details += f"# Error listing files/directories: {str(e)}\n\n"

        # --- List Added Files ---
        # List the names of files currently in context, but not their content.
        chat_files_list = self.chat_files_ref.get(self.session_path, [])
        if chat_files_list:
            details += "# # Files Currently in Chat Context\n"
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

    def _parse_search_replace_blocks(self, diff_str: str) -> Tuple[List[Tuple[str, str]], Optional[str]]:
        """Parses *all* SEARCH/REPLACE blocks from a diff string.

        Args:
            diff_str: The string containing one or more SEARCH/REPLACE blocks.

        Returns:
            A tuple containing:
            - A list of (search_text, replace_text) tuples for each valid block found.
            - An error message string if parsing fails, otherwise None.
        """
        search_marker = "<<<<<<< SEARCH\n"
        divider_marker = "\n=======\n"
        replace_marker = "\n>>>>>>> REPLACE"
        blocks = []
        # Use regex to find all blocks non-greedily
        pattern = re.compile(
            re.escape(search_marker) +
            '(.*?)' + # Capture search text (non-greedy)
            re.escape(divider_marker) +
            '(.*?)' + # Capture replace text (non-greedy)
            re.escape(replace_marker),
            re.DOTALL # Allow '.' to match newlines
        )

        found_blocks = pattern.findall(diff_str)

        if not found_blocks:
            # Check for common markdown fence if no blocks found
            if "```" in diff_str and search_marker not in diff_str:
                 return [], "Diff content seems to be a markdown code block, not a SEARCH/REPLACE block."
            return [], "No valid SEARCH/REPLACE blocks found in the provided diff."

        for search_text, replace_text in found_blocks:
            # Basic validation: ensure markers are not nested within text itself in unexpected ways
            if search_marker in search_text or divider_marker in search_text or replace_marker in search_text or \
               search_marker in replace_text or divider_marker in replace_text or replace_marker in replace_text:
                # This is a simplistic check; complex nesting could still fool it.
                # Consider more robust parsing if needed.
                return [], "Detected malformed or nested SEARCH/REPLACE markers within a block's content."
            blocks.append((search_text, replace_text))

        return blocks, None

    def _handle_replace_in_file(self, params: Dict[str, str]) -> str:
        """Handles replacing content using fuzzy matching for multiple blocks."""
        rel_path = params.get("path")
        diff_str = params.get("diff") # Expecting one or more SEARCH/REPLACE blocks
        similarity_threshold = 0.85 # Configurable threshold (85%)

        if not rel_path:
            return self._format_tool_error(f"Missing required parameter 'path' for {TOOL_REPLACE_IN_FILE}")
        if not diff_str:
            return self._format_tool_error(f"Missing required parameter 'diff' (SEARCH/REPLACE block) for {TOOL_REPLACE_IN_FILE}")

        abs_path = os.path.abspath(os.path.join(self.session_path, rel_path))
        posix_rel_path = rel_path.replace(os.sep, '/')

        try:
            if not os.path.isfile(abs_path):
                return self._format_tool_error(f"File not found: {rel_path}. Please ensure it's added to the chat first.")

            # --- Get File Content ---
            # Try getting from cache first, assuming read_file or initial context load populated it
            file_content = self.chat_file_contents.get(rel_path)
            if file_content is None:
                # If not cached, read it now (consider if this should be an error instead)
                print(f"Reading file content for replace as it wasn't cached: {rel_path}", file=sys.stderr)
                try:
                    file_content = read_file_content(abs_path)
                    # Optionally update cache here if desired
                    # current_mtime = self.repo_mapper.repo_mapper.get_mtime(abs_path)
                    # if current_mtime:
                    #     self.chat_file_mtimes[rel_path] = current_mtime
                    #     self.chat_file_contents[rel_path] = file_content
                except Exception as read_err:
                    return self._format_tool_error(f"Error reading file content for replacement: {read_err}")

            if file_content.startswith("# Error"): # Check if cached content is an error message
                 return self._format_tool_error(f"Cannot perform replacement. Previous error reading file: {rel_path}. Please use read_file again.")

            # --- Parse *All* Diff Blocks ---
            parsed_blocks, parse_error = self._parse_search_replace_blocks(diff_str)
            if parse_error:
                return self._format_tool_error(parse_error)
            if not parsed_blocks:
                return self._format_tool_error("No valid SEARCH/REPLACE blocks found in the diff.")

            # --- Fuzzy Match Each Block Against Original Content ---
            file_lines = file_content.splitlines(keepends=True) # Keep endings for context snippets
            replacements_to_apply = [] # List of (start_line, elisp_end_line, replace_text)
            errors = []
            context_lines = 3 # For error reporting

            for i, (search_text, replace_text) in enumerate(parsed_blocks):
                if not search_text: # Cannot match empty string
                    errors.append(f"Block {i+1}: SEARCH block is empty.")
                    continue # Skip this block

                # Match directly against the full file content string
                matcher = difflib.SequenceMatcher(None, file_content, search_text, autojunk=False)
                match = matcher.find_longest_match(0, len(file_content), 0, len(search_text))

                # Calculate similarity ratio based on the text match
                # Use match.size and len(search_text) for a direct ratio of the longest common subsequence
                match_ratio = 0.0
                if len(search_text) > 0: # Avoid division by zero
                    # Ratio calculation based on the longest contiguous matching block found
                    match_ratio = match.size / len(search_text)
                    # Alternative: matcher.ratio() considers the whole strings including non-matching parts
                    # match_ratio = matcher.ratio()
                    # Let's stick with match.size / len(search_text) as it focuses on the quality of the best block found

                print(f"Block {i+1} Fuzzy match for '{rel_path}': Ratio: {match_ratio:.2f} (Chars: {match.size}/{len(search_text)}) at char index {match.a}", file=sys.stderr)

                if match_ratio < similarity_threshold:
                    # Provide context around the best (but failed) match character index
                    error_char_index = match.a
                    error_line_num = file_content.count('\n', 0, error_char_index) + 1
                    start_ctx_line_idx = max(0, error_line_num - 1 - context_lines)
                    end_ctx_line_idx = min(len(file_lines), error_line_num + context_lines)
                    context_snippet = "".join(file_lines[start_ctx_line_idx:end_ctx_line_idx])
                    errors.append(
                        f"Block {i+1}: Could not find a sufficiently similar block (ratio {match_ratio:.2f} < {similarity_threshold:.2f}) "
                        f"for the SEARCH text in '{posix_rel_path}'.\n"
                        f"Closest match near line {error_line_num} (char index {error_char_index}):\n"
                        f"```\n{context_snippet}\n```"
                    )
                else:
                    # Match Found - Calculate line numbers from character indices
                    start_char_index = match.a
                    end_char_index = match.a + match.size # Index after the last matched character

                    # Calculate 1-based start line
                    start_line = file_content.count('\n', 0, start_char_index) + 1

                    # Calculate 1-based line number containing the *last* character of the match
                    # Need to handle edge case where match.size is 0 (shouldn't happen if ratio > 0)
                    # or where the match ends exactly at the end of the file without a newline.
                    last_char_index = end_char_index - 1
                    if last_char_index < start_char_index: # Handle empty match case if it slips through
                         end_line_containing_last_char = start_line
                    else:
                         end_line_containing_last_char = file_content.count('\n', 0, last_char_index) + 1

                    # Elisp's delete-region uses an *exclusive* end point.
                    # To delete lines inclusively from start_line to end_line_containing_last_char,
                    # we need to provide Elisp with the line number *after* the last line to delete.
                    elisp_end_line = end_line_containing_last_char + 1

                    replacements_to_apply.append((start_line, elisp_end_line, replace_text))
                    # The print statement shows the *inclusive* line range being replaced for clarity
                    print(f"Block {i+1}: Match found >= threshold. Staging replacement for lines {start_line}-{elisp_end_line-1} (Elisp end: {elisp_end_line})")

            # --- Handle Errors or Proceed ---
            if errors:
                error_header = f"Failed to apply replacements to '{posix_rel_path}' due to {len(errors)} error(s):\n"
                error_details = "\n\n".join(errors)
                # Suggest reading the file again
                error_footer = "\nPlease use read_file to get the exact current content and try again with updated SEARCH blocks."
                return self._format_tool_error(error_header + error_details + error_footer)

            if not replacements_to_apply:
                 return self._format_tool_error("No replacements could be applied (all blocks failed matching or were empty).")

            # --- Call Elisp to Perform Multiple Replacements ---
            try:
                # Serialize the list of replacements to JSON for Elisp
                # Convert Python list to JSON array string that Elisp can parse
                replacements_json = json.dumps(replacements_to_apply)
                print(f"Requesting {len(replacements_to_apply)} replacements in '{posix_rel_path}' via Elisp.", file=sys.stderr)

                result = get_emacs_func_result("replace-regions-sync", abs_path, replacements_json)

                # --- Process Elisp Result ---
                if result is True or str(result).lower() == 't': # Check for elisp t
                    print(f"Elisp successfully applied {len(replacements_to_apply)} replacements to '{rel_path}'.", file=sys.stderr)
                    # Success: Re-read content from Emacs to update cache accurately
                    try:
                        updated_content = get_emacs_func_result("read-file-content-sync", abs_path)
                        self.chat_file_contents[rel_path] = updated_content
                        # Update mtime from Emacs? Or just use current time? Let's skip mtime update for now.
                        # current_mtime = self.repo_mapper.repo_mapper.get_mtime(abs_path) # Might be stale if Emacs saved async
                        # self.chat_file_mtimes[rel_path] = current_mtime or time.time()
                        print(f"Updated local cache for '{rel_path}' after successful replacement.", file=sys.stderr)
                    except Exception as read_err:
                        print(f"Warning: Failed to re-read file '{rel_path}' after replacement to update cache: {read_err}", file=sys.stderr)
                        # Invalidate cache entry on read error
                        if rel_path in self.chat_file_contents: del self.chat_file_contents[rel_path]
                        if rel_path in self.chat_file_mtimes: del self.chat_file_mtimes[rel_path]
                        # Return success, but mention the cache issue
                        return self._format_tool_result(f"{TOOL_RESULT_SUCCESS}\nFile '{posix_rel_path}' modified successfully by applying {len(replacements_to_apply)} block(s).\n(Warning: Could not update internal cache after modification.)")

                    return self._format_tool_result(f"{TOOL_RESULT_SUCCESS}\nFile '{posix_rel_path}' modified successfully by applying {len(replacements_to_apply)} block(s).")
                else:
                    # Elisp returned an error
                    error_detail = str(result) if result else "Unknown error during multi-replacement in Emacs."
                    print(f"Error applying multi-replacement via Elisp to '{rel_path}': {error_detail}", file=sys.stderr)
                    return self._format_tool_error(
                        f"Error applying replacements in Emacs: {error_detail}\n\n"
                        f"File: {posix_rel_path}\n"
                        f"Please check the Emacs *Messages* buffer for details."
                    )
            except Exception as elisp_call_err:
                 print(f"Error calling Elisp function 'replace-regions-sync' for '{rel_path}': {elisp_call_err}\n{traceback.format_exc()}", file=sys.stderr)
                 return self._format_tool_error(f"Error communicating with Emacs for replacement: {elisp_call_err}")

        except Exception as e:
            print(f"Error during replace_in_file for '{rel_path}': {e}\n{traceback.format_exc()}", file=sys.stderr)
            return self._format_tool_error(f"Error processing replacement for {posix_rel_path}: {e}")


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

    def _handle_find_definition(self, params: Dict[str, str]) -> str:
        """Find and display the definition snippet for a given symbol."""
        symbol = params.get("symbol")
        if not symbol:
            return self._format_tool_error(f"Missing required parameter 'symbol' for {TOOL_FIND_DEFINITION}")

        try:
            # Access the underlying RepoMap instance and its cache
            repo_map = self.repo_mapper.repo_mapper
            if not hasattr(repo_map, 'TAGS_CACHE'):
                 return self._format_tool_error("Tags cache is not available.")

            definitions = []
            # Iterate through the cache to find definitions matching the symbol
            # Note: This iterates through *all* cached files. Could be optimized if needed.
            for cache_key in repo_map.TAGS_CACHE.keys():
                try:
                    # Check if cache_key is a valid file path before proceeding
                    if not isinstance(cache_key, str) or not os.path.exists(cache_key):
                        continue # Skip non-path keys or non-existent files

                    cached_item = repo_map.TAGS_CACHE.get(cache_key)
                    if cached_item and isinstance(cached_item, dict) and "data" in cached_item:
                        tags = cached_item.get("data", [])
                        for tag in tags:
                            # Ensure tag is a valid Tag object before accessing attributes
                            # Need to import Tag from repomapper
                            from repomapper import Tag
                            if isinstance(tag, Tag) and tag.kind == "def" and tag.name == symbol:
                                definitions.append(tag)
                except Exception as cache_read_err:
                    # Log error reading specific cache entry but continue searching
                    print(f"Warning: Error reading cache entry for {cache_key}: {cache_read_err}", file=sys.stderr)
                    continue # Skip this entry

            if not definitions:
                return self._format_tool_result(f"{TOOL_RESULT_SUCCESS}\nNo definition found for symbol: {symbol}")

            # Group definitions by file and render snippets
            output = f"{TOOL_RESULT_SUCCESS}\nFound definition(s) for symbol '{symbol}':\n"
            from collections import defaultdict # Need to import defaultdict
            grouped_defs = defaultdict(list)
            for tag in definitions:
                grouped_defs[tag.rel_fname].append(tag)

            for rel_fname, tags in sorted(grouped_defs.items()):
                abs_fname = tags[0].fname # Get abs path from the first tag
                lois = sorted(list(set(tag.line for tag in tags if tag.line >= 0))) # Unique, sorted lines
                if lois: # Only render if we have valid line numbers
                    output += f"\n--- File: {rel_fname} ---\n"
                    rendered_tree = repo_map.render_tree(abs_fname, rel_fname, lois)
                    output += rendered_tree
                else:
                    # If no line numbers (e.g., only file-level defs), just list file
                    output += f"\n--- File: {rel_fname} (Definition likely at top level) ---\n"


            return self._format_tool_result(output)

        except Exception as e:
            print(f"Error finding definition for '{symbol}': {e}\n{traceback.format_exc()}", file=sys.stderr)
            return self._format_tool_error(f"Error finding definition for {symbol}: {e}")

    def _handle_find_references(self, params: Dict[str, str]) -> str:
        """Find and list all references to a given symbol."""
        symbol = params.get("symbol")
        if not symbol:
            return self._format_tool_error(f"Missing required parameter 'symbol' for {TOOL_FIND_REFERENCES}")

        try:
            # Access the underlying RepoMap instance and its cache
            repo_map = self.repo_mapper.repo_mapper
            if not hasattr(repo_map, 'TAGS_CACHE'):
                 return self._format_tool_error("Tags cache is not available.")

            references = []
            # Iterate through the cache to find references matching the symbol
            for cache_key in repo_map.TAGS_CACHE.keys():
                 try:
                    if not isinstance(cache_key, str) or not os.path.exists(cache_key):
                        continue

                    cached_item = repo_map.TAGS_CACHE.get(cache_key)
                    if cached_item and isinstance(cached_item, dict) and "data" in cached_item:
                        tags = cached_item.get("data", [])
                        for tag in tags:
                             # Need to import Tag from repomapper
                             from repomapper import Tag
                             if isinstance(tag, Tag) and tag.kind == "ref" and tag.name == symbol:
                                 references.append(tag)
                 except Exception as cache_read_err:
                    print(f"Warning: Error reading cache entry for {cache_key}: {cache_read_err}", file=sys.stderr)
                    continue

            if not references:
                return self._format_tool_result(f"{TOOL_RESULT_SUCCESS}\nNo references found for symbol: {symbol}")

            # Group references by file and line
            output = f"{TOOL_RESULT_SUCCESS}\nFound reference(s) for symbol '{symbol}':\n"
            from collections import defaultdict # Need to import defaultdict
            grouped_refs = defaultdict(list)
            for tag in references:
                # Use line number if available, otherwise indicate file-level ref
                line_info = f":{tag.line}" if tag.line >= 0 else " (file level)"
                grouped_refs[tag.rel_fname].append(line_info)

            for rel_fname, lines in sorted(grouped_refs.items()):
                output += f"\n- {rel_fname}\n"
                # Sort line numbers numerically if possible
                sorted_lines = sorted(lines, key=lambda x: int(x[1:]) if x[1:].isdigit() else float('inf'))
                for line_info in sorted_lines:
                    output += f"  - Line{line_info}\n"

            return self._format_tool_result(output)

        except Exception as e:
            print(f"Error finding references for '{symbol}': {e}\n{traceback.format_exc()}", file=sys.stderr)
            return self._format_tool_error(f"Error finding references for {symbol}: {e}")


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
            # TOOL_REPLACE_IN_FILE uses fuzzy matching now
            TOOL_REPLACE_IN_FILE: self._handle_replace_in_file,
            TOOL_ASK_FOLLOWUP_QUESTION: self._handle_ask_followup_question,
            TOOL_ATTEMPT_COMPLETION: self._handle_attempt_completion,
            TOOL_LIST_REPOMAP: self._handle_list_repomap,
            TOOL_LIST_FILES: self._handle_list_files,
            TOOL_SEARCH_FILES: self._handle_search_files,
            TOOL_FIND_DEFINITION: self._handle_find_definition, # Add new handler
            TOOL_FIND_REFERENCES: self._handle_find_references, # Add new handler
        }

        handler = handler_map.get(tool_name)
        if not handler:
            print(f"Unknown tool requested: {tool_name}", file=sys.stderr)
            return self._format_tool_error(f"Unknown tool: {tool_name}")

        # Define tools that require explicit approval (includes replace now)
        no_auto_approve_list = [
            TOOL_EXECUTE_COMMAND,
            TOOL_WRITE_TO_FILE,
            # Note: replace_in_file approval is handled implicitly by Emacs UI for now
        ]

        # Only request approval for tools in the no_auto_approve_list
        # Read file, list files/repomap, find def/refs, search are read-only.
        # Ask/Attempt completion interact directly.
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
                print(f"\n--- Agent Turn {turn + 1}/{max_turns} ---", file=sys.stderr)

                # --- Build Prompt with History Truncation ---
                full_history = self.llm_client.get_history() # List of (ts, msg_dict)

                # Always include system prompt
                messages_to_send = [{"role": "system", "content": system_prompt}]

                # Extract message dicts
                history_dicts = [msg_dict for _, msg_dict in full_history]

                # --- History Truncation: Keep messages within token limit ---
                messages_to_send.extend(self._truncate_history(history_dicts))

                # --- Append Environment Details ---
                # Append to the *last* message in the list being sent, which should be the user's latest prompt or tool result
                if messages_to_send[-1]["role"] == "user":
                    environment_details = self._get_environment_details()
                    # Use copy() to avoid modifying the history object directly
                    last_message_copy = messages_to_send[-1].copy()
                    last_message_copy["content"] += f"\n\n{environment_details}"
                    messages_to_send[-1] = last_message_copy # Replace the last message with the modified copy
                else:
                    # This case should ideally not happen if history alternates correctly,
                    # but add details as a separate system message if it does.
                    print("Warning: Last message before sending to LLM is not 'user'. Appending environment details as system message.", file=sys.stderr)
                    environment_details = self._get_environment_details()
                    messages_to_send.append({"role": "system", "content": environment_details})


                # --- Verbose Logging (Moved from LLMClient) ---
                if self.verbose:
                    print("\n--- Sending to LLM ---", file=sys.stderr)
                    # Avoid printing potentially large base64 images in verbose mode
                    printable_messages = []
                    for msg in messages_to_send:
                        if isinstance(msg.get("content"), list): # Handle image messages
                            new_content = []
                            for item in msg["content"]:
                                if isinstance(item, dict) and item.get("type") == "image_url":
                                     # Truncate base64 data for printing
                                     img_url = item.get("image_url", {}).get("url", "")
                                     if isinstance(img_url, str) and img_url.startswith("data:"):
                                         new_content.append({"type": "image_url", "image_url": {"url": img_url[:50] + "..."}})
                                     else:
                                         new_content.append(item) # Keep non-base64 or non-string URLs
                                else:
                                    new_content.append(item)
                            printable_messages.append({"role": msg["role"], "content": new_content})
                        else:
                            printable_messages.append(msg)

                    # Calculate approximate token count using self.tokenizer
                    token_count_str = ""
                    if self.tokenizer: # Check if tokenizer exists
                        try:
                            # Use litellm's utility if available, otherwise manual count
                            # Note: Need to access litellm via self.llm_client or import it here
                            # For simplicity, let's use the manual count with self.tokenizer
                            count = 0
                            for msg in messages_to_send:
                                 # Use json.dumps for consistent counting of structure
                                 count += self._count_tokens(json.dumps(msg))
                            token_count_str = f" (estimated {count} tokens)"
                        except Exception as e:
                            token_count_str = f" (token count error: {e})"
                    else:
                        token_count_str = " (tokenizer unavailable for count)"


                    print(json.dumps(printable_messages, indent=2), file=sys.stderr)
                    print(f"--- End LLM Request{token_count_str} ---", file=sys.stderr)

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

    def _truncate_history(self, history: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Truncate history to fit within token limits while preserving important messages."""
        if not history:
            return []

        # Always keep first user message for context
        truncated = [history[0]]
        current_tokens = self._count_tokens(truncated[0]["content"])

        # Add messages from newest to oldest until we hit the limit
        for msg in reversed(history[1:]):
            msg_tokens = self._count_tokens(msg["content"])
            if current_tokens + msg_tokens > self.max_history_tokens:
                if len(truncated) >= self.min_history_messages:
                    break
                # If we're below min messages, keep going but warn
                print(f"Warning: History exceeds token limit but below min message count", file=sys.stderr)

            truncated.insert(1, msg)  # Insert after first message
            current_tokens += msg_tokens

        if self.verbose and len(truncated) < len(history):
            print(f"History truncated from {len(history)} to {len(truncated)} messages ({current_tokens} tokens)", file=sys.stderr)

        return truncated

    def _count_tokens(self, text: str) -> int:
        """Count tokens in text using tokenizer or fallback method."""
        if not text:
            return 0

        if self.tokenizer:
            try:
                return len(self.tokenizer.encode(text))
            except Exception as e:
                print(f"Token counting error, using fallback: {e}", file=sys.stderr)

        # Fallback: approximate tokens as 4 chars per token
        return max(1, len(text) // 4)

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
