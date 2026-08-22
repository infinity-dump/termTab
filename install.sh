#!/bin/zsh
set -euo pipefail

repo_dir="${0:A:h}"
config_dir="$HOME/.config/termtab"
inline_dir="$config_dir/inline-ai"
bin_dir="$HOME/.local/bin"
autosuggestions_dir="$HOME/.zsh/zsh-autosuggestions"
zshrc="$HOME/.zshrc"
legacy_config="$HOME/.config/cmux/ai.toml"

mkdir -p "$inline_dir" "$bin_dir" "$HOME/.zsh"

if [[ ! -r "$autosuggestions_dir/zsh-autosuggestions.zsh" ]]; then
  git clone --depth 1 https://github.com/zsh-users/zsh-autosuggestions "$autosuggestions_dir"
fi

install -m 0755 "$repo_dir/src/inline_ai_complete.py" "$inline_dir/inline_ai_complete.py"
install -m 0755 "$repo_dir/src/pty_record.py" "$inline_dir/pty_record.py"
install -m 0644 "$repo_dir/src/inline-ai.zsh" "$inline_dir/inline-ai.zsh"
install -m 0755 "$repo_dir/src/termtab-ai-key" "$bin_dir/termtab-ai-key"

if [[ ! -f "$config_dir/ai.toml" ]]; then
  if [[ -f "$legacy_config" ]]; then
    install -m 0600 "$legacy_config" "$config_dir/ai.toml"
  else
    install -m 0600 "$repo_dir/config/ai.example.toml" "$config_dir/ai.toml"
  fi
fi

start_marker="# >>> termTab inline AI autocomplete >>>"
end_marker="# <<< termTab inline AI autocomplete <<<"
legacy_start_marker="# >>> cmux inline AI autocomplete >>>"
legacy_end_marker="# <<< cmux inline AI autocomplete <<<"
block="${start_marker}
if [[ -r \"\$HOME/.config/termtab/inline-ai/inline-ai.zsh\" ]]; then
  source \"\$HOME/.config/termtab/inline-ai/inline-ai.zsh\"
fi
${end_marker}"

touch "$zshrc"
tmp="$(mktemp)"
awk -v start="$legacy_start_marker" -v end="$legacy_end_marker" '
  $0 == start {skip=1; next}
  $0 == end {skip=0; next}
  $0 ~ /\.config\/cmux\/inline-ai\/inline-ai\.zsh/ {next}
  $0 ~ /^# cmux inline AI autocomplete/ {next}
  skip != 1 {print}
' "$zshrc" > "$tmp"
mv "$tmp" "$zshrc"

if ! grep -Fq "$start_marker" "$zshrc"; then
  {
    print ""
    print "$block"
  } >> "$zshrc"
fi

rm -rf "$HOME/.config/cmux/inline-ai"
rm -f "$HOME/.local/bin/cmux-inline-ai-key"

print "Installed termTab inline AI autocomplete."
print "Store a key with: termtab-ai-key"
print "Open a new terminal, or run: source ~/.zshrc"
