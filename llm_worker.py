#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LLM Interaction Worker Process.

This script runs as a separate process managed by `emigo.py`. Its primary
purpose is to isolate the potentially long-running and resource-intensive
Large Language Model (LLM) interactions and agent logic from the main Emigo
EPC server process.

Key Responsibilities:
- Listens for interaction requests (including prompt, history, config, context)
  from `emigo.py` via stdin.
- Initializes the `LLMClient` (from `llm.py`) and `Agent` (from `agent.py`)
  for each interaction request.
- Executes the main agentic loop: prepares prompts, calls the LLM, parses
  responses for tool usage.
- Streams LLM responses back to `emigo.py` via stdout.
- Sends requests for tool execution back to `emigo.py` via stdout and waits
  for results via stdin.
- Sends requests for updated environment details (like file contents or
  repository maps) back to `emigo.py` via stdout and waits for results via stdin.
- Reports completion status or errors back to `emigo.py` via stdout.
"""

import sys
import json
import time
import traceback
import os

from utils import _filter_environment_details
from llm import LLMClient
from agent import Agent
# Import tool definitions and provider formatting
from tool_definitions import get_all_tools
from llm_providers import get_formatted_tools
# Import constants used for tool results
from config import TOOL_DENIED, TOOL_ERROR_PREFIX

# Add project root to sys.path to allow importing other modules like llm, agent, utils
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# --- Communication Functions ---

def send_message(msg_type, session_path, **kwargs):
    """Sends a JSON message to stdout for the main process."""
    message = {"type": msg_type, "session": session_path, **kwargs}
    try:
        print(json.dumps(message), flush=True)
    except TypeError as e:
        # Handle potential non-serializable data in kwargs
        print(json.dumps({
            "type": "error",
            "session": session_path,
            "message": f"Serialization error: {e}. Data: {repr(kwargs)}"
        }), flush=True)
    except Exception as e:
        print(json.dumps({
            "type": "error",
            "session": session_path,
            "message": f"Error sending message: {e}"
        }), flush=True)


def request_tool_execution(session_path, tool_name, parameters_dict):
    """Sends a tool request with structured parameters and waits for the result."""
    request_id = f"tool_{time.time_ns()}" # Unique ID for the request
    # Send the parameters as a dictionary
    send_message("tool_request", session_path, request_id=request_id, tool_name=tool_name, parameters=parameters_dict)
    # Wait for the corresponding tool_result from stdin
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                # Main process likely closed stdin, worker should exit
                send_message("error", session_path, message="Stdin closed unexpectedly. Exiting.")
                sys.exit(1)
            response = json.loads(line)
            if response.get("type") == "tool_result" and response.get("request_id") == request_id:
                return response.get("result")
        except json.JSONDecodeError:
            send_message("error", session_path, message=f"Worker received invalid JSON from stdin: {line.strip()}")
            # Continue waiting, maybe the next line is valid
        except Exception as e:
            send_message("error", session_path, message=f"Error reading tool result from stdin: {e}")
            # Return an error state to the agent logic
            return f"<tool_error>Error receiving tool result: {e}</tool_error>"

# --- Agent Logic Adaptation ---

def handle_interaction_request(request):
    """Handles a single interaction request dictionary."""
    session_path = request.get("session_path")
    prompt = request.get("prompt")
    history = request.get("history", []) # List of (timestamp, message_dict)
    config = request.get("config", {})
    chat_files_list = request.get("chat_files", [])
    environment_details_str = request.get("environment_details", "<environment_details>\n# Error: Details not provided by main process.\n</environment_details>") # Get details from request

    if not all([session_path, prompt]):
        send_message("error", session_path or "unknown", message="Worker received incomplete request.")
        return

    # --- Initialize LLM Client ---
    # Get config from request data
    model_name = config.get("model")
    api_key = config.get("api_key")
    base_url = config.get("base_url")
    verbose = config.get("verbose", False)
    extra_headers = config.get("extra_headers", {})

    if not model_name:
        send_message("error", session_path, message="Missing 'model' in config.")
        return

    llm_client = LLMClient(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        verbose=verbose,
        extra_headers=extra_headers
    )
    # History is managed locally within this function now.

    # --- Initialize Agent (or adapt its logic) ---
    # Create a temporary chat_files dict for this request instance (still needed for Agent init?)
    # Agent class might not need chat_files_ref anymore if env details are pre-built.
    # Let's keep it for now in case Agent uses it for other things.
    # The main process owns the canonical chat_files state
    current_chat_files = {session_path: list(chat_files_list)}

    # NOTE: This creates a new Agent instance for *every* request because
    # the worker might be killed. If the worker were persistent, we might
    # reuse instances.
    # Environment details are fetched dynamically within the loop now.
    agent = Agent(
        session_path=session_path,
        llm_client=llm_client,
        chat_files_ref=current_chat_files, # Pass the temporary dict
        verbose=verbose
    )
    # Set initial environment details (might be stale, but needed for first turn)
    agent.environment_details_str = environment_details_str

    # --- Adapt Agent Interaction Logic ---
    # Implement a version of Agent.run_interaction that uses our communication functions

    # Override the agent's communication methods to use our send_message function
    def stream_to_main_process(content, role="llm"):
        send_message("stream", session_path, role=role, content=content)

    # Override the agent's tool execution to use our request_tool_execution function
    def execute_tool_via_main_process(tool_name, params):
        return request_tool_execution(session_path, tool_name, params)

    # We don't need to patch tool execution on the agent side anymore,
    # the worker will call request_tool_execution directly.
    # We also don't need to patch the agent's LLM call method, the worker loop will handle it.

    # --- Run the Agent Interaction Loop ---
    # Keep track of history *during* this interaction locally
    # Start with a copy of the history received from the main process
    interaction_history = [msg_dict for _, msg_dict in history] # Extract dicts

    try:
        # Build system prompt
        system_prompt = agent._build_system_prompt()

        # User prompt is already the last item in the history snapshot received
        # No need to append it separately here.

        # Signal start of interaction
        send_message("stream", session_path, role="llm", content="\nAssistant:\n")

        max_turns = 10  # Limit turns to prevent infinite loops
        for turn in range(max_turns):
            print(f"Worker: Agent Turn {turn + 1}/{max_turns}", file=sys.stderr)

            # 1. Prepare Prompt (Pass the current state of the local interaction_history)
            messages_to_send = agent._prepare_llm_prompt(system_prompt, interaction_history) # Pass the list of dicts

            # 2. Call LLM (directly using llm_client)
            full_response_text = "" # Accumulate the textual response
            tool_call_fragments = {} # {index: {"id": str, "type": str, "function": {"name": str, "arguments": str}}}
            started_tool_calls = set() # Keep track of tool_ids for which 'tool_json' has been sent
            llm_error_occurred = False # Flag to track LLM errors

            try:
                # Get available tools and format them for the provider
                available_tools = get_all_tools() # From tool_definitions
                formatted_tools = get_formatted_tools(available_tools, llm_client.model_name) # From llm_providers

                # Prepare arguments for llm_client.send
                completion_args = {"stream": True}
                if formatted_tools:
                    completion_args["tools"] = formatted_tools
                    completion_args["tool_choice"] = "auto" # Or make configurable if needed

                # Call llm_client directly, enabling streaming and passing tools
                response_stream = llm_client.send(messages_to_send, **completion_args)

                # Stream text chunks and accumulate tool calls
                for chunk in response_stream:
                    # --- Check for stream error marker ---
                    if isinstance(chunk, dict) and chunk.get("_stream_error"):
                        llm_error_occurred = True
                        error_message = f"[Error during LLM streaming: {chunk.get('error_message', 'Unknown stream error')}]"
                        print(f"\n{error_message}", file=sys.stderr) # Print detailed error
                        stream_to_main_process(error_message, "error") # Send simplified error
                        # Add error to local history for this interaction attempt
                        interaction_history.append({"role": "assistant", "content": error_message})
                        break # Exit the stream processing loop

                    # --- Safely access delta ---
                    delta = None
                    try:
                        # Ensure chunk is not the error marker before accessing choices/delta
                        if chunk and not isinstance(chunk, dict) and hasattr(chunk, 'choices') and chunk.choices and len(chunk.choices) > 0:
                             # Access delta safely
                             if hasattr(chunk.choices[0], 'delta'):
                                 delta = chunk.choices[0].delta
                             else:
                                 # print(f"  - Skipping chunk choice missing 'delta': {chunk.choices[0]}", file=sys.stderr)
                                 continue # Skip choice if delta is missing
                        else:
                            # Log unexpected chunk structure if needed, but don't stop
                            # print(f"  - Skipping chunk with unexpected structure: {chunk}", file=sys.stderr)
                            continue # Skip to next chunk
                    except AttributeError as e:
                        print(f"  - Error accessing chunk attributes: {e}. Chunk: {chunk}", file=sys.stderr)
                        continue # Skip malformed chunk
                    except Exception as e: # Catch other potential errors during access
                        print(f"  - Unexpected error accessing chunk delta: {e}. Chunk: {chunk}", file=sys.stderr)
                        continue

                    if not delta:
                        # print(f"  - Skipping chunk with no delta: {chunk}", file=sys.stderr)
                        continue # Skip chunk if delta couldn't be accessed

                    # --- Process text content ---
                    try:
                        if hasattr(delta, 'content') and delta.content:
                            content_piece = delta.content
                            stream_to_main_process(content_piece) # Stream text content
                            full_response_text += content_piece # Accumulate text
                    except Exception as e:
                         print(f"  - Error processing delta.content: {e}. Delta: {delta}", file=sys.stderr)
                         # Continue processing other parts if possible

                    # --- Process tool calls ---
                    try:
                        if hasattr(delta, 'tool_calls') and delta.tool_calls:
                            for call_chunk in delta.tool_calls:
                                # --- Safely access tool call chunk attributes ---
                                index = getattr(call_chunk, 'index', None)
                                if index is None:
                                    print(f"  - Skipping tool call chunk missing 'index': {call_chunk}", file=sys.stderr)
                                    continue

                                # --- Initialize fragment if new ---
                                if index not in tool_call_fragments:
                                    tool_id = getattr(call_chunk, 'id', None)
                                    tool_type = getattr(call_chunk, 'type', 'function') # Default type
                                    # Safely access function name
                                    function_obj = getattr(call_chunk, 'function', None)
                                    func_name = getattr(function_obj, 'name', None) if function_obj else None

                                    if tool_id and func_name: # Require id and function name to initialize
                                        tool_call_fragments[index] = {
                                            "id": tool_id,
                                            "type": tool_type,
                                            "function": {"name": func_name, "arguments": ""}
                                        }
                                        print(f"  - Started tool call fragment {index}: id={tool_id}, name={func_name}", file=sys.stderr)
                                        # --- Send Start of JSON Structure ---
                                        # Send tool_name explicitly in the message payload, content is now just a marker/empty
                                        send_message("stream", session_path, role="tool_json",
                                                     content="", tool_id=tool_id, tool_name=func_name) # Send empty content
                                    else:
                                        print(f"  - Skipping incomplete tool call chunk (missing id or func name): {call_chunk}", file=sys.stderr)
                                        continue # Skip if essential init info is missing

                                # --- Append and Stream Argument Chunks ---
                                # Check if fragment was successfully initialized before appending args
                                if index in tool_call_fragments:
                                    # Safely access arguments
                                    function_obj = getattr(call_chunk, 'function', None)
                                    arguments_chunk = getattr(function_obj, 'arguments', None) if function_obj else None
                                    if arguments_chunk:
                                        # Append to internal fragment storage (still needed for final parsing/history)
                                        tool_call_fragments[index]["function"]["arguments"] += arguments_chunk
                                        # --- Stream Argument Chunk ---
                                        send_message("stream", session_path, role="tool_json_args", content=arguments_chunk, tool_id=tool_call_fragments[index]["id"])
                                        # print(f"  - Streamed args chunk for fragment {index}: {arguments_chunk}", file=sys.stderr) # Verbose
                    except Exception as e:
                         print(f"  - Error processing delta.tool_calls: {e}. Delta: {delta}", file=sys.stderr)
                         # Continue processing other parts if possible

            except Exception as e:
                llm_error_occurred = True # Set flag
                error_message = f"[Error during LLM communication or streaming: {e}]\n{traceback.format_exc()}"
                print(f"\n{error_message}", file=sys.stderr) # Print detailed error
                stream_to_main_process(f"[LLM Error: {e}]", "error") # Send simplified error
                # Add error to local history for this interaction attempt
                interaction_history.append({"role": "assistant", "content": f"[LLM Error: {e}]"})
                # No 'break' here, let it proceed to 'finished' message

            # --- Check if stream loop ended due to error ---
            if llm_error_occurred:
                print("Worker: Breaking outer turn loop due to detected LLM stream error.", file=sys.stderr)
                break # Exit the 'for turn...' loop immediately

            # 3. Process Response (Parse Tool Calls from accumulated fragments)
            tool_calls_extracted = [] # List of (tool_call_id, tool_name, parameters_dict)
            reconstructed_tool_calls = [] # List of {id:.., type:.., function:{name:.., arguments:...}} for history

            if not llm_error_occurred and tool_call_fragments: # Only process tools if no LLM error
                print(f"Worker: Processing {len(tool_call_fragments)} accumulated tool call fragments.", file=sys.stderr)
                # Sort fragments by index to process in order
                sorted_indices = sorted(tool_call_fragments.keys())

                for index in sorted_indices:
                    fragment = tool_call_fragments[index]
                    tool_call_id = fragment.get("id")
                    tool_type = fragment.get("type", "function") # Usually 'function'
                    func_name = fragment.get("function", {}).get("name")
                    arguments_str = fragment.get("function", {}).get("arguments", "")

                    if not all([tool_call_id, func_name]):
                        print(f"  - Warning: Skipping incomplete tool call fragment at index {index}: {fragment}", file=sys.stderr)
                        continue

                    # Add the fully reconstructed tool call to the list for history
                    reconstructed_tool_calls.append({
                        "id": tool_call_id,
                        "type": tool_type,
                        "function": {"name": func_name, "arguments": arguments_str}
                    })

                    # Try parsing arguments for execution
                    try:
                        stripped_args = arguments_str.strip()
                        if not stripped_args:
                            parameters = {} # Treat empty args as an empty dict
                        else:
                            parameters = json.loads(stripped_args) # Parse non-empty args

                        if isinstance(parameters, dict):
                            tool_call_tuple = (tool_call_id, func_name, parameters)
                            tool_calls_extracted.append(tool_call_tuple)
                            print(f"  - Parsed tool call {index}: {func_name}({parameters}) (ID: {tool_call_id})", file=sys.stderr)
                            # --- JSON streaming is handled during the chunk processing loop ---
                            # (Keep the parsing logic here to prepare for execution)
                        else:
                            print(f"  - Error: Arguments for tool {func_name} (Index {index}) is not a JSON object: {arguments_str}", file=sys.stderr)
                            # Don't add to tool_calls_extracted if params are invalid
                    except json.JSONDecodeError as json_decode_err:
                        print(f"  - Error: Failed to decode JSON arguments for tool {func_name} (Index {index}). Error: {json_decode_err}. Arguments received:\n{arguments_str}", file=sys.stderr)
                        # Don't add to tool_calls_extracted if params are invalid
                    except Exception as parse_err:
                        print(f"  - Error: Unexpected error parsing arguments for tool {func_name} (Index {index}): {parse_err}", file=sys.stderr)
                        # Don't add to tool_calls_extracted on other errors

            # --- Log incomplete fragments if stream error occurred ---
            if llm_error_occurred and tool_call_fragments:
                parsed_ids = {call["id"] for call in reconstructed_tool_calls}
                incomplete_fragments = []
                for index, fragment in tool_call_fragments.items():
                    frag_id = fragment.get("id")
                    if frag_id and frag_id not in parsed_ids:
                        incomplete_fragments.append(f"Index {index} (ID: {frag_id}, Name: {fragment.get('function', {}).get('name')})")
                if incomplete_fragments:
                    print(f"Worker: Detected incomplete tool call fragments likely due to stream error: {', '.join(incomplete_fragments)}", file=sys.stderr)

            # Add the assistant message to history *before* executing tools
            # Include reconstructed tool calls if any were generated
            if not llm_error_occurred:
                assistant_message = {"role": "assistant"}
                filtered_response_text = _filter_environment_details(full_response_text.strip())
                # Add content only if it's non-empty after filtering
                if filtered_response_text:
                    assistant_message["content"] = filtered_response_text
                else:
                    # Per OpenAI spec, content is null if only tool_calls are present
                    assistant_message["content"] = None # Explicitly null

                # Add tool_calls structure if tools were generated
                if reconstructed_tool_calls:
                    assistant_message["tool_calls"] = reconstructed_tool_calls

                # Add message to history only if it has content OR tool calls
                if assistant_message.get("content") or assistant_message.get("tool_calls"):
                    interaction_history.append(assistant_message)
                elif not tool_call_fragments: # If no text AND no tool fragments, add empty assistant message (content="")
                     interaction_history.append({"role": "assistant", "content": ""})


            # 4. Execute Tools (if any calls were successfully *parsed* and no LLM error)
            should_continue_interaction = True # Assume continuation unless tool signals otherwise
            # Add logging before the check
            print(f"Worker: Checking tool execution. LLM Error: {llm_error_occurred}. Parsed Tool Calls: {len(tool_calls_extracted)}. Reconstructed Tool Calls: {len(reconstructed_tool_calls)}", file=sys.stderr)

            if not llm_error_occurred and tool_calls_extracted:
                tool_results_for_history = [] # Store results for history (role='tool')

                for tool_call_id, tool_name, parameters_dict in tool_calls_extracted:
                    print(f"Worker: Requesting execution for tool: {tool_name} (ID: {tool_call_id})", file=sys.stderr)
                    # Pass the already parsed dictionary
                    tool_result_str = request_tool_execution(session_path, tool_name, parameters_dict)

                    # --- Check raw tool_result_str for signals BEFORE filtering ---
                    if tool_result_str == "COMPLETION_SIGNALLED":
                        print(f"Worker: Completion signalled by tool {tool_name}. Ending interaction.", file=sys.stderr)
                        should_continue_interaction = False
                        # Add the signalling result to history before breaking
                        tool_results_for_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "name": tool_name,
                            "content": tool_result_str
                        })
                        break # Stop processing further tools
                    elif tool_result_str == TOOL_DENIED: # Use constant
                        print(f"Worker: Tool {tool_name} denied by user. Ending interaction.", file=sys.stderr)
                        should_continue_interaction = False
                        # Add the denial result to history before breaking
                        tool_results_for_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "name": tool_name,
                            "content": tool_result_str
                        })
                        break # Stop processing further tools
                    elif tool_result_str.startswith(TOOL_ERROR_PREFIX): # Use constant
                        print(f"Worker: Tool {tool_name} failed. Ending interaction. Result: {tool_result_str}", file=sys.stderr)
                        should_continue_interaction = False
                         # Add the error result to history before breaking
                        tool_results_for_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "name": tool_name,
                            "content": tool_result_str
                        })
                        break # Stop processing further tools

                    # --- If no signal, filter and prepare result for history ---
                    filtered_tool_result = _filter_environment_details(tool_result_str)
                    tool_results_for_history.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "content": filtered_tool_result
                    })

                # 5. Add Tool Results to History (potentially including signal messages)
                if tool_results_for_history:
                    interaction_history.extend(tool_results_for_history)

                # 6. Check if loop should break based on tool results
                if not should_continue_interaction:
                    print("Worker: Ending interaction loop due to tool result (completion, denial, error).", file=sys.stderr)
                    break # Exit the turn loop

                # 7. Fetch updated environment details ONLY if continuing
                if should_continue_interaction: # Check flag before fetching
                    print("Worker: Requesting updated environment details for next turn...", file=sys.stderr)
                    updated_env_details = request_environment_details(session_path)
                    agent.environment_details_str = updated_env_details # Update agent's state
                    print("Worker: Updated environment details received.", file=sys.stderr)

            # Check if interaction should end because no *parsed* tools were called
            # or if an LLM error occurred.
            elif not llm_error_occurred and not tool_calls_extracted:
                # This condition is met if:
                # - LLM produced no tool_call fragments OR
                # - LLM produced fragments, but they failed parsing (JSON error, etc.)
                print("Worker: No valid tool calls parsed or executed in this turn, ending interaction.", file=sys.stderr)
                break # Exit the turn loop
            elif llm_error_occurred: # LLM error occurred
                print("Worker: Ending interaction loop due to LLM communication error.", file=sys.stderr)
                break # Exit loop


        # --- End of Turn Loop ---

        # --- Send End of JSON Structure for each tool call ---
        if tool_call_fragments:
            print(f"Worker: Sending end markers for {len(tool_call_fragments)} tool calls.", file=sys.stderr)
            for index in sorted(tool_call_fragments.keys()):
                fragment = tool_call_fragments[index]
                tool_id = fragment.get("id")
                if tool_id:
                    # Send an empty content marker for the end
                    send_message("stream", session_path, role="tool_json_end", content="", tool_id=tool_id) # Send empty content

        # Signal interaction finished
        # Determine status based on whether an LLM error occurred or max turns were reached
        if llm_error_occurred:
            status = "llm_error"
            finish_message = "Interaction ended due to LLM communication error."
        elif turn >= max_turns - 1: # Check if loop finished due to max_turns
            status = "max_turns_reached"
            finish_message = f"Interaction ended after reaching max {max_turns} turns."
        else: # Loop finished normally (no tool calls, completion, denial, or tool error)
            status = "success"
            finish_message = f"Interaction ended after {turn + 1} turns."

        finish_data = {
            "status": status,
            "message": finish_message
        }
        # Include the final history state unless there was an LLM error
        if status != "llm_error":
            finish_data["final_history"] = interaction_history # Send back the list of dicts

        send_message("finished", session_path, **finish_data)

    except Exception as e:
        tb_str = traceback.format_exc()
        error_msg = f"Critical error in agent interaction loop: {e}\n{tb_str}"
        print(error_msg, file=sys.stderr)
        # Ensure session_path is valid before sending messages
        valid_session_path = session_path or "unknown_session"
        # Use send_message for consistency
        send_message("stream", valid_session_path, role="error", content=f"[Agent Critical Error: {e}]")
        send_message("finished", valid_session_path, status="critical_error", message=error_msg)


# --- Main Worker Loop ---

def main():
    """Reads requests from stdin and handles them."""
    # Indicate worker is ready (optional)
    # print(json.dumps({"type": "status", "status": "ready"}), flush=True)

    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                # End of input, exit gracefully
                # print(json.dumps({"type": "status", "status": "exiting", "reason": "stdin closed"}), flush=True)
                break

            request = json.loads(line)
            if request.get("type") == "interaction_request":
                handle_interaction_request(request.get("data"))
            elif request.get("type") == "ping": # Example control message
                send_message("pong", request.get("session", "control"))
                # Handle other control messages if needed (e.g., shutdown)

        except json.JSONDecodeError:
            # Log error but try to continue reading
             print(json.dumps({"type": "error", "session":"unknown", "message": f"Worker received invalid JSON: {line.strip()}"}), flush=True)
        except Exception as e:
            # Log unexpected errors
            tb_str = traceback.format_exc()
            print(json.dumps({"type": "error", "session":"unknown", "message": f"Worker main loop error: {e}\n{tb_str}"}), flush=True)
            # Depending on the error, might want to break or continue
            time.sleep(1) # Avoid tight loop on persistent error


def request_environment_details(session_path):
    """Sends a request for environment details and waits for the result."""
    request_id = f"env_{time.time_ns()}" # Unique ID for the request
    send_message("get_environment_details_request", session_path, request_id=request_id)
    # Wait for the corresponding response from stdin
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                send_message("error", session_path, message="Stdin closed unexpectedly while waiting for env details. Exiting.")
                sys.exit(1)
            response = json.loads(line)
            if response.get("type") == "get_environment_details_response" and response.get("request_id") == request_id:
                return response.get("details", "") # Return details string or empty
        except json.JSONDecodeError:
            send_message("error", session_path, message=f"Worker received invalid JSON from stdin while waiting for env details: {line.strip()}")
        except Exception as e:
            send_message("error", session_path, message=f"Error reading env details result from stdin: {e}")
            return f"<environment_details>\n# Error receiving details: {e}\n</environment_details>" # Return error state


if __name__ == "__main__":
    main()
