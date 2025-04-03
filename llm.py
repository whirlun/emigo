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

import datetime # Keep for potential future use, but time.time() is simpler for timestamp
import importlib
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
    ) -> Union[Iterator[str], str]:
        """
        Sends the provided messages list to the LLM and returns the response.

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
            response = litellm.completion(**completion_kwargs)

            if stream:
                # Generator to yield content chunks
                def content_stream():
                    full_response_content = ""
                    for chunk in response:
                        # Check if chunk and choices are valid
                        if chunk and chunk.choices and len(chunk.choices) > 0:
                             delta = chunk.choices[0].delta
                             # Check if delta and content are valid
                             if delta and delta.content:
                                 content_piece = delta.content
                                 full_response_content += content_piece
                                 yield content_piece
                    # Optionally store the full response after streaming for history
                    # self._last_full_response = full_response_content

                return content_stream()
            else:
                # Return the full content directly for non-streaming
                if response and response.choices and len(response.choices) > 0:
                    message = response.choices[0].message
                    return message.content or ""
                else:
                    print("Warning: Received empty or invalid response from LLM.", file=sys.stderr)
                    return ""

        except Exception as e:
            # Catch potential exceptions from litellm (API errors, connection issues, etc.)
            print(f"\nError during LLM communication: {e}", file=sys.stderr)
            # Depending on the error type, you might want to raise it or handle differently
            # For simplicity, we'll return an empty response or re-raise
            if stream:
                return iter([]) # Return an empty iterator on error for streaming
            else:
                return "" # Return an empty string on error for non-streaming


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
    import json
    print(json.dumps(messages, indent=2))


if __name__ == "__main__":
    main()
