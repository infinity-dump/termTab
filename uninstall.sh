#!/bin/zsh
set -euo pipefail

zshrc="$HOME/.zshrc"
start_marker="# >>> cmux inline AI autocomplete >>>"
end_marker="# <<< cmux inline AI autocomplete <<<"

rm -rf "$HOME/.config/cmux/inline-ai"
rm -f "$HOME/.local/bin/cmux-inline-ai-key"

if [[ -f "$zshrc" ]]; then
  tmp="$(mktemp)"
  awk -v start="$start_marker" -v end="$end_marker" '
    $0 == start {skip=1; next}
    $0 == end {skip=0; next}
    skip != 1 {print}
  ' "$zshrc" > "$tmp"
  mv "$tmp" "$zshrc"
fi

print "Removed cmux inline AI autocomplete helper files."
print "Keychain entries were left intact. Remove manually with:"
print "security delete-generic-password -s cmux-inline-ai-openrouter"
