#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Handles formatting tool definitions for specific LLM providers.

Currently supports formatting for OpenAI's function/tool calling API.
Can be extended to support other providers like Anthropic, Google Gemini, etc.
"""

from typing import List, Dict, Any
from tool_definitions import ToolDefinition

def format_tools_for_openai(tools: List[ToolDefinition]) -> List[Dict[str, Any]]:
    """
    Formats a list of ToolDefinition objects into the JSON structure
    expected by OpenAI's Chat Completions API (for tool_choice='auto').
    """
    openai_tools = []
    for tool in tools:
        properties = {}
        required_params = []
        for param in tool.get('parameters', []): # Use .get for safety
            # Basic JSON schema type mapping
            param_type = param.get('type', 'string') # Default to string if missing
            properties[param['name']] = {
                "type": param_type,
                "description": param.get('description', '') # Default description
            }
            if param_type == "array":
                properties[param['name']]['items'] = param.get('items', {"type": "string"})

            if param.get('required', False): # Default to not required
                required_params.append(param['name'])

        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.get('name', 'unknown_tool'), # Default name
                "description": tool.get('description', ''), # Default description
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required_params
                }
            }
        })
    return openai_tools

# --- Add formatters for other providers as needed ---
# def format_tools_for_anthropic(tools: List[ToolDefinition]) -> List[Dict[str, Any]]:
#     # Implementation for Anthropic's tool format
#     pass

# def format_tools_for_google(tools: List[ToolDefinition]) -> List[Dict[str, Any]]:
#     # Implementation for Google Gemini's tool format
#     pass

# --- Provider Selection Logic (Example) ---
# You might have logic elsewhere to choose the correct formatter based on the LLM model name
def get_formatted_tools(tools: List[ToolDefinition], model_name: str) -> List[Dict[str, Any]]:
    """Selects the appropriate formatter based on the model name."""
    # Simple example: default to OpenAI format
    # Add more sophisticated logic if supporting multiple providers
    if "claude" in model_name.lower():
        # return format_tools_for_anthropic(tools)
        pass # Placeholder
    elif "gemini" in model_name.lower():
        # return format_tools_for_google(tools)
        pass # Placeholder
    else: # Default to OpenAI
        return format_tools_for_openai(tools)

    # Fallback if no specific provider matched
    return format_tools_for_openai(tools)
