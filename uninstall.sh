#!/bin/zsh
set -euo pipefail

zshrc="$HOME/.zshrc"
start_marker="# >>> termTab inline AI autocomplete >>>"
end_marker="# <<< termTab inline AI autocomplete <<<"
legacy_start_marker="# >>> cmux inline AI autocomplete >>>"
legacy_end_marker="# <<< cmux inline AI autocomplete <<<"

rm -rf "$HOME/.config/termtab/inline-ai"
rm -rf "$HOME/.config/cmux/inline-ai"
rm -f "$HOME/.local/bin/termtab-ai-key" "$HOME/.local/bin/cmux-inline-ai-key"

if [[ -f "$zshrc" ]]; then
  tmp="$(mktemp)"
  awk -v start="$start_marker" -v end="$end_marker" -v legacy_start="$legacy_start_marker" -v legacy_end="$legacy_end_marker" '
    $0 == start {skip=1; next}
    $0 == end {skip=0; next}
    $0 == legacy_start {skip=1; next}
    $0 == legacy_end {skip=0; next}
    $0 ~ /\.config\/termtab\/inline-ai\/inline-ai\.zsh/ {next}
    $0 ~ /\.config\/cmux\/inline-ai\/inline-ai\.zsh/ {next}
    $0 ~ /^# termTab inline AI autocomplete/ {next}
    $0 ~ /^# cmux inline AI autocomplete/ {next}
    skip != 1 {print}
  ' "$zshrc" > "$tmp"
  mv "$tmp" "$zshrc"
fi

print "Removed termTab inline AI autocomplete helper files."
print "Keychain entries were left intact. Remove manually with:"
print "security delete-generic-password -s termtab-ai-openrouter"
