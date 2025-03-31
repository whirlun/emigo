# Emigo

Emigo brings AI-powered development to Emacs. If you're missing Cursor but prefer living in Emacs, Emigo provides similar AI capabilities while staying true to Emacs workflows. Inspired by projects like Aider and Cline, Emigo implements **agentic tooluse** to enable powerful AI-assisted development directly within Emacs.

## Note: This is an active development project on a daily basis. Expect breaking changes and instability as features are being developed. Contributions and feedback are welcome!

## Installation
1. Install Emacs 28 or higher version
2. Install Python dependencies: `pip3 install -r ./requirements.txt`
3. Download this repository using git clone, and replace the load-path path in the configuration below.
4. Add the following code to your configuration file ~/.emacs:

```elisp
(add-to-list 'load-path "<path-to-emigo>")

(require 'emigo)
(emigo-enable)
(setq emigo-model "openrouter/anthropic/claude-3.7-sonnet")
(setq emigo-base-url "https://openrouter.ai/api/v1")
(setq emigo-api-key (emigo-read-file-content "~/.config/openrouter/key.txt"))
```

Note, you need fill AI key content in `~/.config/openrouter/key.txt` first.

## Usage
Execute command `emigo`, input prompt.

Emigo will send prompt to AI.
