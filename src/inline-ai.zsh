# termTab inline AI autocomplete bridge for zsh.
# Uses zsh-autosuggestions for gray inline rendering and a BYOK helper for LLM completions.

zmodload zsh/datetime 2>/dev/null || true
autoload -Uz add-zsh-hook 2>/dev/null || true

if [[ -z "${TERMTAB_SESSION_ID:-}" ]]; then
  typeset -gx TERMTAB_SESSION_ID="${HOST:-host}-${$}-${EPOCHSECONDS:-0}-${RANDOM}"
  TERMTAB_SESSION_ID="${TERMTAB_SESSION_ID//[^A-Za-z0-9_.-]/_}"
fi

if [[ -z "${TERMTAB_SESSION_LOG:-}" ]]; then
  typeset -gx TERMTAB_SESSION_LOG="$HOME/.cache/termtab/inline-ai/sessions/${TERMTAB_SESSION_ID}.log"
fi

command mkdir -p "${TERMTAB_SESSION_LOG:h}" 2>/dev/null

_termtab_ai_command_has_secret() {
  emulate -L zsh
  local cmd="$1"
  [[ "$cmd" == *KEY=* || "$cmd" == *TOKEN=* || "$cmd" == *SECRET=* || "$cmd" == *PASSWORD=* ]] && return 0
  [[ "$cmd" == *AWS_* || "$cmd" == *sk-* || "$cmd" == *sk_ant_* || "$cmd" == *sk-ant-* ]] && return 0
  [[ "$cmd" == *ghp_* || "$cmd" == *github_pat_* || "$cmd" == *Bearer\ * ]] && return 0
  return 1
}

_termtab_ai_preexec() {
  emulate -L zsh
  typeset -g TERMTAB_LAST_COMMAND="$1"
  typeset -g TERMTAB_LAST_CWD="$PWD"
}

_termtab_ai_precmd() {
  emulate -L zsh
  local exit_status="$?"
  local cmd="${TERMTAB_LAST_COMMAND:-}"
  local cwd="${TERMTAB_LAST_CWD:-$PWD}"
  unset TERMTAB_LAST_COMMAND TERMTAB_LAST_CWD

  [[ -z "$cmd" ]] && return
  _termtab_ai_command_has_secret "$cmd" && return

  cmd="${cmd//$'\n'/ }"
  cmd="${cmd//$'\t'/ }"
  cwd="${cwd//$'\n'/ }"
  cwd="${cwd//$'\t'/ }"
  local sep=$'\t'
  print -r -- "${EPOCHSECONDS:-0}${sep}${exit_status}${sep}${cwd}${sep}${cmd}" >> "$TERMTAB_SESSION_LOG" 2>/dev/null
}

if (( $+functions[add-zsh-hook] )); then
  add-zsh-hook -d preexec _termtab_ai_preexec 2>/dev/null || true
  add-zsh-hook -d precmd _termtab_ai_precmd 2>/dev/null || true
  add-zsh-hook preexec _termtab_ai_preexec 2>/dev/null || true
  add-zsh-hook precmd _termtab_ai_precmd 2>/dev/null || true
fi

if [[ -r "$HOME/.zsh/zsh-autosuggestions/zsh-autosuggestions.zsh" ]]; then
  _zsh_autosuggest_strategy_termtab_ai() {
    emulate -L zsh
    typeset -g suggestion
    local line="$1"

    [[ ${#line} -gt ${ZSH_AUTOSUGGEST_BUFFER_MAX_SIZE:-240} ]] && return

    local result
    builtin fc -AI 2>/dev/null
    result="$(TERMTAB_INLINE_AI_CWD="$PWD" python3 "$HOME/.config/termtab/inline-ai/inline_ai_complete.py" --line "$line" --cwd "$PWD" 2>/dev/null)"
    if [[ -n "$result" && "$result" == "$line"* && "$result" != "$line" ]]; then
      suggestion="$result"
    fi
  }

  typeset -ga ZSH_AUTOSUGGEST_STRATEGY
  ZSH_AUTOSUGGEST_STRATEGY=(termtab_ai history)
  ZSH_AUTOSUGGEST_BUFFER_MAX_SIZE="${ZSH_AUTOSUGGEST_BUFFER_MAX_SIZE:-240}"
  ZSH_AUTOSUGGEST_HIGHLIGHT_STYLE="${ZSH_AUTOSUGGEST_HIGHLIGHT_STYLE:-fg=8}"

  source "$HOME/.zsh/zsh-autosuggestions/zsh-autosuggestions.zsh"
  bindkey '^I' autosuggest-accept
fi
