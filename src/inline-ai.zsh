# cmux inline AI autocomplete bridge for zsh.
# Uses zsh-autosuggestions for gray inline rendering and a BYOK helper for LLM completions.

if [[ -r "$HOME/.zsh/zsh-autosuggestions/zsh-autosuggestions.zsh" ]]; then
  _zsh_autosuggest_strategy_cmux_inline_ai() {
    emulate -L zsh
    typeset -g suggestion
    local line="$1"

    [[ -z "$line" ]] && return
    [[ ${#line} -gt ${ZSH_AUTOSUGGEST_BUFFER_MAX_SIZE:-240} ]] && return

    local result
    builtin fc -AI 2>/dev/null
    result="$(CMUX_INLINE_AI_CWD="$PWD" python3 "$HOME/.config/cmux/inline-ai/inline_ai_complete.py" --line "$line" --cwd "$PWD" 2>/dev/null)"
    if [[ -n "$result" && "$result" == "$line"* && "$result" != "$line" ]]; then
      suggestion="$result"
    fi
  }

  typeset -ga ZSH_AUTOSUGGEST_STRATEGY
  ZSH_AUTOSUGGEST_STRATEGY=(cmux_inline_ai history)
  ZSH_AUTOSUGGEST_BUFFER_MAX_SIZE="${ZSH_AUTOSUGGEST_BUFFER_MAX_SIZE:-240}"
  ZSH_AUTOSUGGEST_HIGHLIGHT_STYLE="${ZSH_AUTOSUGGEST_HIGHLIGHT_STYLE:-fg=8}"

  source "$HOME/.zsh/zsh-autosuggestions/zsh-autosuggestions.zsh"
  bindkey '^I' autosuggest-accept
fi
