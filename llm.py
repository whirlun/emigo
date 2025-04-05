#!/usr/bin/env python

"""
LLM Client Wrapper using LiteLLM.

Provides a simplified interface (`LLMClient`) for interacting with various
Large Language Models (LLMs) supported by the `litellm` library. It handles
API calls, streaming responses, and basic configuration (model name, API keys,
base URLs).

Note: This client is designed to be stateless regarding chat history. The
calling process (e.g., `llm_worker.py`) is responsible for managing and
passing the complete message history for each API call.
"""

import importlib
import json
import os
import sys
import time
import warnings
from typing import Dict, Iterator, List, Optional, Union # Removed Tuple

# Filter out UserWarning from pydantic used by litellm
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

# --- Lazy Loading for litellm ---

# Configure basic litellm settings globally
EMIGO_SITE_URL = "https://github.com/MatthewZMD/emigo" # Example URL, adjust if needed
EMIGO_APP_NAME = "Emigo" # Example App Name
os.environ["OR_SITE_URL"] = os.environ.get("OR_SITE_URL", EMIGO_SITE_URL)
os.environ["OR_APP_NAME"] = os.environ.get("OR_APP_NAME", EMIGO_APP_NAME)
os.environ["LITELLM_MODE"] = os.environ.get("LITELLM_MODE", "PRODUCTION")

VERBOSE_LLM_LOADING = False # Set to True for debugging litellm loading

class LazyLiteLLM:
    """Lazily loads the litellm library upon first access."""
    _lazy_module = None

    def __getattr__(self, name):
        # Avoid infinite recursion during initialization
        if name == "_lazy_module":
            return super().__getattribute__(name)

        self._load_litellm()
        return getattr(self._lazy_module, name)

    def _load_litellm(self):
        """Loads and configures the litellm module."""
        if self._lazy_module is not None:
            return

        if VERBOSE_LLM_LOADING:
            print("Loading litellm...", file=sys.stderr)
        start_time = time.time()

        try:
            self._lazy_module = importlib.import_module("litellm")

            # Basic configuration similar to Aider
            self._lazy_module.suppress_debug_info = True
            self._lazy_module.set_verbose = False
            self._lazy_module.drop_params = True # Drop unsupported params silently
            # Attempt to disable internal debugging/logging if method exists
            if hasattr(self._lazy_module, "_logging") and hasattr(
                self._lazy_module._logging, "_disable_debugging"
            ):
                self._lazy_module._logging._disable_debugging()

        except ImportError as e:
            print(
                f"Error: {e} litellm not found. Please install it: pip install litellm",
                file=sys.stderr,
            )
            sys.exit(1)
        except Exception as e:
            print(f"Error loading litellm: {e}", file=sys.stderr)
            sys.exit(1)

        if VERBOSE_LLM_LOADING:
            load_time = time.time() - start_time
            print(f"Litellm loaded in {load_time:.2f} seconds.", file=sys.stderr)

# Global instance of the lazy loader
litellm = LazyLiteLLM()

# --- LLM Client Class ---

class LLMClient:
    """Handles interaction with the LLM and manages chat history."""

    def __init__(
        self,
        model_name: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        verbose: bool = False,
    ):
        """
        Initializes the LLM client.

        Args:
            model_name: The name of the language model to use (e.g., "gpt-4o").
            api_key: Optional API key for the LLM service.
            base_url: Optional base URL for custom LLM endpoints (like Ollama).
            verbose: If True, enables verbose output.
        """
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.verbose = verbose

    def send(
        self,
        messages: List[Dict],
        stream: bool = True,
        temperature: float = 0.7,
        tools: Optional[List[Dict]] = None, # Add tools parameter
        tool_choice: Optional[str] = "auto", # Add tool_choice parameter
    ) -> Union[Iterator[str], object]: # Return type might be object for raw response
        """
        Sends the provided messages list to the LLM, potentially with tool definitions,
        and returns the response.

        Args:
            messages: The list of message dictionaries to send.
            stream: Whether to stream the response or wait for the full completion.
            temperature: The sampling temperature for the LLM.

        Returns:
            An iterator yielding response chunks if stream=True, otherwise the
            full response content string.
        """
        # Ensure litellm is loaded before making the call
        litellm._load_litellm()

        completion_kwargs = {
            "model": self.model_name,
            "messages": messages,
            "stream": stream,
            "temperature": temperature,
        }
        # Add tools and tool_choice if provided and not None/empty
        if tools:
            completion_kwargs["tools"] = tools
        if tool_choice: # Only add if tool_choice is meaningful
            completion_kwargs["tool_choice"] = tool_choice # e.g., "auto", "required", specific tool

        # Add API key and base URL if they were provided
        if self.api_key:
            completion_kwargs["api_key"] = self.api_key
        if self.base_url:
            completion_kwargs["base_url"] = self.base_url
            # OLLAMA specific adjustment if needed (example)
            if "ollama" in self.model_name or (self.base_url and "ollama" in self.base_url):
                 # LiteLLM might handle this automatically, but explicitly setting can help
                 completion_kwargs["model"] = self.model_name.replace("ollama/", "")

        try:
            # Store the raw response object for potential parsing later (e.g., tool calls)
            self.last_response_object = None # Initialize

            # Initiate the LLM call
            response = litellm.completion(**completion_kwargs)
            self.last_response_object = response # Store the raw response

            # --- Verbose Logging ---
            if self.verbose:
                # Import json here if not already imported at the top level
                import json
                print("\n--- Sending to LLM ---", file=sys.stderr)
                # Avoid printing potentially large base64 images in verbose mode
                printable_messages = []
                for msg in messages: # Use the 'messages' argument passed to send()
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
                        # Append the modified message with potentially truncated image data
                        printable_messages.append({"role": msg["role"], "content": new_content})
                    else:
                        printable_messages.append(msg) # Append non-image messages as is

                # Calculate approximate token count using litellm's utility
                token_count_str = ""
                try:
                    # Ensure litellm is loaded before using its utilities
                    litellm._load_litellm()
                    # Use litellm's token counter if available
                    count = litellm.token_counter(model=self.model_name, messages=messages)
                    token_count_str = f" (estimated {count} tokens)"
                except Exception as e:
                     # Fallback or simple message if token counting fails
                     # We can't easily use the agent's tokenizer here, so rely on litellm or skip detailed count
                     token_count_str = f" (token count unavailable: {e})"


                print(json.dumps(printable_messages, indent=2), file=sys.stderr)
                print(f"--- End LLM Request{token_count_str} ---", file=sys.stderr)
            # --- End Verbose Logging ---

            if stream:
                # Generator to yield the raw litellm chunk objects
                def raw_chunk_stream():
                    try:
                        for chunk in response:
                            yield chunk # Yield the original chunk object
                    except litellm.exceptions.APIConnectionError as e:
                        # Catch the specific error observed in the traceback during stream iteration
                        print(f"\n[LLMClient Warning] Caught APIConnectionError during stream processing: {e}", file=sys.stderr)
                        print("[LLMClient Warning] Stream may be incomplete due to provider error.", file=sys.stderr)
                        # Optionally yield one last error message? For now, just break.
                        # yield {"error": f"Stream interrupted by APIConnectionError: {e}"}
                        pass # Gracefully end the stream
                    except Exception as e:
                        # Catch other potential errors during streaming
                        print(f"\n[LLMClient Warning] Caught unexpected error during stream processing: {e}", file=sys.stderr)
                        pass # Gracefully end the stream


                return raw_chunk_stream() # Return the generator yielding full chunks
            else:
                # For non-streaming, return the raw response object
                # The caller (llm_worker) will parse content or tool calls
                return response # Return the whole LiteLLM response object

        except litellm.APIConnectionError as e:
            # Handle connection errors specifically (like the OpenRouterException)
            error_message = f"API Connection Error: {e}"
            print(f"\n{error_message}", file=sys.stderr)
            # Optionally log the messages sent for debugging
            # print(f"Messages sent: {messages}", file=sys.stderr)
            if stream:
                # Yield the error message as part of the stream
                def error_stream():
                    yield f"[LLM Error: {error_message}]"
                return error_stream()
            else:
                return f"[LLM Error: {error_message}]" # Return error message directly

        except Exception as e:
            # Catch other potential exceptions from litellm
            error_message = f"General Error during LLM communication: {e}"
            print(f"\n{error_message}", file=sys.stderr)
            # Optionally log the messages sent for debugging
            # print(f"Messages sent: {messages}", file=sys.stderr)
            if stream:
                 # Yield the error message as part of the stream
                def error_stream():
                    yield f"[LLM Error: {error_message}]"
                return error_stream()
            else:
                return f"[LLM Error: {error_message}]" # Return error message directly


# --- Example Usage (Optional) ---

def main():
    """Basic example demonstrating the LLMClient."""
    # Configure from environment variables or defaults
    model = os.getenv("EMIGO_MODEL", "gpt-4o-mini") # Example: use EMIGO_MODEL env var
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_API_BASE") # Or OLLAMA_HOST, etc.

    if not api_key and not base_url:
        print("Warning: No API key or base URL found. Using default litellm configuration.", file=sys.stderr)

    client = LLMClient(model_name=model, api_key=api_key, base_url=base_url, verbose=True)

    # Example messages list (history is managed externally)
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the capital of France?"}
    ]
    print(f"\nUser: {messages[-1]['content']}")

    # Send the messages list (non-streaming)
    print("\nAssistant (non-streaming):")
    assistant_response = client.send(messages, stream=False)
    print(assistant_response)

    # Add assistant's response to the external history list
    messages.append({"role": "assistant", "content": assistant_response})

    # Add another user message
    user_input_2 = "What about Spain?"
    messages.append({"role": "user", "content": user_input_2})
    print(f"\nUser: {user_input_2}")

    # Send again (streaming)
    print("\nAssistant (streaming):")
    full_streamed_response = ""
    response_stream = client.send(messages, stream=True)
    for chunk in response_stream:
        print(chunk, end="", flush=True)
        full_streamed_response += chunk
    print() # Newline after stream

    # Add streamed response to the external history list
    messages.append({"role": "assistant", "content": full_streamed_response})

    print("\n--- Final Messages List ---")
    print(json.dumps(messages, indent=2))


if __name__ == "__main__":
    main()
