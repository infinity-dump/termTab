#!/bin/zsh
set -euo pipefail

repo_dir="${0:A:h}"
config_dir="$HOME/.config/cmux"
inline_dir="$config_dir/inline-ai"
bin_dir="$HOME/.local/bin"
autosuggestions_dir="$HOME/.zsh/zsh-autosuggestions"
zshrc="$HOME/.zshrc"

mkdir -p "$inline_dir" "$bin_dir" "$HOME/.zsh"

if [[ ! -r "$autosuggestions_dir/zsh-autosuggestions.zsh" ]]; then
  git clone --depth 1 https://github.com/zsh-users/zsh-autosuggestions "$autosuggestions_dir"
fi

install -m 0755 "$repo_dir/src/inline_ai_complete.py" "$inline_dir/inline_ai_complete.py"
install -m 0644 "$repo_dir/src/inline-ai.zsh" "$inline_dir/inline-ai.zsh"
install -m 0755 "$repo_dir/src/cmux-inline-ai-key" "$bin_dir/cmux-inline-ai-key"

if [[ ! -f "$config_dir/ai.toml" ]]; then
  install -m 0600 "$repo_dir/config/ai.example.toml" "$config_dir/ai.toml"
fi

start_marker="# >>> cmux inline AI autocomplete >>>"
end_marker="# <<< cmux inline AI autocomplete <<<"
block="${start_marker}
if [[ -r \"\$HOME/.config/cmux/inline-ai/inline-ai.zsh\" ]]; then
  source \"\$HOME/.config/cmux/inline-ai/inline-ai.zsh\"
fi
${end_marker}"

touch "$zshrc"
if ! grep -Fq "$start_marker" "$zshrc"; then
  {
    print ""
    print "$block"
  } >> "$zshrc"
fi

print "Installed cmux inline AI autocomplete."
print "Store a key with: cmux-inline-ai-key"
print "Open a new terminal, or run: source ~/.zshrc"
