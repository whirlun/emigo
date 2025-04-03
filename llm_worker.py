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
- Initializes the `LLMClient` (from `llm.py`) and `Agents` (from `agents.py`)
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

# Add project root to sys.path to allow importing other modules like llm, agents, utils
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from llm import LLMClient
from agents import Agents

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


def request_tool_execution(session_path, tool_name, params):
    """Sends a tool request and waits for the result from stdin."""
    request_id = f"tool_{time.time_ns()}" # Unique ID for the request
    send_message("tool_request", session_path, request_id=request_id, tool_name=tool_name, params=params)
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

    if not model_name:
        send_message("error", session_path, message="Missing 'model' in config.")
        return

    llm_client = LLMClient(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        verbose=verbose,
    )
    # History is managed locally within this function now.

    # --- Initialize Agent (or adapt its logic) ---
    # Create a temporary chat_files dict for this request instance (still needed for Agent init?)
    # Agent class might not need chat_files_ref anymore if env details are pre-built.
    # Let's keep it for now in case Agent uses it for other things.
    # The main process owns the canonical chat_files state
    current_chat_files = {session_path: list(chat_files_list)}

    # NOTE: This creates a new Agents instance for *every* request because
    # the worker might be killed. If the worker were persistent, we might
    # reuse instances.
    # Environment details are fetched dynamically within the loop now.
    agent = Agents(
        session_path=session_path,
        llm_client=llm_client,
        chat_files_ref=current_chat_files, # Pass the temporary dict
        verbose=verbose
    )
    # Set initial environment details (might be stale, but needed for first turn)
    agent.environment_details_str = environment_details_str

    # --- Adapt Agent Interaction Logic ---
    # Implement a version of Agents.run_interaction that uses our communication functions

    # Override the agent's communication methods to use our send_message function
    def stream_to_main_process(content, role="llm"):
        send_message("stream", session_path, role=role, content=content)

    # Override the agent's tool execution to use our request_tool_execution function
    def execute_tool_via_main_process(tool_name, params):
        return request_tool_execution(session_path, tool_name, params)

    # Patch the agent instance's _call_llm_and_stream_response method
    # to use our communication wrapper.
    # Note: We are replacing the method on the *instance*, not the class.
    # This lambda captures the necessary variables (agent, llm_client, stream_to_main_process)
    agent._call_llm_and_stream_response = lambda messages_to_send: agent_call_llm_wrapper(
        agent, llm_client, messages_to_send, stream_to_main_process
    )
    # We don't need to patch tool execution on the agent side anymore,
    # the worker will call request_tool_execution directly.

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

            # 2. Call LLM (streaming happens via our patched method)
            full_response = None
            # The llm_client is now stateless regarding history
            try:
                # Stream the response through our patched method
                response_stream = llm_client.send(messages_to_send, stream=True)
                full_response = ""
                for chunk in response_stream:
                    stream_to_main_process(str(chunk))
                    full_response += chunk
            except Exception as e:
                error_message = f"[Error during LLM communication: {e}]"
                print(f"\n{error_message}", file=sys.stderr)
                stream_to_main_process(str(error_message), "error")
                # Add error to local history for this interaction attempt
                interaction_history.append({"role": "assistant", "content": error_message})
                break  # Exit loop on communication error

            if full_response is None: # Check if LLM call failed
                # Error message already streamed, just break
                break

            # Add assistant's response to the local interaction history
            interaction_history.append({"role": "assistant", "content": full_response})

            # 3. Process Response (Parse Tools)
            tool_requests = agent._parse_tool_use(full_response) # Returns list of (tool_name, params)

            if not tool_requests:
                # No tool use found, assume final response
                print("Worker: No tool use found, ending interaction", file=sys.stderr)
                break # Exit the turn loop

            # 4. Execute Tools (if any)
            tool_results = []
            should_continue_interaction = True
            for tool_name, params in tool_requests:
                # Special handling for completion tool
                if tool_name == "attempt_completion":
                    result_text = params.get("result", "")
                    command = params.get("command", "")
                    # Send completion message to main process (which signals Emacs)
                    # The main process handles the "COMPLETION_SIGNALLED" logic now.
                    # We just need to request the tool execution.
                    print(f"Worker: Requesting execution for completion tool", file=sys.stderr)
                    tool_result = request_tool_execution(session_path, tool_name, params)
                    tool_results.append(tool_result) # Append the result (e.g., "COMPLETION_SIGNALLED")
                    should_continue_interaction = False # End interaction after this tool
                    break # Stop processing further tools in this turn

                # Execute other tools via main process communication
                print(f"Worker: Requesting execution for tool: {tool_name}", file=sys.stderr)
                tool_result = request_tool_execution(session_path, tool_name, params)
                tool_results.append(tool_result)

                # Check for denial or error that should stop the interaction
                # The main process formats these standard strings.
                if tool_result == "TOOL_DENIED" or tool_result.startswith("<tool_error>"):
                    print(f"Worker: Tool denied or failed, ending interaction. Result: {tool_result}", file=sys.stderr)
                    should_continue_interaction = False
                    break # Stop processing further tools in this turn

            # 5. Add Tool Results to History
            if tool_results:
                # Filter out the special completion marker if present
                llm_tool_results = [res for res in tool_results if res != "COMPLETION_SIGNALLED"]
                if llm_tool_results:
                    combined_tool_result = "\n\n".join(llm_tool_results)
                    # Add tool results to the local interaction history
                    # Tool results act like user input for the next LLM turn
                    interaction_history.append({"role": "user", "content": combined_tool_result})

            # 6. Check if loop should break
            if not should_continue_interaction:
                print("Worker: Ending interaction due to completion, denial, or error.", file=sys.stderr)
                break # Exit the turn loop

            # 7. Fetch updated environment details for the *next* turn
            print("Worker: Requesting updated environment details...", file=sys.stderr)
            updated_env_details = request_environment_details(session_path)
            agent.environment_details_str = updated_env_details # Update agent's state for _prepare_llm_prompt
            # Also update the environment details in the request dict in case it's needed elsewhere? No, agent uses its internal copy.
            print("Worker: Updated environment details received.", file=sys.stderr)


        # --- End of Turn Loop ---

        # Signal interaction finished
        status = "success" if turn < max_turns - 1 else "max_turns_reached"
        finish_data = {
            "status": status,
            "message": f"Interaction ended after {turn+1} turns."
        }
        # Include the final history state if successful
        if status in ["success", "max_turns_reached"]:
            finish_data["final_history"] = interaction_history # Send back the list of dicts

        send_message("finished", session_path, **finish_data)

    except Exception as e:
        tb_str = traceback.format_exc()
        error_msg = f"Critical error in agent interaction loop: {e}\n{tb_str}"
        print(error_msg, file=sys.stderr)
        stream_to_main_process(f"[Agent Critical Error: {e}]", "error")
        send_message("finished", session_path, status="error", message=error_msg)


# Helper function to call LLM and stream response
def agent_call_llm_wrapper(agent, llm_client, messages_to_send, stream_callback):
    """Wrapper for agent's _call_llm_and_stream_response that uses our stream callback."""
    full_response = ""
    try:
        # Send the temporary list with context included
        response_stream = llm_client.send(messages_to_send, stream=True)
        for chunk in response_stream:
            stream_callback(str(chunk))
            full_response += chunk
    except Exception as e:
        error_msg = f"[LLM Communication Error: {str(e)}]"
        stream_callback(error_msg)
        full_response = error_msg
    return full_response


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
