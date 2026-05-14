# termTab

Warp-style inline AI autocomplete for zsh, rendered through `zsh-autosuggestions`
and powered by a BYOK local helper.

The plugin is designed for command-line completions:

- Local zsh history is searched first and weighted by recency plus frequency.
- Repeated commands complete locally without a network call.
- Unseen commands fall through to an LLM provider.
- OpenRouter is the default provider, using `mistralai/codestral-2508`.
- Anthropic, OpenAI-compatible, and Ollama providers are supported by config.
- API keys are stored in macOS Keychain or read from environment variables.
- API keys are never stored in this repository or in `ai.toml`.

## Install

```zsh
./install.sh
```

Then store an OpenRouter key in macOS Keychain:

```zsh
cmux-inline-ai-key
```

Provider-specific key storage is also supported:

```zsh
cmux-inline-ai-key openrouter
cmux-inline-ai-key anthropic
cmux-inline-ai-key openai
```

Open a new terminal, then test:

```zsh
python3 ~/.config/cmux/inline-ai/inline_ai_complete.py --line 'git sta' --cwd "$PWD"
```

Expected output when the command exists in your history:

```text
git status
```

## Configuration

Runtime config lives at:

```text
~/.config/cmux/ai.toml
```

The public example is [config/ai.example.toml](config/ai.example.toml).

No secrets belong in `ai.toml`. Use Keychain through `cmux-inline-ai-key`, or
environment variables:

- `OPENROUTER_API_KEY`
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`

## How It Works

`src/inline-ai.zsh` registers a `zsh-autosuggestions` strategy. On each
autocomplete request it flushes recent zsh history, invokes
`src/inline_ai_complete.py`, and renders the returned full-line suggestion as
gray ghost text.

The Python helper:

1. Rejects likely secret-bearing input.
2. Searches shell history for prefix matches.
3. Scores matches by recency, frequency, and concise suffix length.
4. Returns a strong history match immediately.
5. Otherwise builds a scrubbed prompt with recent history and cached
   `<command> --help` text.
6. Calls the configured provider and normalizes the response to a full-line
   suggestion.

## Secret Handling

The helper redacts common credential shapes before prompt construction:

- `*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`
- `AWS_*`
- AWS access key IDs
- OpenAI, Anthropic, GitHub, and bearer-token-like values
- Private key block headers

The repository intentionally includes only source, docs, tests, and example
config. It does not include local Keychain contents, API keys, shell history,
or runtime caches.

## Uninstall

```zsh
./uninstall.sh
```

The uninstall script removes the installed helper files and the zshrc source
block. It does not delete Keychain entries unless you remove them manually.

## Development

Run the tests:

```zsh
python3 -m unittest discover -s tests
```

Run the helper syntax check:

```zsh
python3 -m py_compile src/inline_ai_complete.py
```
