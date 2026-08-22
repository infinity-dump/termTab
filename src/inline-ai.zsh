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
# Transcripts hold raw terminal output; keep the whole dir owner-only.
command chmod 700 "${TERMTAB_SESSION_LOG:h}" 2>/dev/null

_termtab_ai_filesize() {
  local sz
  sz=$(wc -c < "$1" 2>/dev/null | tr -d ' \n')
  print -r -- "${sz:-0}"
}

_termtab_ai_command_has_secret() {
  emulate -L zsh
  local cmd="$1"
  [[ "$cmd" == *KEY=* || "$cmd" == *TOKEN=* || "$cmd" == *SECRET=* || "$cmd" == *PASSWORD=* ]] && return 0
  [[ "$cmd" == *AWS_* || "$cmd" == *sk-* || "$cmd" == *sk_ant_* || "$cmd" == *sk-ant-* ]] && return 0
  [[ "$cmd" == *ghp_* || "$cmd" == *github_pat_* || "$cmd" == *Bearer\ * ]] && return 0
  return 1
}

_termtab_ai_capture_enabled() {
  emulate -L zsh
  [[ "${TERMTAB_SCRIPT_ACTIVE:-0}" == 1 ]] && return 1
  [[ "${TERMTAB_INLINE_AI_NO_CAPTURE:-0}" == 1 ]] && return 1
  [[ -t 0 && -t 1 ]] || return 1
  [[ -r "$HOME/.config/termtab/inline-ai/pty_record.py" ]] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  local toml="$HOME/.config/termtab/ai.toml"
  [[ -r "$toml" ]] || return 0
  awk '
    /^[[:space:]]*\[output_capture\][[:space:]]*(#.*)?$/ {section=1; next}
    /^[[:space:]]*\[/ {section=0; next}
    section && /^[[:space:]]*enabled[[:space:]]*=[[:space:]]*false/ {found=1}
    END {exit (found ? 1 : 0)}
  ' "$toml"
}

if _termtab_ai_capture_enabled; then
  typeset -gx TERMTAB_SCRIPT_ACTIVE=1
  typeset -gx TERMTAB_TYPESCRIPT="${TERMTAB_SESSION_LOG:r}.tty"
  typeset -gx TERMTAB_OUTPUT_LAST="${TERMTAB_SESSION_LOG:r}.out.tsv"
  ( umask 077; : > "$TERMTAB_TYPESCRIPT" ) 2>/dev/null || true
  rm -f "$TERMTAB_OUTPUT_LAST" "${TERMTAB_SESSION_LOG:r}.last.tsv" 2>/dev/null || true
  find "${TERMTAB_SESSION_LOG:h}" -maxdepth 1 -type f \( -name '*.tty' -o -name '*.last.tsv' -o -name '*.out.tsv' \) -mtime +7 -delete 2>/dev/null
  # pty_record.py instead of script(1): BSD script sizes its inner pty once
  # and never forwards window resizes, which mangled fullscreen TUIs.
  exec python3 "$HOME/.config/termtab/inline-ai/pty_record.py" "$TERMTAB_TYPESCRIPT" -- zsh -i
fi

# Sidecars hold command lines and output ranges; create 0600 before first append.
( umask 077
  : >> "$TERMTAB_SESSION_LOG"
  [[ -n "${TERMTAB_OUTPUT_LAST:-}" ]] && : >> "$TERMTAB_OUTPUT_LAST"
) 2>/dev/null || true

_termtab_ai_preexec() {
  emulate -L zsh
  typeset -g TERMTAB_LAST_COMMAND="$1"
  typeset -g TERMTAB_LAST_CWD="$PWD"
  if [[ -n "${TERMTAB_TYPESCRIPT:-}" && -e "$TERMTAB_TYPESCRIPT" ]]; then
    typeset -g TERMTAB_LAST_OFFSET="$(_termtab_ai_filesize "$TERMTAB_TYPESCRIPT")"
  fi
}

_termtab_ai_precmd() {
  local exit_status="$?"
  emulate -L zsh
  local cmd="${TERMTAB_LAST_COMMAND:-}"
  local cwd="${TERMTAB_LAST_CWD:-$PWD}"
  local start_offset="${TERMTAB_LAST_OFFSET:-0}"
  unset TERMTAB_LAST_COMMAND TERMTAB_LAST_CWD TERMTAB_LAST_OFFSET

  [[ -z "$cmd" ]] && return
  _termtab_ai_command_has_secret "$cmd" && return

  cmd="${cmd//$'\n'/ }"
  cmd="${cmd//$'\t'/ }"
  cwd="${cwd//$'\n'/ }"
  cwd="${cwd//$'\t'/ }"
  local sep=$'\t'
  print -r -- "${EPOCHSECONDS:-0}${sep}${exit_status}${sep}${cwd}${sep}${cmd}" >> "$TERMTAB_SESSION_LOG" 2>/dev/null

  if [[ -n "${TERMTAB_OUTPUT_LAST:-}" && -n "${TERMTAB_TYPESCRIPT:-}" && -e "$TERMTAB_TYPESCRIPT" ]]; then
    local end_offset
    end_offset="$(_termtab_ai_filesize "$TERMTAB_TYPESCRIPT")"
    print -r -- "${EPOCHSECONDS:-0}${sep}${exit_status}${sep}${cwd}${sep}${cmd}${sep}${TERMTAB_TYPESCRIPT}${sep}${start_offset}${sep}${end_offset}" \
      >> "$TERMTAB_OUTPUT_LAST" 2>/dev/null
    local out_size
    out_size="$(_termtab_ai_filesize "$TERMTAB_OUTPUT_LAST")"
    if (( out_size > 262144 )); then
      local tmp="${TERMTAB_OUTPUT_LAST}.tmp"
      tail -c 131072 "$TERMTAB_OUTPUT_LAST" 2>/dev/null | sed '1d' > "$tmp" && mv "$tmp" "$TERMTAB_OUTPUT_LAST" 2>/dev/null
    fi
  fi
}

if (( $+functions[add-zsh-hook] )); then
  add-zsh-hook -d preexec _termtab_ai_preexec 2>/dev/null || true
  add-zsh-hook -d precmd _termtab_ai_precmd 2>/dev/null || true
  add-zsh-hook preexec _termtab_ai_preexec 2>/dev/null || true
  add-zsh-hook precmd _termtab_ai_precmd 2>/dev/null || true
fi

typeset -g _TERMTAB_AI_REPLACE_MARKER=$'\037termtab-replace\037'

_termtab_ai_clear_replacement() {
  emulate -L zsh
  if [[ -n "${TERMTAB_AI_REPLACEMENT:-}" ]]; then
    zle -M "" 2>/dev/null || true
  fi
  unset TERMTAB_AI_REPLACEMENT TERMTAB_AI_REPLACEMENT_BUFFER
}

_termtab_ai_show_replacement_hint() {
  emulate -L zsh
  local shown="$1"
  local max="${TERMTAB_AI_REPLACEMENT_HINT_MAX:-96}"
  (( max < 16 )) && max=16
  shown="${shown//$'\n'/ }"
  shown="${shown//$'\t'/ }"
  if (( ${#shown} > max )); then
    shown="${shown[1,$((max - 3))]}..."
  fi
  zle -M "termTab fix: Alt-R replaces with: ${shown}" 2>/dev/null || true
}

_termtab_ai_replacement_for_buffer() {
  emulate -L zsh
  local line="$BUFFER"
  [[ -z "$line" ]] && return 1
  [[ ${#line} -gt ${ZSH_AUTOSUGGEST_BUFFER_MAX_SIZE:-240} ]] && return 1
  builtin fc -AI 2>/dev/null
  TERMTAB_INLINE_AI_CWD="$PWD" python3 "$HOME/.config/termtab/inline-ai/inline_ai_complete.py" \
    --line "$line" --cwd "$PWD" --mode replace 2>/dev/null
}

_termtab_ai_replace_line() {
  emulate -L zsh
  local current="$BUFFER"
  local replacement=""

  if [[ -n "${TERMTAB_AI_REPLACEMENT:-}" && "${TERMTAB_AI_REPLACEMENT_BUFFER:-}" == "$current" ]]; then
    replacement="$TERMTAB_AI_REPLACEMENT"
  else
    replacement="$(_termtab_ai_replacement_for_buffer)"
  fi

  if [[ -n "$replacement" && "$replacement" != "$current" ]]; then
    BUFFER="$replacement"
    CURSOR=${#BUFFER}
    POSTDISPLAY=
    _termtab_ai_clear_replacement
    zle -M "termTab fix applied"
    zle -R
    return 0
  fi

  zle -M "termTab: no replacement available"
  zle -R
  return 0
}

_termtab_ai_restore_tab_completion() {
  emulate -L zsh
  local binding
  binding="$(bindkey '^I' 2>/dev/null || true)"
  if [[ "$binding" == *"autosuggest-accept"* || "$binding" == *"termtab-ai-"* ]]; then
    bindkey '^I' expand-or-complete
  fi
}

if [[ -r "$HOME/.zsh/zsh-autosuggestions/zsh-autosuggestions.zsh" ]]; then
  _zsh_autosuggest_strategy_termtab_ai() {
    emulate -L zsh
    typeset -g suggestion
    local line="$1"

    [[ ${#line} -gt ${ZSH_AUTOSUGGEST_BUFFER_MAX_SIZE:-240} ]] && return

    local result mode value sep
    sep=$'\t'
    builtin fc -AI 2>/dev/null
    result="$(TERMTAB_INLINE_AI_CWD="$PWD" python3 "$HOME/.config/termtab/inline-ai/inline_ai_complete.py" --line "$line" --cwd "$PWD" --format zsh 2>/dev/null)"
    mode="${result%%$sep*}"
    if [[ "$mode" == "$result" ]]; then
      mode="suggest"
      value="$result"
    else
      value="${result#*$sep}"
    fi

    if [[ "$mode" == "suggest" && -n "$value" && "$value" == "$line"* && "$value" != "$line" ]]; then
      suggestion="$value"
    elif [[ "$mode" == "replace" && -n "$value" && "$value" != "$line" ]]; then
      suggestion="${line}${_TERMTAB_AI_REPLACE_MARKER}${value}"
    fi
  }

  typeset -ga ZSH_AUTOSUGGEST_STRATEGY
  ZSH_AUTOSUGGEST_STRATEGY=(termtab_ai)
  ZSH_AUTOSUGGEST_BUFFER_MAX_SIZE="${ZSH_AUTOSUGGEST_BUFFER_MAX_SIZE:-240}"
  ZSH_AUTOSUGGEST_HIGHLIGHT_STYLE="${ZSH_AUTOSUGGEST_HIGHLIGHT_STYLE:-fg=8}"

  source "$HOME/.zsh/zsh-autosuggestions/zsh-autosuggestions.zsh"

  typeset -ga ZSH_AUTOSUGGEST_IGNORE_WIDGETS
  ZSH_AUTOSUGGEST_IGNORE_WIDGETS+=(termtab-ai-replace-line)

  if (( $+functions[_zsh_autosuggest_suggest] && ! $+functions[_termtab_ai_orig_zsh_autosuggest_suggest] )); then
    functions[_termtab_ai_orig_zsh_autosuggest_suggest]=$functions[_zsh_autosuggest_suggest]
  fi

  _zsh_autosuggest_suggest() {
    emulate -L zsh
    local raw="$1"
    local marker="${_TERMTAB_AI_REPLACE_MARKER:-}"
    local prefix="${BUFFER}${marker}"

    if [[ -n "$marker" && -n "$raw" && "${raw[1,${#prefix}]}" == "$prefix" ]]; then
      local replacement="${raw[$((${#prefix} + 1)),-1]}"
      if [[ -n "$replacement" && "$replacement" != "$BUFFER" ]]; then
        typeset -g TERMTAB_AI_REPLACEMENT="$replacement"
        typeset -g TERMTAB_AI_REPLACEMENT_BUFFER="$BUFFER"
        POSTDISPLAY=
        _termtab_ai_show_replacement_hint "$replacement"
        return 0
      fi
    fi

    _termtab_ai_clear_replacement
    _termtab_ai_orig_zsh_autosuggest_suggest "$raw"
  }

  zle -N termtab-ai-replace-line _termtab_ai_replace_line
  bindkey '^[r' termtab-ai-replace-line
  bindkey '^X^R' termtab-ai-replace-line
  _termtab_ai_restore_tab_completion
fi
