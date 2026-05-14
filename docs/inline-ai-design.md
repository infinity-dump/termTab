# Inline AI Command Autocomplete Design

## Product Shape

termTab is a terminal-agnostic zsh autocomplete plugin. It provides Warp-style
ghost text without requiring control of the terminal renderer. The renderer is
`zsh-autosuggestions`; the intelligence is a local Python helper that first
uses weighted shell history and then, only when needed, calls a configured BYOK
model provider.

The design goal is practical compatibility: it should work in Apple Terminal,
iTerm2, Ghostty, VS Code terminals, and any other terminal that can run zsh and
`zsh-autosuggestions`.

## Architecture

```text
zsh line editor
  owns: editable buffer, cursor, accept key behavior
  |
  v
zsh-autosuggestions strategy
  owns: ghost text rendering and Tab acceptance
  |
  v
termTab Python helper
  owns: history scoring, secret redaction, help capture, prompt building
  |
  +--> local history and command --help cache
  |
  +--> provider adapter
       OpenRouter, Anthropic, OpenAI-compatible, or Ollama
```

The terminal app only displays the normal shell UI. termTab does not need
private OSC protocols, terminal-grid overlays, a socket API, or renderer hooks.
That keeps the plugin portable and avoids coupling command prediction to any
single terminal.

## Completion Flow

1. zsh asks `zsh-autosuggestions` for a suggestion.
2. The termTab strategy flushes recent shell history with `fc -AI`.
3. The helper receives the current line and cwd.
4. Secret-looking input is rejected before any prompt is built.
5. History is searched for prefix matches.
6. Matches are scored by recency, frequency, and concise suffix length.
7. A strong prefix match is returned immediately, without network access.
8. If history does not answer, the helper builds a scrubbed prompt.
9. The configured provider returns a suffix.
10. The helper normalizes the suffix into a full-line suggestion.
11. `zsh-autosuggestions` renders the result as gray ghost text.

For repeated commands, the hot path is local and should be fast enough to feel
native. Provider calls are a fallback for new commands, unfamiliar flags, and
CLIs whose `--help` output adds useful context.

## Public Configuration

Runtime config lives at:

```text
~/.config/termtab/ai.toml
```

Example:

```toml
[inline]
enabled = true
accept_keys = ["right", "tab"]
debounce_ms = 120

[history]
enabled = true
direct_match_min_chars = 3
max_entries = 8000
max_bytes = 2097152
top_matches = 8
max_suggestion_chars = 320
recency_weight = 100
frequency_weight = 8

[provider]
name = "openrouter"
model = "mistralai/codestral-2508"
base_url = "https://openrouter.ai/api/v1"
sort = "latency"
max_tokens = 24
```

No API key belongs in this file.

## Secret Handling

Keys are stored in macOS Keychain through:

```zsh
termtab-ai-key
```

The helper also supports provider environment variables:

- `OPENROUTER_API_KEY`
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`

The prompt builder redacts common credential shapes before model calls:

- environment names ending in `KEY`, `TOKEN`, `SECRET`, or `PASSWORD`
- AWS-looking environment names
- OpenAI, Anthropic, GitHub, AWS, bearer-token-like, and private-key-like values

If the editable command line itself appears to contain a secret, the helper
returns no suggestion.

## Provider Defaults

OpenRouter is the default provider because it gives one key for many models.
The default model is `mistralai/codestral-2508`, chosen because it is built for
low-latency code and completion-style work. OpenRouter latency sorting is
enabled by default.

Good alternatives:

- `openai/gpt-4.1-nano` for an OpenAI-hosted low-latency option.
- `google/gemini-2.5-flash-lite` for a small general model.
- Ollama for local-only use, accepting that latency depends on local hardware.

Routers such as `openrouter/auto` are useful for broad chat tasks but are not a
good default for per-keystroke ghost text because routing can dominate latency.

## Migration Notes

Older local installs used `~/.config/cmux/ai.toml` and Keychain service names
prefixed with `cmux-inline-ai-`. termTab reads those as a migration fallback so
existing users do not need to re-enter keys immediately. New installs write
only `~/.config/termtab/ai.toml` and `termtab-ai-*` Keychain services.

## Non-Goals

- Native terminal-grid overlays.
- Bash and fish support.
- Multi-line completions.
- Natural-language-to-command translation.
- Telemetry or hosted inference keys.

These can be added later without changing the zsh-first core.
