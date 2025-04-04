#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Defines the structure for tools and registers available tools for Emigo.

This module provides:
- TypedDict definitions for ToolParameter and ToolDefinition.
- Concrete definitions for each available tool, linking to their implementation
  in tools.py.
- A TOOL_REGISTRY dictionary for easy access to all tool definitions.
- Helper functions to retrieve tool definitions.
"""

from typing import Callable, Dict, List, TypedDict, Literal, Any, Optional
# Import tool implementation functions from tools.py
from tools import (
    execute_command,
    read_file,
    write_to_file,
    replace_in_file,
    search_files,
    list_files,
    list_repomap,
    ask_followup_question,
    attempt_completion
)

# --- Type Definitions ---

class ToolParameter(TypedDict):
    """Defines the structure for a single tool parameter."""
    name: str
    type: Literal["string", "integer", "boolean", "number", "array", "object"] # JSON Schema types
    description: str
    required: bool
    # Optional fields for complex types (future enhancement)
    # items: Optional[Dict] # For array type
    # properties: Optional[Dict[str, Dict]] # For object type

class ToolDefinition(TypedDict):
    """Defines the structure for a single tool."""
    name: str
    description: str
    parameters: List[ToolParameter]
    function: Callable[..., str] # Function signature: (session: Session, parameters: Dict[str, Any]) -> str

# --- Tool Definitions ---

# Define each tool using the ToolDefinition structure

EXECUTE_COMMAND_TOOL = ToolDefinition(
    name="execute_command",
    description="Request to execute a CLI command on the system. Use this when you need to perform system operations or run specific commands to accomplish any step in the user's task. You must tailor your command to the user's system and provide a clear explanation of what the command does. For command chaining, use the appropriate chaining syntax for the user's shell. Prefer to execute complex CLI commands over creating executable scripts, as they are more flexible and easier to run.",
    parameters=[
        ToolParameter(name="command", type="string", description="The shell command to execute.", required=True),
        # Note: requires_approval is handled internally in emigo.py based on tool name, not an LLM param.
    ],
    function=execute_command
)

READ_FILE_TOOL = ToolDefinition(
    name="read_file",
    description="Request to read the contents of a file at the specified path. Use this tool *only* when the user has explicitly instructed you to read a specific file path or you have already used list_repomap and identified this specific file as necessary for the next step. Do NOT use this tool based on guesses about where functionality might reside; use list_repomap first in such cases. Use this tool if the file's content is not already present in <environment_details>. Reading a file will add its content to <environment_details> for subsequent turns. May not be suitable for other types of binary files, as it returns the raw content as a string.",
    parameters=[
        ToolParameter(name="path", type="string", description="The relative path of the file to read.", required=True),
    ],
    function=read_file
)

WRITE_TO_FILE_TOOL = ToolDefinition(
    name="write_to_file",
    description="Request to write content to a file at the specified path. If the file exists, it will be overwritten with the provided content. If the file doesn't exist, it will be created. This tool will automatically create any directories needed to write the file.",
    parameters=[
        ToolParameter(name="path", type="string", description="The relative path of the file to write.", required=True),
        ToolParameter(name="content", type="string", description="The complete content to write to the file.", required=True),
    ],
    function=write_to_file
)

REPLACE_IN_FILE_TOOL = ToolDefinition(
    name="replace_in_file",
    description="Request to replace sections of content in an existing file using SEARCH/REPLACE blocks that define exact changes to specific parts of the file. This tool should be used when you need to make targeted changes to specific parts of a file.",
    parameters=[
        ToolParameter(name="path", type="string", description="The relative path of the file to modify.", required=True),
        ToolParameter(name="diff", type="string", description="""
One or more SEARCH/REPLACE blocks following this exact format:
````
<<<<<<< SEARCH
[exact content to find]
=======
[new content to replace with]
>>>>>>> REPLACE
````
Critical rules:
1. SEARCH content must match the associated file section to find EXACTLY:
   * Match character-for-character including whitespace, indentation, line endings
   * Include all comments, docstrings, etc.
2. SEARCH/REPLACE blocks will ONLY replace the first match occurrence.
   * Including multiple unique SEARCH/REPLACE blocks if you need to make multiple changes.
   * Include *just* enough lines in each SEARCH section to uniquely match each set of lines that need to change.
   * When using multiple SEARCH/REPLACE blocks, list them in the order they appear in the file.
3. Keep SEARCH/REPLACE blocks concise:
   * Break large SEARCH/REPLACE blocks into a series of smaller blocks that each change a small portion of the file.
   * Include just the changing lines, and a few surrounding lines if needed for uniqueness.
   * Do not include long runs of unchanging lines in SEARCH/REPLACE blocks.
   * Each line must be complete. Never truncate lines mid-way through as this can cause matching failures.
4. Special operations:
   * To move code: Use two SEARCH/REPLACE blocks (one to delete from original + one to insert at new location)
   * To delete code: Use empty REPLACE section""", required=True),
    ],
    function=replace_in_file
)

SEARCH_FILES_TOOL = ToolDefinition(
    name="search_files",
    description="Request to perform a regex search across files in a specified directory, providing context-rich results. This tool searches for patterns or specific content across multiple files, displaying each match with its line number and the line content.",
    parameters=[
        ToolParameter(name="path", type="string", description="The path of the directory to search in (relative to the session directory). This directory will be recursively searched.", required=True),
        ToolParameter(name="pattern", type="string", description="The regular expression pattern to search for. Uses Python regex syntax. Ensure the pattern is correctly escaped if needed.", required=True),
        ToolParameter(name="case_sensitive", type="boolean", description="Whether the search should be case-sensitive (default: false).", required=False),
        ToolParameter(name="max_matches", type="integer", description="Maximum number of matches to return (default: 20, max: 200).", required=False),
    ],
    function=search_files
)

LIST_FILES_TOOL = ToolDefinition(
    name="list_files",
    description="Request to list files and directories within the specified directory. If recursive is true, it will list all files and directories recursively. If recursive is false or not provided, it will only list the top-level contents. Do not use this tool to confirm the existence of files you may have created, as the user will let you know if the files were created successfully or not.",
    parameters=[
        ToolParameter(name="path", type="string", description="The relative path of the directory to list.", required=True),
        ToolParameter(name="recursive", type="boolean", description="Whether to list files recursively (default: false).", required=False),
    ],
    function=list_files
)

LIST_REPOMAP_TOOL = ToolDefinition(
    name="list_repomap",
    description="Request a high-level summary of the codebase structure within the session directory. This tool analyzes the source code files (respecting .gitignore and avoiding binary/ignored files) and extracts key definitions (classes, functions, methods, variables, etc.) along with relevant code snippets showing their usage context. It uses a ranking algorithm (PageRank) to prioritize the most important and interconnected parts of the code, especially considering files already discussed or mentioned. This provides a concise yet informative overview, far more useful than a simple file listing (list_files) or reading individual files (read_file) when you need to understand the project's architecture, identify where specific functionality resides, or plan complex changes. **When unsure where functionality resides or how code is structured, you MUST use list_repomap first.** It is much more efficient and context-aware than guessing file paths and using read_file sequentially. Use list_repomap to get a map of the relevant code landscape before diving into specific files. The analysis focuses on the source files within the session directory. The result of this tool will be added to the <environment_details> for subsequent turns.",
    parameters=[], # No parameters for list_repomap
    function=list_repomap
)

ASK_FOLLOWUP_QUESTION_TOOL = ToolDefinition(
    name="ask_followup_question",
    description="Ask the user a question to gather additional information needed to complete the task. This tool should be used when you encounter ambiguities, need clarification, or require more details to proceed effectively. It allows for interactive problem-solving by enabling direct communication with the user. Use this tool judiciously to maintain a balance between gathering necessary information and avoiding excessive back-and-forth.",
    parameters=[
        ToolParameter(name="question", type="string", description="The question to ask the user.", required=True),
        ToolParameter(name="options", type="array", description="Optional array of 2-5 string options for the user to choose from.", required=False),
    ],
    function=ask_followup_question
)

ATTEMPT_COMPLETION_TOOL = ToolDefinition(
    name="attempt_completion",
    description="Use this tool ONLY when you have successfully completed all steps required by the user's request. After using a tool like `replace_in_file` or `write_to_file`, analyze the result: if the change successfully fulfills the user's request, use this tool to present the final result. Do not attempt further refinements unless explicitly asked. Optionally, provide a CLI command to demonstrate the result. The user may provide feedback if unsatisfied, which you can use to make improvements and try again.",
    parameters=[
        ToolParameter(name="result", type="string", description="The final result description.", required=True),
        ToolParameter(name="command", type="string", description="Optional CLI command to demonstrate the result.", required=False),
    ],
    function=attempt_completion
)

# --- Tool Registry ---

TOOL_REGISTRY: Dict[str, ToolDefinition] = {
    tool['name']: tool for tool in [
        EXECUTE_COMMAND_TOOL,
        READ_FILE_TOOL,
        WRITE_TO_FILE_TOOL,
        REPLACE_IN_FILE_TOOL,
        SEARCH_FILES_TOOL,
        LIST_FILES_TOOL,
        LIST_REPOMAP_TOOL,
        ASK_FOLLOWUP_QUESTION_TOOL,
        ATTEMPT_COMPLETION_TOOL,
    ]
}

# --- Helper Functions ---

def get_tool(name: str) -> Optional[ToolDefinition]:
    """Retrieves a tool definition by name."""
    return TOOL_REGISTRY.get(name)

def get_all_tools() -> List[ToolDefinition]:
    """Retrieves a list of all registered tool definitions."""
    return list(TOOL_REGISTRY.values())

# --- Tool Name Constants (Redundant if importing from system_prompt, but useful here) ---
# These should match the 'name' field in the definitions above
TOOL_EXECUTE_COMMAND = "execute_command"
TOOL_READ_FILE = "read_file"
TOOL_WRITE_TO_FILE = "write_to_file"
TOOL_REPLACE_IN_FILE = "replace_in_file"
TOOL_SEARCH_FILES = "search_files"
TOOL_LIST_FILES = "list_files"
TOOL_LIST_REPOMAP = "list_repomap"
TOOL_ASK_FOLLOWUP_QUESTION = "ask_followup_question"
TOOL_ATTEMPT_COMPLETION = "attempt_completion"
