#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tool Implementations for the Emigo Agent.

This module defines the concrete Python functions that correspond to the tools
the LLM agent can request (as defined in `system_prompt.py`). These functions
are dispatched by the main `emigo.py` process after receiving a tool request
from the `llm_worker.py` and potentially obtaining user approval via Emacs.

Each tool function receives the relevant `Session` object (providing access to
session state like the root path and caches) and a dictionary of parameters
extracted from the LLM's request.

Tools interact with the user's environment primarily by:
- Calling back to Emacs functions via `utils.py` (e.g., for executing commands,
  replacing text in buffers, asking questions).
- Interacting with the file system within the session's directory.
- Modifying the session state (e.g., adding files to context, updating caches).

Each tool function returns a string result formatted for the LLM, indicating
success (often with output) or failure (with an error message).
"""

import os
import sys
import json
import re
import traceback
import difflib
from typing import Dict, List, Tuple, Optional

# Import Session class for type hinting and accessing session state
from session import Session
# Import utilities for calling Emacs and file reading
from utils import get_emacs_func_result, eval_in_emacs, read_file_content
# Import system prompt constants for standard messages/prefixes
from system_prompt import (
    TOOL_RESULT_SUCCESS, TOOL_RESULT_OUTPUT_PREFIX,
    TOOL_REPLACE_IN_FILE, TOOL_DENIED, TOOL_ERROR_PREFIX, TOOL_ERROR_SUFFIX
)

# --- Helper Functions ---

def _format_tool_result(result_content: str) -> str:
    """Formats a successful tool result."""
    # Simple format for now
    return f"{TOOL_RESULT_SUCCESS}\n{result_content}"

def _format_tool_error(error_message: str) -> str:
    """Formats a tool error message using standard prefixes/suffixes."""
    return f"{TOOL_ERROR_PREFIX}{error_message}{TOOL_ERROR_SUFFIX}"

def _resolve_path(session_path: str, rel_path: str) -> str:
    """Resolves a relative path within the session path."""
    return os.path.abspath(os.path.join(session_path, rel_path))

def _posix_path(path: str) -> str:
    """Converts a path to use POSIX separators."""
    return path.replace(os.sep, '/')

# --- Tool Implementations ---

def execute_command(session: Session, params: Dict[str, str]) -> str:
    """Executes a shell command via Emacs."""
    command = params.get("command")
    if not command:
        return _format_tool_error("Missing required parameter 'command'")

    try:
        print(f"Executing command: {command} in {session.session_path}", file=sys.stderr)
        # Use synchronous call to Emacs to run command and get result
        output = get_emacs_func_result("execute-command-sync", session.session_path, command)
        return _format_tool_result(f"{TOOL_RESULT_OUTPUT_PREFIX}{output}")
    except Exception as e:
        print(f"Error executing command '{command}' via Emacs: {e}", file=sys.stderr)
        return _format_tool_error(f"Error executing command: {e}")

def read_file(session: Session, params: Dict[str, str]) -> str:
    """Reads a file, adds it to context, and updates the session cache."""
    rel_path = params.get("path")
    if not rel_path:
        return _format_tool_error("Missing required parameter 'path'")

    abs_path = _resolve_path(session.session_path, rel_path)
    posix_rel_path = _posix_path(rel_path)

    try:
        if not os.path.isfile(abs_path):
             return _format_tool_error(f"File not found: {posix_rel_path}")

        # Add file to context list (Session class handles duplicates)
        added, add_msg = session.add_file_to_context(abs_path) # Use abs_path here
        if added:
            print(add_msg, file=sys.stderr)
            eval_in_emacs("message", f"[Emigo] {add_msg}") # Notify Emacs

        # Session._update_file_cache (called by add_file_to_context or get_cached_content)
        # handles reading and caching. We just need to ensure it's in context.
        # Force a cache update/read if it wasn't already added.
        if not added:
            session._update_file_cache(rel_path)

        # Return success message; content is now cached for environment details
        return _format_tool_result(f"File '{posix_rel_path}' read and added to context.")
    except Exception as e:
        print(f"Error reading file '{rel_path}': {e}", file=sys.stderr)
        session.invalidate_cache(rel_path) # Invalidate cache on error
        return _format_tool_error(f"Error reading file: {e}")

def write_to_file(session: Session, params: Dict[str, str]) -> str:
    """Writes content to a file and updates the session cache."""
    rel_path = params.get("path")
    content = params.get("content")
    if not rel_path:
        return _format_tool_error("Missing required parameter 'path'")
    if content is None: # Allow empty string content
        return _format_tool_error("Missing required parameter 'content'")

    abs_path = _resolve_path(session.session_path, rel_path)
    posix_rel_path = _posix_path(rel_path)

    try:
        # Ensure parent directory exists
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)

        # Write the file directly
        with open(abs_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Written content to {abs_path}", file=sys.stderr)

        # Inform Emacs about the change so it can prompt user to revert if needed
        eval_in_emacs("emigo--file-written-externally", abs_path)

        # Update session cache with the written content
        session._update_file_cache(rel_path, content=content)

        return _format_tool_result(f"File '{posix_rel_path}' written successfully.")

    except Exception as e:
        print(f"Error writing file '{rel_path}': {e}", file=sys.stderr)
        session.invalidate_cache(rel_path) # Invalidate cache on error
        return _format_tool_error(f"Error writing file: {e}")

def _parse_search_replace_blocks(diff_str: str) -> Tuple[List[Tuple[str, str]], Optional[str]]:
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


def replace_in_file(session: Session, params: Dict[str, str]) -> str:
    """Replaces content in a file using SEARCH/REPLACE blocks via Emacs."""
    rel_path = params.get("path")
    diff_str = params.get("diff")
    similarity_threshold = 0.85 # Configurable threshold (85%)

    if not rel_path:
        return _format_tool_error(f"Missing required parameter 'path' for {TOOL_REPLACE_IN_FILE}")
    if not diff_str:
        return _format_tool_error(f"Missing required parameter 'diff' (SEARCH/REPLACE block) for {TOOL_REPLACE_IN_FILE}")

    abs_path = os.path.abspath(os.path.join(session.session_path, rel_path))
    posix_rel_path = rel_path.replace(os.sep, '/')

    try:
        if not os.path.isfile(abs_path):
            return _format_tool_error(f"File not found: {rel_path}. Please ensure it's added to the chat first.")

        # --- Get File Content ---
        # Use the session's method to get cached content (updates if stale)
        file_content = session.get_cached_content(rel_path)
        if file_content is None:
            # If get_cached_content returns None, it means the file likely doesn't exist
            # or couldn't be read/cached previously.
            return _format_tool_error(f"Could not get content for file: {posix_rel_path}. It might not exist or be readable.")

        # Note: session.get_cached_content already handles reading if necessary.
        # The check below is redundant if get_cached_content works correctly,
        # but we keep it as a safeguard against potential error strings stored in cache.
        if file_content.startswith("# Error"): # Check if cached content is an error message
             return _format_tool_error(f"Cannot perform replacement. Cached content indicates a previous error for: {posix_rel_path}. Please use read_file again.")

        # --- Parse *All* Diff Blocks ---
        parsed_blocks, parse_error = _parse_search_replace_blocks(diff_str)
        if parse_error:
            return _format_tool_error(parse_error)
        if not parsed_blocks:
            return _format_tool_error("No valid SEARCH/REPLACE blocks found in the diff.")

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
                errors.append(
                    f"Block {i+1}: Could not find a sufficiently similar block (ratio {match_ratio:.2f} < {similarity_threshold:.2f}) "
                    f"for the SEARCH text in '{posix_rel_path}'.\n"
                    f"Closest match near line {error_line_num} (char index {error_char_index}):\n"
                    f"```\n{search_text}\nreplacing with\n{replace_text}```"
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
            error_header = f"Failed to apply replacements to '{posix_rel_path}' due to {len(errors)} error(s):\n{replace_text}"
            error_details = "\n\n".join(errors)
            # Suggest reading the file again
            error_footer = "\nPlease use read_file to get the exact current content and try again with updated SEARCH blocks."
            return _format_tool_error(error_header + error_details + error_footer)

        if not replacements_to_apply:
             return _format_tool_error("No replacements could be applied (all blocks failed matching or were empty).")


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
                # Success: Re-read content from Emacs and update session cache
                try:
                    updated_content = read_file_content(abs_path)
                    # Use session's method to update cache with new content
                    session._update_file_cache(rel_path, content=updated_content)
                    print(f"Updated session cache for '{rel_path}' after successful replacement.", file=sys.stderr)
                except Exception as read_err:
                    print(f"Warning: Failed to re-read file '{rel_path}' after replacement to update cache: {read_err}", file=sys.stderr)
                    # Invalidate cache entry on read error using session method
                    session.invalidate_cache(rel_path)
                    # Return success, but mention the cache issue
                    return _format_tool_result(f"{TOOL_RESULT_SUCCESS}\nFile '{posix_rel_path}' modified successfully by applying {len(replacements_to_apply)} block(s).\n(Warning: Could not update session cache after modification.)")

                return _format_tool_result(f"{TOOL_RESULT_SUCCESS}\nFile '{posix_rel_path}' modified successfully by applying {len(replacements_to_apply)} block(s).")
            else:
                # Elisp returned an error
                error_detail = str(result) if result else "Unknown error during multi-replacement in Emacs."
                print(f"Error applying multi-replacement via Elisp to '{rel_path}': {error_detail}", file=sys.stderr)
                return _format_tool_error(
                    f"Error applying replacements in Emacs: {error_detail}\n\n"
                    f"File: {posix_rel_path}\n"
                    f"Please check the Emacs *Messages* buffer for details."
                )
        except Exception as elisp_call_err:
             print(f"Error calling Elisp function 'replace-regions-sync' for '{rel_path}': {elisp_call_err}\n{traceback.format_exc()}", file=sys.stderr)
             return _format_tool_error(f"Error communicating with Emacs for replacement: {elisp_call_err}")

    except Exception as e:
        print(f"Error during replace_in_file for '{rel_path}': {e}\n{traceback.format_exc()}", file=sys.stderr)
        return _format_tool_error(f"Error processing replacement for {posix_rel_path}: {e}")


def ask_followup_question(session: Session, params: Dict[str, str]) -> str:
    """Asks the user a question via Emacs."""
    question = params.get("question")
    options_str = params.get("options") # Optional: "[Option1, Option2]" as JSON string
    if not question:
        return _format_tool_error("Missing required parameter 'question'")

    try:
        # Validate and prepare options JSON string
        valid_options_str = "[]"
        if options_str:
            try:
                parsed_options = json.loads(options_str)
                if isinstance(parsed_options, list):
                    valid_options_str = options_str # Use original if valid list
                else:
                    print(f"Warning: Invalid format for options, expected JSON array string: {options_str}", file=sys.stderr)
            except json.JSONDecodeError:
                print(f"Warning: Invalid JSON for options: {options_str}", file=sys.stderr)

        # Ask Emacs to present the question and get the user's answer (synchronous)
        answer = get_emacs_func_result("ask-user-sync", session.session_path, question, valid_options_str)

        if answer is None or answer == "": # Check for nil or empty string from Emacs
            # User likely cancelled or provided no input
            print("User cancelled or provided no answer to followup question.", file=sys.stderr)
            return TOOL_DENIED # Use standard denial message
        else:
            # Wrap answer for clarity in the LLM prompt
            return _format_tool_result(f"<answer>\n{answer}\n</answer>")
    except Exception as e:
        print(f"Error asking followup question via Emacs: {e}", file=sys.stderr)
        return _format_tool_error(f"Error asking question: {e}")

def attempt_completion(session: Session, params: Dict[str, str]) -> str:
    """Signals completion to Emacs."""
    result_text = params.get("result")
    command = params.get("command") # Optional command to demonstrate

    if result_text is None: # Allow empty string result
        return _format_tool_error("Missing required parameter 'result'")

    try:
        # Signal completion to Emacs (asynchronous is fine here)
        eval_in_emacs("emigo--signal-completion", session.session_path, result_text, command or "")
        # This tool use itself doesn't return content to the LLM, it ends the loop.
        # Return a special marker that the main process/worker can check.
        return "COMPLETION_SIGNALLED"
    except Exception as e:
        print(f"Error signalling completion to Emacs: {e}", file=sys.stderr)
        return _format_tool_error(f"Error signalling completion: {e}")

def list_repomap(session: Session, params: Dict[str, str]) -> str:
    """Generates and caches the repository map."""
    try:
        chat_files = session.get_chat_files()
        print(f"Generating repomap for {session.session_path} with chat files: {chat_files}", file=sys.stderr)
        # Use the session's RepoMapper instance
        repo_map_content = session.repo_mapper.generate_map(chat_files=chat_files) # Pass chat files
        if not repo_map_content:
            repo_map_content = "(No map content generated)"

        # Store the generated map content in the session cache
        session.set_last_repomap(repo_map_content)

        # Return success message; map content is cached for environment details
        return _format_tool_result(f"Repository map generated for {_posix_path(session.session_path)}.")
    except Exception as e:
        print(f"Error generating repomap: {e}\n{traceback.format_exc()}", file=sys.stderr)
        session.set_last_repomap(None) # Clear stored map on error
        return _format_tool_error(f"Error generating repository map: {e}")

def list_files(session: Session, params: Dict[str, str]) -> str:
    """Lists files in a directory via Emacs."""
    rel_path = params.get("path", ".") # Default to session path root
    recursive = params.get("recursive", "false").lower() == "true"

    abs_path = _resolve_path(session.session_path, rel_path)
    posix_rel_path = _posix_path(rel_path)
    try:
        # Use Emacs function to list files respecting ignores etc.
        files_str = get_emacs_func_result("list-files-sync", abs_path, recursive)
        # Elisp function should return a newline-separated string of relative paths

        return _format_tool_result(
            f"Files in '{posix_rel_path}' ({'recursive' if recursive else 'non-recursive'}):\n{files_str}"
        )
    except Exception as e:
        print(f"Error listing files via Emacs: {e}", file=sys.stderr)
        return _format_tool_error(f"Error listing files: {e}")

def search_files(session: Session, params: Dict[str, str]) -> str:
    """Searches files using Emacs's capabilities."""
    rel_path = params.get("path", ".")
    pattern = params.get("pattern")
    case_sensitive = params.get("case_sensitive", "false").lower() == "true"
    # Use a slightly larger default, capped reasonably
    max_matches = min(200, int(params.get("max_matches", "50")))

    if not pattern:
        return _format_tool_error("Missing 'pattern' parameter.")

    abs_path = _resolve_path(session.session_path, rel_path)
    posix_rel_path = _posix_path(rel_path)

    try:
        # Call Emacs function to perform the search
        search_results = get_emacs_func_result(
            "search-files-sync", abs_path, pattern, case_sensitive, max_matches
        )

        if not search_results or search_results.strip() == "":
             return _format_tool_result(f"No matches found for pattern: {pattern} in '{posix_rel_path}'")

        result = f"Found matches for pattern '{pattern}' in '{posix_rel_path}':\n{search_results}"
        # Elisp function should ideally handle truncation notes if applicable

        return _format_tool_result(result)

    except Exception as e:
        print(f"Error searching files via Emacs: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return _format_tool_error(f"Error searching files: {e}")

# --- Tool Dispatcher ---

# Map tool names (from system_prompt.py) to implementation functions
TOOL_HANDLER_MAP = {
    "execute_command": execute_command,
    "read_file": read_file,
    "write_to_file": write_to_file,
    "replace_in_file": replace_in_file,
    "ask_followup_question": ask_followup_question,
    "attempt_completion": attempt_completion,
    "list_repomap": list_repomap,
    "list_files": list_files,
    "search_files": search_files,
}

def dispatch_tool(session: Session, tool_name: str, params: Dict[str, str]) -> str:
    """Finds and calls the appropriate tool implementation."""
    handler = TOOL_HANDLER_MAP.get(tool_name)
    if not handler:
        print(f"Unknown tool requested: {tool_name}", file=sys.stderr)
        return _format_tool_error(f"Unknown tool: {tool_name}")

    try:
        # Call the handler function, passing the session object and params
        return handler(session, params)
    except Exception as e:
        # Catch errors within the handler itself
        print(f"Error during execution of tool '{tool_name}': {e}\n{traceback.format_exc()}", file=sys.stderr)
        return _format_tool_error(f"Error executing tool '{tool_name}': {e}")
