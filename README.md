<p align="center">
  <img style='height: auto; width: 35%; object-fit: contain' src="./Emigo.png">
</p>

# Emigo: Agentic AI Programming Assistant for Emacs

Emigo brings AI-powered development to Emacs, integrating large language models directly into your workflow. Inspired by the capabilities of tools like [Aider](https://github.com/paul-gauthier/aider) and [Cline](https://github.com/sturdy-dev/cline), and the spiritual successor of [Aidermacs](https://github.com/MatthewZMD/aidermacs), Emigo acts as an **agentic** AI assistant. It leverages **tool use** to interact with your project, read files, write code, execute commands, and more, all within Emacs.

## Development Status

⚠️ **Beta Warning**: Emigo is under active development. Expect:

- Breaking API changes
- Unstable features
- Incomplete documentation

Please use only for testing and development. Report issues - your feedback helps shape Emigo!

## Key Features

*   **Agentic Tool Use:** Emigo doesn't just generate text; it uses tools to interact with your environment based on the LLM's reasoning.
*   **Emacs Integration:** Designed to feel native within Emacs, leveraging familiar interfaces and workflows.
*   **Flexible LLM Support:** Connects to various LLM providers through [LiteLLM](https://github.com/BerriAI/litellm), allowing you to choose the model that best suits your needs.
*   **Context-Aware Interactions:** Manages chat history and project context for coherent sessions.

## Installation

1.  **Prerequisites:**
    *   Emacs 28 or higher.
    *   Python 3.x.
2.  **Install Python Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
3.  **Install with straight.el:** Add to your Emacs config:

    ```emacs-lisp
    (use-package emigo
      :straight (:host github :repo "MatthewZMD/emigo")
      :config
      (emigo-enable) ;; Starts the background process automatically
      :custom
      ;; Encourage using OpenRouter with Deepseek
      (emigo-model "openrouter/deepseek/deepseek-chat-v3-0324"")
      (emigo-base-url "https://openrouter.ai/api/v1")
      ;; Securely load your API key (replace with your preferred method)
      (emigo-api-key (emigo-read-file-content "~/.config/openrouter/key.txt")))
    ```

## Usage

1.  **Start Emigo:** Navigate to your project directory (or any directory you want to work in) and run `M-x emigo`.
2.  **Enter Prompt:** You'll be prompted for your request in the minibuffer.
3.  **Interact:** Emigo will open a dedicated buffer. The AI will respond, potentially using tools. You might be asked for approval for certain actions (like running commands or writing files).
4.  **Add Files to Context:** Mention files in your prompt using the `@` symbol (e.g., `Refactor the function in @src/utils.py`). Emigo will automatically add mentioned files to the context if they exist within the project.
5.  **Manage Context:**
    *   `C-c C-l` (`emigo-list-context-files`): List files currently included in the chat context.
    *   `C-c C-f` (`emigo-remove-file-from-context`): Remove a file from the context.

Emigo manages sessions based on the directory where you invoke `M-x emigo`. If invoked within a Git repository, the repository root is typically used as the session path. Use `C-u M-x emigo` to force the session path to be the current `default-directory`.

## Understanding Tool Use

The core of Emigo's power lies in its agentic tool use. Instead of just providing code suggestions, the LLM analyzes your request and decides which actions (tools) are necessary to accomplish the task.

1.  **LLM Reasoning:** Based on your prompt and the current context, the LLM determines the next step.
2.  **Tool Selection:** It chooses an appropriate tool, such as `read_file`, `write_to_file`, `replace_in_file`, `execute_command`, `list_files`, `list_repomap`, or `ask_followup_question`.
3.  **Tool Execution:** Emigo executes the chosen tool, potentially asking for your approval for sensitive operations.
4.  **Result Feedback:** The result of the tool execution (e.g., file content, command output, error message) is fed back into the conversation history.
5.  **Iteration:** The LLM uses this new information to decide the next step, continuing the cycle until the task is complete or requires further input.

This iterative process allows Emigo to tackle more complex tasks that involve multiple steps and interactions with your project files and system. The LLM uses an XML format to specify the tool and its parameters.
