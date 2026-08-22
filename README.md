# termTab

Terminal-agnostic inline AI autocomplete for zsh, rendered through
`zsh-autosuggestions` and powered by a BYOK local helper.

The plugin is designed for command-line completions:

- Local zsh history is searched first and weighted by recency plus frequency.
- Commands from the current terminal session are tracked separately and ranked
  above older global history.
- Recent command intent is used for local next-step suggestions, such as
  `mkdir app` followed by `c` completing to `cd app`.
- Repeated commands complete locally without a network call.
- Unseen commands fall through to an LLM provider.
- OpenRouter is the default provider, using `mistralai/codestral-2508`.
- Anthropic, OpenAI-compatible, and Ollama providers are supported by config.
- API keys are stored in macOS Keychain or read from environment variables.
- API keys are never stored in this repository or in `ai.toml`.

It works in any terminal that can run zsh and `zsh-autosuggestions`: Apple
Terminal, iTerm2, Ghostty, VS Code terminals, and similar PTY-based terminal
apps. There is no dependency on a terminal-specific renderer.

## Install

```zsh
./install.sh
```

Then store an OpenRouter key in macOS Keychain:

```zsh
termtab-ai-key
```

Provider-specific key storage is also supported:

```zsh
termtab-ai-key openrouter
termtab-ai-key anthropic
termtab-ai-key openai
```

Open a new terminal, then test:

```zsh
python3 ~/.config/termtab/inline-ai/inline_ai_complete.py --line 'git sta' --cwd "$PWD"
```

Expected output when the command exists in your history:

```text
git status
```

## Configuration

Runtime config lives at:

```text
~/.config/termtab/ai.toml
```

The public example is [config/ai.example.toml](config/ai.example.toml).

No secrets belong in `ai.toml`. Use Keychain through `termtab-ai-key`, or
environment variables:

- `OPENROUTER_API_KEY`
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`

## How It Works

`src/inline-ai.zsh` registers a `zsh-autosuggestions` strategy. On each
autocomplete request it flushes recent zsh history, invokes
`src/inline_ai_complete.py`, and renders the returned full-line suggestion as
gray ghost text.

When `output_capture.enabled = true` (the default), the plugin also re-execs
the interactive zsh under `script(1)` so the session typescript is recorded
to `~/.cache/termtab/inline-ai/sessions/<id>.tty`. After each command, the
plugin records the byte range covering that command's output, plus exit
status and cwd, into `<id>.last.tsv`. The Python helper reads that sidecar
on the next keystroke, strips ANSI, and uses the last command's output as
context — for example, when `gti status` fails with `zsh: command not found:
gti`, typing `g` ghost-suggests `git status` instead of replaying the typo.
Set `output_capture.enabled = false` in `ai.toml` to keep the shell vanilla.

When a correction diverges from what is already typed, zsh-autosuggestions
cannot render it as normal ghost text. termTab then shows a ZLE status message
like `termTab fix: Alt-R replaces with: ...`; press Alt-R/Esc-r, or
Ctrl-X Ctrl-R, to replace the whole buffer with the correction.

Tab remains normal zsh completion. If the shell can complete or list filesystem
or command matches, Tab keeps doing that; use right-arrow/end to accept ghost
text.

The Python helper:

1. Rejects likely secret-bearing input.
2. Checks current-session command events for deterministic follow-ups.
3. Searches shell history for prefix matches.
4. Scores matches by recency, frequency, and concise suffix length.
5. Returns a strong local match immediately.
6. Otherwise builds a scrubbed prompt with recent history and cached
   `<command> --help` text.
7. Calls the configured provider and normalizes the response to a full-line
   suggestion.

Example local follow-up:

```zsh
mkdir app
# type: c
# suggestion: cd app
```

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

Existing installs that used the older `cmux-inline-ai-*` Keychain service names
continue to work as a migration fallback, but new installs use `termtab-ai-*`.

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
