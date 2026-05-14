#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    import tomllib
except Exception:  # pragma: no cover
    tomllib = None


CONFIG_PATH = pathlib.Path.home() / ".config" / "cmux" / "ai.toml"
CACHE_DIR = pathlib.Path.home() / ".cache" / "cmux" / "inline-ai"
DEFAULT_MODELS = {
    "anthropic": "claude-3-5-haiku-latest",
    "openai": "gpt-4o-mini",
    "openrouter": "mistralai/codestral-2508",
    "ollama": "llama3.2",
}

SECRET_PATTERNS = [
    re.compile(r"(?i)\b[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)\s*=\s*[^ \t;]+"),
    re.compile(r"(?i)\bAWS_[A-Z0-9_]+\s*=\s*[^ \t;]+"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
]


def load_config():
    config = {
        "inline": {"enabled": True, "debounce_ms": 120},
        "history": {
            "enabled": True,
            "direct_match_min_chars": 3,
            "max_entries": 8000,
            "max_bytes": 2097152,
            "top_matches": 8,
            "max_suggestion_chars": 320,
            "recency_weight": 100,
            "frequency_weight": 8,
        },
        "provider": {
            "name": "openrouter",
            "model": "mistralai/codestral-2508",
            "base_url": "https://openrouter.ai/api/v1",
            "sort": "latency",
            "max_tokens": 24,
        },
    }
    if CONFIG_PATH.exists() and tomllib is not None:
        try:
            parsed = tomllib.loads(CONFIG_PATH.read_text())
            for section in ("inline", "history", "provider"):
                if isinstance(parsed.get(section), dict):
                    config.setdefault(section, {}).update(parsed[section])
        except Exception:
            return config
    return config


def redact(text):
    out = text or ""
    for pattern in SECRET_PATTERNS:
        out = pattern.sub("<redacted>", out)
    return out


def contains_secret(text):
    return any(pattern.search(text or "") for pattern in SECRET_PATTERNS)


def keychain_password(service):
    account = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    cmd = ["security", "find-generic-password", "-a", account, "-s", service, "-w"]
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=2)
    except Exception:
        return None
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def provider_key(provider):
    provider = provider.lower()
    if provider == "anthropic":
        return keychain_password("cmux-inline-ai-anthropic") or os.environ.get("ANTHROPIC_API_KEY")
    if provider == "openai":
        return keychain_password("cmux-inline-ai-openai") or os.environ.get("OPENAI_API_KEY")
    if provider == "openrouter":
        return keychain_password("cmux-inline-ai-openrouter") or os.environ.get("OPENROUTER_API_KEY")
    return None


def history_path():
    return pathlib.Path(os.environ.get("HISTFILE", pathlib.Path.home() / ".zsh_history"))


def parse_history_command(raw):
    if raw.startswith(": ") and ";" in raw:
        raw = raw.split(";", 1)[1]
    return raw.strip()


def history_commands(max_entries=8000, max_bytes=2097152):
    histfile = history_path()
    if not histfile.exists():
        return []
    try:
        with histfile.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - int(max_bytes))
            handle.seek(start)
            text = handle.read().decode(errors="ignore")
    except Exception:
        return []
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    commands = []
    for raw in lines:
        command = parse_history_command(raw)
        if command and len(command) <= 512 and not contains_secret(command):
            commands.append(redact(command))
    return commands[-int(max_entries) :]


def head_command(line):
    try:
        parts = shlex.split(line, posix=True)
    except Exception:
        parts = line.strip().split()
    if not parts:
        return None
    command = parts[0]
    if not re.match(r"^[A-Za-z0-9_.+-]+$", command):
        return None
    return command


def ranked_history_matches(line, history_cfg, limit=None, prefix_only=True):
    if not line or not history_cfg.get("enabled", True):
        return []
    max_entries = int(history_cfg.get("max_entries", 8000) or 8000)
    max_bytes = int(history_cfg.get("max_bytes", 2097152) or 2097152)
    limit = int(limit or history_cfg.get("top_matches", 8) or 8)
    max_chars = int(history_cfg.get("max_suggestion_chars", 320) or 320)
    recency_weight = float(history_cfg.get("recency_weight", 100) or 100)
    frequency_weight = float(history_cfg.get("frequency_weight", 8) or 8)
    commands = history_commands(max_entries=max_entries, max_bytes=max_bytes)
    if not commands:
        return []

    line_head = head_command(line)
    aggregate = {}
    total = len(commands)
    for index, command in enumerate(commands):
        if command == line or len(command) > max_chars or contains_secret(command):
            continue
        if prefix_only:
            if not command.startswith(line):
                continue
        elif line_head and head_command(command) != line_head:
            continue
        elif not line_head:
            continue

        entry = aggregate.setdefault(command, {"command": command, "count": 0, "last_index": 0})
        entry["count"] += 1
        entry["last_index"] = index

    ranked = []
    for entry in aggregate.values():
        command = entry["command"]
        recency = (entry["last_index"] + 1) / max(total, 1)
        frequency = min(entry["count"], 20)
        suffix_len = max(len(command) - len(line), 0)
        concise_bonus = max(0, 1 - min(suffix_len, 200) / 200) * 10
        score = (recency_weight * recency) + (frequency_weight * frequency) + concise_bonus
        ranked.append({"command": command, "score": score, "count": entry["count"]})
    ranked.sort(key=lambda item: (-item["score"], -item["count"], item["command"]))
    return ranked[:limit]


def best_history_suggestion(line, history_cfg):
    if len(line.strip()) < int(history_cfg.get("direct_match_min_chars", 3) or 3):
        return ""
    matches = ranked_history_matches(line, history_cfg, limit=1, prefix_only=True)
    if not matches:
        return ""
    command = matches[0]["command"]
    if command.startswith(line) and command != line and not contains_secret(command):
        return command
    return ""


def recent_history(limit=8, history_cfg=None):
    if history_cfg:
        commands = history_commands(
            max_entries=min(int(history_cfg.get("max_entries", 8000) or 8000), 200),
            max_bytes=min(int(history_cfg.get("max_bytes", 2097152) or 2097152), 262144),
        )
        return commands[-limit:]
    commands = history_commands(max_entries=200, max_bytes=262144)
    return commands[-limit:]


def help_cache_key(command, resolved):
    stat = pathlib.Path(resolved).stat()
    material = f"{command}\0{resolved}\0{stat.st_mtime_ns}\0{stat.st_size}"
    return hashlib.sha256(material.encode()).hexdigest()


def cached_help(command):
    resolved = shutil.which(command)
    if not resolved:
        return ""
    try:
        key = help_cache_key(command, resolved)
    except Exception:
        return ""
    path = CACHE_DIR / "help" / f"{key}.txt"
    if path.exists():
        try:
            return path.read_text(errors="ignore")[:24576]
        except Exception:
            return ""
    try:
        result = subprocess.run(
            [resolved, "--help"],
            text=True,
            capture_output=True,
            timeout=0.8,
            env={k: v for k, v in os.environ.items() if not re.search(r"(?i)(KEY|TOKEN|SECRET|PASSWORD)|^AWS_", k)},
        )
    except Exception:
        return ""
    text = redact((result.stdout or "") + ("\n" + result.stderr if result.stderr else ""))
    text = text[:24576]
    if text:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        except Exception:
            pass
    return text


def build_prompt(line, cwd, history_cfg=None):
    command = head_command(line) or ""
    history_cfg = history_cfg or {}
    history = "\n".join(f"- {item}" for item in recent_history(history_cfg=history_cfg))
    weighted_history = ranked_history_matches(line, history_cfg, prefix_only=True)
    if not weighted_history:
        weighted_history = ranked_history_matches(line, history_cfg, prefix_only=False)
    weighted_history_text = "\n".join(
        f"- score={item['score']:.1f} count={item['count']}: {item['command']}" for item in weighted_history
    )
    help_text = cached_help(command) if command else ""
    system = (
        "You are cmux inline autocomplete. Complete the current terminal command line like "
        "Warp autocomplete. Return only the suffix that should be inserted at the cursor. "
        "Do not repeat the existing prefix. Do not include explanations, markdown, quotes, "
        "or trailing newline. Prefer highly weighted history matches, then exact flags/subcommands "
        "from provided help. If no useful completion is likely, return an empty string."
    )
    user = f"""cwd: {cwd}
shell: zsh
line_before_cursor: {redact(line)}
line_after_cursor:
head_command: {command}

recent_terminal_history:
{history}

weighted_history_matches:
{weighted_history_text}

cached_help_for_head_command:
{help_text}

constraints:
- Single-line command suffix only.
- Must be safe to insert literally at the cursor.
- Prefer a weighted history match when it is compatible with the current prefix.
- Do not invent secrets, paths, hostnames, or destructive flags.
- If the user is typing a flag prefix, complete the most likely flag.
"""
    return system, user


def http_json(url, headers, payload, timeout=4):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


def complete_anthropic(model, key, system, user):
    payload = {
        "model": model,
        "max_tokens": 64,
        "temperature": 0,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    response = http_json(
        "https://api.anthropic.com/v1/messages",
        {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": key,
        },
        payload,
    )
    parts = response.get("content") or []
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


def complete_openai(model, key, base_url, system, user, extra_headers=None, max_tokens=64, provider_options=None):
    base = (base_url or "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    if provider_options:
        payload["provider"] = provider_options
    headers = {"content-type": "application/json", "authorization": f"Bearer {key}"}
    if extra_headers:
        headers.update(extra_headers)
    response = http_json(f"{base}/chat/completions", headers, payload)
    return response.get("choices", [{}])[0].get("message", {}).get("content", "")


def complete_openrouter(model, key, base_url, system, user, max_tokens=24, provider_options=None):
    return complete_openai(
        model,
        key,
        base_url or "https://openrouter.ai/api/v1",
        system,
        user,
        {
            "HTTP-Referer": "https://github.com/manaflow-ai/cmux",
            "X-OpenRouter-Title": "cmux inline AI",
        },
        max_tokens,
        provider_options,
    )


def complete_ollama(model, base_url, system, user):
    base = (base_url or "http://127.0.0.1:11434").rstrip("/")
    payload = {
        "model": model,
        "prompt": f"{system}\n\n{user}",
        "stream": False,
        "options": {"temperature": 0, "num_predict": 64},
    }
    response = http_json(f"{base}/api/generate", {"content-type": "application/json"}, payload)
    return response.get("response", "")


def merge_completion(line, raw):
    text = (raw or "").strip().strip("`").splitlines()[0].strip() if raw else ""
    if not text:
        return ""
    if text.startswith(line):
        return text
    token_prefix = line[line.rfind(" ") + 1 :] if " " in line else line
    if token_prefix and text.startswith(token_prefix):
        text = text[len(token_prefix) :]
    if len(text) > 160:
        return ""
    return line + text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--line", required=True)
    parser.add_argument("--cwd", default=os.getcwd())
    args = parser.parse_args()

    line = args.line
    if len(line.strip()) < 2 or len(line) > 240 or contains_secret(line):
        return 0

    config = load_config()
    if not config.get("inline", {}).get("enabled", True):
        return 0

    history_cfg = config.get("history", {})
    history_suggestion = best_history_suggestion(line, history_cfg)
    if history_suggestion:
        sys.stdout.write(history_suggestion)
        return 0

    provider_cfg = config.get("provider", {})
    provider = str(provider_cfg.get("name", "openrouter")).lower()
    model = str(provider_cfg.get("model") or DEFAULT_MODELS.get(provider, "mistralai/codestral-2508"))
    max_tokens = int(provider_cfg.get("max_tokens") or (24 if provider == "openrouter" else 64))
    max_tokens = max(16, min(max_tokens, 128))
    provider_options = {}
    if provider == "openrouter" and provider_cfg.get("sort"):
        provider_options["sort"] = str(provider_cfg.get("sort"))
    key = None
    if provider in ("anthropic", "openai", "openrouter"):
        key = provider_key(provider)
        if not key:
            return 0

    debounce_ms = int(config.get("inline", {}).get("debounce_ms", 120) or 120)
    if debounce_ms > 0:
        time.sleep(min(debounce_ms, 1000) / 1000)

    system, user = build_prompt(line, args.cwd, history_cfg=history_cfg)

    try:
        if provider == "anthropic":
            raw = complete_anthropic(model, key, system, user)
        elif provider == "openai":
            raw = complete_openai(model, key, str(provider_cfg.get("base_url", "")), system, user, max_tokens=max_tokens)
        elif provider == "openrouter":
            raw = complete_openrouter(
                model,
                key,
                str(provider_cfg.get("base_url", "")),
                system,
                user,
                max_tokens=max_tokens,
                provider_options=provider_options or None,
            )
        elif provider == "ollama":
            raw = complete_ollama(model, str(provider_cfg.get("base_url", "")), system, user)
        else:
            return 0
    except (urllib.error.URLError, TimeoutError, subprocess.SubprocessError, OSError, KeyError, ValueError, IndexError, TypeError):
        return 0

    suggestion = merge_completion(line, raw)
    if suggestion and suggestion.startswith(line) and not contains_secret(suggestion):
        sys.stdout.write(suggestion)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
