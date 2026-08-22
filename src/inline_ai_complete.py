#!/usr/bin/env python3
import argparse
import difflib
import hashlib
import json
import os
import pathlib
import posixpath
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


CONFIG_PATH = pathlib.Path(os.environ.get("TERMTAB_AI_CONFIG", pathlib.Path.home() / ".config" / "termtab" / "ai.toml"))
LEGACY_CONFIG_PATH = pathlib.Path.home() / ".config" / "cmux" / "ai.toml"
CACHE_DIR = pathlib.Path.home() / ".cache" / "termtab" / "inline-ai"
KEYCHAIN_PREFIX = "termtab-ai-"
LEGACY_KEYCHAIN_PREFIX = "cmux-inline-ai-"
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
    # DOTALL block: redact the whole key body, or to EOF when the END
    # marker was truncated out of the captured output.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        re.DOTALL,
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
]

ANSI_PATTERN = re.compile(
    rb"\x1b(?:\[[?0-9;]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[PX^_].*?\x1b\\|[@-Z\\-_])",
    re.DOTALL,
)

OUTPUT_MAX_BYTES = 32768
OUTPUT_MAX_LINES = 150
OUTPUT_TAIL_FOR_PROMPT = 4096
OUTPUT_TAIL_FOR_CORRECTION = 8192
PATH_BIN_CACHE_TTL = 300


def load_config():
    config = {
        "inline": {"enabled": True, "debounce_ms": 120},
        "history": {
            "enabled": True,
            "session_enabled": True,
            "session_max_entries": 200,
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
    config_path = CONFIG_PATH if CONFIG_PATH.exists() else LEGACY_CONFIG_PATH
    if config_path.exists() and tomllib is not None:
        try:
            parsed = tomllib.loads(config_path.read_text())
            for section in ("inline", "history", "provider"):
                if isinstance(parsed.get(section), dict):
                    config.setdefault(section, {}).update(parsed[section])
        except Exception:
            return config
    return config


def _expanduser_safe(raw):
    # ~unknownuser raises RuntimeError (not OSError); keep the literal path.
    path = pathlib.Path(raw)
    try:
        return path.expanduser()
    except (OSError, RuntimeError):
        return path


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
        return (
            keychain_password(KEYCHAIN_PREFIX + "anthropic")
            or keychain_password(LEGACY_KEYCHAIN_PREFIX + "anthropic")
            or os.environ.get("ANTHROPIC_API_KEY")
        )
    if provider == "openai":
        return (
            keychain_password(KEYCHAIN_PREFIX + "openai")
            or keychain_password(LEGACY_KEYCHAIN_PREFIX + "openai")
            or os.environ.get("OPENAI_API_KEY")
        )
    if provider == "openrouter":
        return (
            keychain_password(KEYCHAIN_PREFIX + "openrouter")
            or keychain_password(LEGACY_KEYCHAIN_PREFIX + "openrouter")
            or os.environ.get("OPENROUTER_API_KEY")
        )
    return None


def history_path():
    return pathlib.Path(os.environ.get("HISTFILE", pathlib.Path.home() / ".zsh_history"))


def session_log_path():
    raw = os.environ.get("TERMTAB_SESSION_LOG")
    return pathlib.Path(raw).expanduser() if raw else None


def parse_history_command(raw):
    if raw.startswith(": ") and ";" in raw:
        raw = raw.split(";", 1)[1]
    return raw.strip()


def parse_session_line(raw):
    parts = raw.rstrip("\n").split("\t", 3)
    if len(parts) != 4:
        return None
    timestamp, status, cwd, command = parts
    command = command.strip()
    if not command or len(command) > 512 or contains_secret(command):
        return None
    try:
        status_int = int(status)
    except ValueError:
        status_int = 0
    return {"timestamp": timestamp, "status": status_int, "cwd": cwd, "command": redact(command)}


def sanitize_typescript(data):
    if not data:
        return ""
    cleaned = ANSI_PATTERN.sub(b"", data)
    cleaned = cleaned.replace(b"\r\n", b"\n").replace(b"\x00", b"")
    text = cleaned.decode("utf-8", errors="replace")
    out_lines = []
    for line in text.split("\n"):
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        if "\b" in line:
            buf = []
            for ch in line:
                if ch == "\b":
                    if buf:
                        buf.pop()
                else:
                    buf.append(ch)
            line = "".join(buf)
        out_lines.append(line)
    return "\n".join(out_lines)


def trim_output(text, max_lines=OUTPUT_MAX_LINES, max_bytes=OUTPUT_MAX_BYTES):
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    out = "\n".join(lines)
    if len(out.encode("utf-8", errors="replace")) > max_bytes:
        out = out.encode("utf-8", errors="replace")[-max_bytes:].decode("utf-8", errors="replace")
    return out


def last_event_path():
    raw = os.environ.get("TERMTAB_OUTPUT_LAST")
    return pathlib.Path(raw).expanduser() if raw else None


def parse_last_event_line(raw):
    parts = raw.rstrip("\n").split("\t", 6)
    if len(parts) != 7:
        return None
    timestamp, status, cwd, command, tty_path, start, end = parts
    if not command or contains_secret(command):
        return None
    try:
        return {
            "ts": int(timestamp or 0),
            "exit": int(status or 0),
            "cwd": cwd,
            "cmd": redact(command),
            "tty": tty_path,
            "start": int(start or 0),
            "end": int(end or 0),
        }
    except ValueError:
        return None


def read_typescript_slice(tty_path, start, end, max_bytes=OUTPUT_MAX_BYTES):
    if not tty_path or start is None or end is None or end <= start:
        return ""
    try:
        path = pathlib.Path(tty_path).expanduser()
        size = path.stat().st_size
    except (OSError, ValueError):
        return ""
    start = max(0, min(int(start), size))
    end = max(start, min(int(end), size))
    length = end - start
    if length <= 0:
        return ""
    if length > max_bytes:
        start = end - max_bytes
        length = max_bytes
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read(length)
    except OSError:
        return ""
    return sanitize_typescript(raw)


def read_recent_terminal_events(max_records=50):
    path = last_event_path()
    if not path or not path.exists():
        return []
    try:
        lines = path.read_text(errors="ignore").splitlines()[-int(max_records):]
    except OSError:
        return []
    events = []
    for line in lines:
        event = parse_last_event_line(line)
        if event:
            events.append(event)
    return events


def _populate_output(event):
    if event is None:
        return None
    raw_output = read_typescript_slice(event.get("tty"), event.get("start"), event.get("end"))
    event["output"] = redact(trim_output(raw_output))
    return event


def last_terminal_event():
    events = read_recent_terminal_events(max_records=1)
    if not events:
        return None
    return _populate_output(events[-1])


def find_retry_context(line, current_cwd, max_records=30):
    if not line:
        return None
    target_cwd = (current_cwd or "").rstrip("/")
    for event in reversed(read_recent_terminal_events(max_records=max_records)):
        event_cwd = (event.get("cwd") or "").rstrip("/")
        if target_cwd and event_cwd and event_cwd != target_cwd:
            continue
        if not is_retry_intent(line, event.get("cmd") or ""):
            continue
        event = _populate_output(event)
        if event.get("exit", 0) == 0 and not output_looks_failed(event.get("output") or ""):
            continue
        return event
    return None


def cached_path_executables():
    cache_path = CACHE_DIR / "path-bins.txt"
    try:
        mtime = cache_path.stat().st_mtime
        if time.time() - mtime < PATH_BIN_CACHE_TTL:
            return cache_path.read_text(errors="ignore").splitlines()
    except OSError:
        pass
    bins = set()
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        if not directory:
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=True) and os.access(entry.path, os.X_OK):
                            bins.add(entry.name)
                    except OSError:
                        continue
        except OSError:
            continue
    listing = sorted(bins)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("\n".join(listing))
    except OSError:
        pass
    return listing


def damerau_levenshtein(a, b, max_d=2):
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_d:
        return max_d + 1
    n, m = len(a), len(b)
    if n == 0:
        return m
    if m == 0:
        return n
    prev_prev = [0] * (m + 1)
    prev = list(range(m + 1))
    cur = [0] * (m + 1)
    for i in range(1, n + 1):
        cur[0] = i
        row_min = cur[0]
        for j in range(1, m + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev_prev[j - 2] + 1)
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > max_d:
            return max_d + 1
        prev_prev = prev[:]
        prev = cur[:]
    return prev[m]


def closest_command(name):
    if not name or len(name) < 2:
        return ""
    candidates = cached_path_executables()
    if not candidates:
        return ""
    max_distance = 1 if len(name) <= 3 else 2
    best = ""
    best_d = max_distance + 1
    for candidate in candidates:
        if abs(len(candidate) - len(name)) > max_distance:
            continue
        distance = damerau_levenshtein(name, candidate, max_d=max_distance)
        if distance < best_d:
            best_d = distance
            best = candidate
            if distance == 1:
                break
    return best


COMMAND_NOT_FOUND_RE = re.compile(r"(?:command not found|not found):\s*(\S+)", re.IGNORECASE)
GIT_NOT_A_COMMAND_RE = re.compile(r"git: '([^']+)' is not a git command")
GIT_DID_YOU_MEAN_RE = re.compile(r"The most similar commands?\s+(?:is|are)\s*\n((?:\s+\S+\n?)+)", re.IGNORECASE)
DID_YOU_MEAN_RE = re.compile(r"[Dd]id you mean\s*['\"]?(\S+?)['\"]?[\?\.]")
NPM_DID_YOU_MEAN_RE = re.compile(r"Did you mean (?:this\??|one of these\??)?\n((?:\s+\S+.*\n?)+)", re.IGNORECASE)
NO_SUCH_FILE_RE = re.compile(
    r"(?:No such file or directory|cannot stat|cannot access|cannot find|does not exist)",
    re.IGNORECASE,
)
PATH_LIKE_TOKEN_RE = re.compile(r"^[~./]")
ILLEGAL_OPTION_RE = re.compile(
    r"(?:illegal option|invalid option|unrecognized option|unknown option)\s*"
    r"(?:--\s*)?['\"]?(-{0,2}[A-Za-z0-9][\w-]*)['\"]?",
    re.IGNORECASE,
)
FAILURE_OUTPUT_RE = re.compile(
    r"(?:No such file or directory|cannot stat|cannot access|cannot find|does not exist|"
    r"command not found|not found:\s*\S+|is not a git command|Did you mean|"
    r"illegal option|invalid option|unrecognized option|unknown option)",
    re.IGNORECASE,
)


def output_looks_failed(output):
    return bool(output and FAILURE_OUTPUT_RE.search(output))


def _replace_first_word(cmd, old_word, new_word):
    tokens = cmd.split()
    if not tokens or not new_word or contains_secret(new_word):
        return ""
    if tokens[0] == old_word:
        tokens[0] = new_word
        return " ".join(tokens)
    return ""


def _replace_subcommand(cmd, old_sub, new_sub):
    tokens = cmd.split()
    if len(tokens) < 2 or not new_sub or contains_secret(new_sub):
        return ""
    for index in range(1, len(tokens)):
        if tokens[index] == old_sub:
            tokens[index] = new_sub
            return " ".join(tokens)
    return ""


def _shellquote_command(parts):
    out = []
    for part in parts:
        if part and re.fullmatch(r"[\w./@:+,=%~-]+", part):
            out.append(part)
        else:
            out.append(shlex.quote(part))
    return " ".join(out)


def _looks_like_path_arg(arg):
    if not arg:
        return False
    if arg.startswith("-"):
        return False
    if "*" in arg or "?" in arg or "$" in arg or "`" in arg:
        return False
    if "=" in arg and not PATH_LIKE_TOKEN_RE.match(arg):
        return False
    return True


def detect_path_correction(failed_cmd, output, cwd):
    if not failed_cmd or not output or not NO_SUCH_FILE_RE.search(output):
        return ""
    try:
        parts = shlex.split(failed_cmd, posix=True)
    except ValueError:
        return ""
    if len(parts) < 2:
        return ""
    base_dir = pathlib.Path(cwd or ".").expanduser()
    head = parts[0]
    # For mv/cp/ln, the LAST argument is the destination and may legitimately not exist.
    skip_last = head in {"mv", "cp", "ln", "rsync", "install"}
    fixed = list(parts)
    upper = len(parts) - 1 if skip_last else len(parts)
    for index in range(1, upper):
        arg = parts[index]
        if not _looks_like_path_arg(arg):
            continue
        candidate = _expanduser_safe(arg)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        if candidate.exists():
            continue
        parent = candidate.parent
        if not parent.exists() or not parent.is_dir():
            continue
        try:
            names = [entry.name for entry in parent.iterdir() if not entry.name.startswith(".")]
        except OSError:
            continue
        if not names:
            continue
        target_name = candidate.name
        cutoff = 0.62 if len(target_name) >= 4 else 0.78
        matches = difflib.get_close_matches(target_name, names, n=1, cutoff=cutoff)
        if not matches:
            continue
        repaired_name = matches[0]
        if repaired_name == target_name:
            continue
        dir_part, _basename = posixpath.split(arg)
        new_arg = posixpath.join(dir_part, repaired_name) if dir_part else repaired_name
        if contains_secret(new_arg):
            continue
        fixed[index] = new_arg
        return _shellquote_command(fixed)
    return ""


def detect_illegal_option_correction(failed_cmd, output):
    if not failed_cmd or not output:
        return ""
    match = ILLEGAL_OPTION_RE.search(output)
    if not match:
        return ""
    raw_bad = match.group(1)
    bad = raw_bad.lstrip("-")
    if not bad or len(bad) > 32:
        return ""
    try:
        parts = shlex.split(failed_cmd, posix=True)
    except ValueError:
        return ""
    if not parts:
        return ""
    new_parts = [parts[0]]
    removed = False
    for token in parts[1:]:
        if not removed and token.startswith("--"):
            if token == "--" + bad or token.startswith("--" + bad + "="):
                removed = True
                continue
        elif not removed and token.startswith("-") and len(token) > 1:
            if len(bad) == 1 and bad in token[1:]:
                stripped = "-" + token[1:].replace(bad, "", 1)
                if stripped == "-":
                    removed = True
                    continue
                token = stripped
                removed = True
            elif token == "-" + bad:
                removed = True
                continue
        new_parts.append(token)
    if not removed or len(new_parts) < 2:
        return ""
    fix = _shellquote_command(new_parts)
    if contains_secret(fix):
        return ""
    return fix


def detect_correction(failed_cmd, output, cwd=None):
    if not failed_cmd or not output:
        return ""
    text = output

    match = GIT_NOT_A_COMMAND_RE.search(text)
    if match:
        wrong_sub = match.group(1)
        similar = GIT_DID_YOU_MEAN_RE.search(text)
        if similar:
            for candidate in re.findall(r"\s+(\S+)", similar.group(1)):
                fix = _replace_subcommand(failed_cmd, wrong_sub, candidate)
                if fix:
                    return fix

    cnf = COMMAND_NOT_FOUND_RE.search(text)
    if cnf:
        wrong = cnf.group(1)
        suggestion = closest_command(wrong)
        if suggestion and suggestion != wrong:
            fix = _replace_first_word(failed_cmd, wrong, suggestion)
            if fix:
                return fix

    dym = DID_YOU_MEAN_RE.search(text)
    if dym:
        candidate = dym.group(1)
        tokens = failed_cmd.split()
        if tokens:
            for index, token in enumerate(tokens):
                if token != candidate and difflib.SequenceMatcher(None, token, candidate).ratio() >= 0.6:
                    tokens[index] = candidate
                    fix = " ".join(tokens)
                    if not contains_secret(fix):
                        return fix

    path_fix = detect_path_correction(failed_cmd, text, cwd)
    if path_fix:
        return path_fix

    flag_fix = detect_illegal_option_correction(failed_cmd, text)
    if flag_fix:
        return flag_fix
    return ""


def is_retry_intent(line, failed_cmd):
    line_norm = (line or "").strip()
    if not line_norm or not failed_cmd:
        return False
    failed_first = failed_cmd.split()[0] if failed_cmd.split() else ""
    line_first = line_norm.split()[0] if line_norm.split() else ""
    if not failed_first or not line_first:
        return False
    if failed_cmd.startswith(line_norm):
        return True
    if line_first == failed_first and difflib.SequenceMatcher(None, line_norm, failed_cmd).ratio() >= 0.72:
        return True
    if line_first != failed_first:
        if line_first and failed_first.startswith(line_first[:1]):
            if damerau_levenshtein(line_first, failed_first[: len(line_first) + 1], max_d=2) <= 1:
                return True
        if damerau_levenshtein(line_first, failed_first, max_d=2) <= 2:
            return True
    return False


def correction_suggestion(line, current_cwd, event, require_prefix=True):
    if not event:
        return ""
    if not line or len(line.strip()) < 1:
        return ""
    failed_cmd = event.get("cmd") or ""
    output = event.get("output") or ""
    event_cwd = (event.get("cwd") or "").rstrip("/") or current_cwd
    corrected = detect_correction(failed_cmd, output, cwd=event_cwd or current_cwd)
    if not corrected:
        return ""
    if corrected == line:
        return ""
    if require_prefix and not corrected.startswith(line):
        return ""
    if contains_secret(corrected) or len(corrected) > 320:
        return ""
    return corrected


def session_events(max_entries=200):
    path = session_log_path()
    if not path or not path.exists():
        return []
    try:
        lines = path.read_text(errors="ignore").splitlines()[-int(max_entries) :]
    except Exception:
        return []
    events = []
    for line in lines:
        event = parse_session_line(line)
        if event:
            events.append(event)
    return events


def session_commands(max_entries=200):
    return [event["command"] for event in session_events(max_entries=max_entries)]


def recent_failed_commands(cwd, max_entries=200, lookback=20):
    failed = set()
    target = (cwd or "").rstrip("/")
    for event in session_events(max_entries=max_entries)[-lookback:]:
        if event.get("status", 0) == 0:
            continue
        event_cwd = (event.get("cwd") or "").rstrip("/")
        if target and event_cwd and event_cwd != target:
            continue
        cmd = event.get("command")
        if cmd:
            failed.add(cmd)
    return failed


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


def combined_history_commands(history_cfg):
    max_entries = int(history_cfg.get("max_entries", 8000) or 8000)
    max_bytes = int(history_cfg.get("max_bytes", 2097152) or 2097152)
    commands = history_commands(max_entries=max_entries, max_bytes=max_bytes)
    if history_cfg.get("session_enabled", True):
        commands.extend(session_commands(max_entries=int(history_cfg.get("session_max_entries", 200) or 200)))
    return commands


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
    commands = combined_history_commands({**history_cfg, "max_entries": max_entries, "max_bytes": max_bytes})
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


def best_history_suggestion(line, history_cfg, exclude=None, cwd=None):
    min_chars = int(history_cfg.get("direct_match_min_chars", 3) or 3)
    exclusions = set()
    if isinstance(exclude, str):
        if exclude:
            exclusions.add(exclude)
    elif exclude:
        exclusions.update(item for item in exclude if item)
    if exclusions:
        min_chars = min(min_chars, 2)
    if len(line.strip()) < min_chars:
        return ""
    matches = ranked_history_matches(line, history_cfg, limit=8, prefix_only=True)
    for match in matches:
        command = match["command"]
        if command in exclusions:
            continue
        if not suggestion_valid_for_cwd(command, cwd):
            continue
        if command.startswith(line) and command != line and not contains_secret(command):
            return command
    return ""


def recent_history(limit=8, history_cfg=None):
    if history_cfg:
        commands = combined_history_commands(
            {
                **history_cfg,
                "max_entries": min(int(history_cfg.get("max_entries", 8000) or 8000), 200),
                "max_bytes": min(int(history_cfg.get("max_bytes", 2097152) or 2097152), 262144),
            }
        )
        return commands[-limit:]
    commands = history_commands(max_entries=200, max_bytes=262144)
    return commands[-limit:]


def shell_quote(path):
    return shlex.quote(path)


def cd_path_suggestion(line, cwd):
    if not line or contains_secret(line):
        return ""
    match = re.match(r"^(cd|pushd)\s+([^;&|<>`$()]*)$", line)
    if not match:
        return ""

    command = match.group(1)
    raw_arg = match.group(2)
    if not raw_arg or raw_arg.startswith("-") or raw_arg.endswith(" "):
        return ""
    if any(ch in raw_arg for ch in "\"'\\"):
        return ""

    dir_part, name_prefix = posixpath.split(raw_arg)
    parent_arg = dir_part or "."
    try:
        parent = _resolve_candidate_path(parent_arg, cwd)
    except OSError:
        return ""
    if not parent.is_dir():
        return ""

    try:
        names = []
        for entry in parent.iterdir():
            if not entry.name.startswith(name_prefix):
                continue
            if not name_prefix.startswith(".") and entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    names.append(entry.name)
            except OSError:
                continue
    except OSError:
        return ""

    if len(names) != 1:
        return ""
    suffix = _escape_completion_suffix(names[0][len(name_prefix):])
    if suffix is None:
        return ""
    # Extend the typed line verbatim: zsh-autosuggestions only renders
    # suggestions that prefix-match the buffer, so quoting the whole path
    # ("cd 'demo app'", "cd '~/Projects'") silently drops the ghost text.
    suggestion = line + suffix
    if suggestion == line or contains_secret(suggestion):
        return ""
    return suggestion


def _escape_completion_suffix(text):
    if "\n" in text or "\r" in text:
        return None
    return re.sub(r"([^A-Za-z0-9_./+,^@%=:-])", r"\\\1", text)


def _resolve_candidate_path(arg, cwd):
    candidate = _expanduser_safe(arg)
    if not candidate.is_absolute():
        candidate = pathlib.Path(cwd or ".").expanduser() / candidate
    return candidate


def simple_command_parts(command):
    if re.search(r"[;&|<>`$()]", command):
        return []
    try:
        parts = shlex.split(command, posix=True)
    except Exception:
        return []
    while parts and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", parts[0]):
        parts.pop(0)
    while parts and parts[0] in ("command", "builtin", "noglob"):
        parts.pop(0)
    return parts


def suggestion_valid_for_cwd(command, cwd=None):
    if not cwd:
        return True
    if not command or contains_secret(command):
        return False
    parts = simple_command_parts(command)
    if not parts:
        return True

    head = parts[0]
    if head in {"cd", "pushd"}:
        if len(parts) == 1:
            return True
        if len(parts) > 2:
            return False
        arg = parts[1]
        if not _looks_like_path_arg(arg):
            return True
        try:
            return _resolve_candidate_path(arg, cwd).is_dir()
        except OSError:
            return False

    if head not in {"mv", "cp", "ln", "install"}:
        return True

    args = []
    option_mode = True
    for arg in parts[1:]:
        if option_mode and arg == "--":
            option_mode = False
            continue
        if option_mode and arg.startswith("-"):
            continue
        args.append(arg)
    if len(args) < 2:
        return True

    for arg in args[:-1]:
        if not _looks_like_path_arg(arg):
            continue
        try:
            if not _resolve_candidate_path(arg, cwd).exists():
                return False
        except OSError:
            return False
    return True


def mkdir_paths(command):
    parts = simple_command_parts(command)
    if not parts or parts[0] != "mkdir":
        return []

    paths = []
    option_mode = True
    for arg in parts[1:]:
        if option_mode and arg == "--":
            option_mode = False
            continue
        if option_mode and arg.startswith("-"):
            continue
        if arg:
            paths.append(arg)
    return paths


def cd_suggestion_for_mkdir(command, event_cwd, current_cwd):
    paths = mkdir_paths(command)
    if not paths:
        return ""
    for created in reversed(paths):
        if contains_secret(created):
            continue
        created_path = _expanduser_safe(created)
        if not created_path.is_absolute():
            base = pathlib.Path(event_cwd or current_cwd).expanduser()
            created_path = base / created_path
        try:
            resolved = created_path.resolve()
        except Exception:
            resolved = created_path
        if not resolved.is_dir():
            continue
        current = pathlib.Path(current_cwd).expanduser()
        try:
            same_cwd = current.resolve() == pathlib.Path(event_cwd or current_cwd).expanduser().resolve()
        except Exception:
            same_cwd = str(current) == str(event_cwd or current_cwd)
        target = created if same_cwd and not _expanduser_safe(created).is_absolute() else str(resolved)
        return f"cd {shell_quote(target)}"
    return ""


def mv_target_path(command, event_cwd, current_cwd):
    parts = simple_command_parts(command)
    if not parts or parts[0] != "mv":
        return ""

    args = []
    option_mode = True
    for arg in parts[1:]:
        if option_mode and arg == "--":
            option_mode = False
            continue
        if option_mode and arg.startswith("-"):
            continue
        args.append(arg)
    if len(args) < 2:
        return ""

    dest_arg = args[-1]
    src_args = args[:-1]
    if contains_secret(dest_arg) or any(contains_secret(src) for src in src_args):
        return ""

    base = pathlib.Path(event_cwd or current_cwd).expanduser()
    dest_path = _expanduser_safe(dest_arg)
    if not dest_path.is_absolute():
        dest_path = base / dest_path

    target_path = dest_path
    target_arg = dest_arg
    if len(src_args) == 1:
        moved_name = pathlib.Path(src_args[0]).name
        nested = dest_path / moved_name
        if nested.is_dir():
            target_path = nested
            target_arg = posixpath.join(dest_arg, moved_name)

    if not target_path.is_dir():
        return ""

    current = pathlib.Path(current_cwd).expanduser()
    try:
        same_cwd = current.resolve() == base.resolve()
    except Exception:
        same_cwd = str(current) == str(base)
    if same_cwd and not pathlib.Path(dest_arg).expanduser().is_absolute():
        return target_arg
    try:
        return str(target_path.resolve())
    except Exception:
        return str(target_path)


def cd_suggestion_for_moved_dir(command, event_cwd, current_cwd):
    target = mv_target_path(command, event_cwd, current_cwd)
    if not target:
        return ""
    return f"cd {shell_quote(target)}"


def event_success_status(event):
    try:
        return int(event.get("status", event.get("exit", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def event_succeeded(event):
    if event_success_status(event) != 0:
        return False
    if event.get("tty"):
        populated = _populate_output(dict(event))
        if output_looks_failed(populated.get("output") or ""):
            return False
    return True


def followup_suggestion_for_event(event, cwd):
    command = event.get("command") or event.get("cmd") or ""
    event_cwd = event.get("cwd") or cwd
    return cd_suggestion_for_mkdir(command, event_cwd, cwd) or cd_suggestion_for_moved_dir(command, event_cwd, cwd)


def inferred_next_command(line, cwd, history_cfg):
    if not history_cfg.get("enabled", True):
        return ""
    candidates = []
    candidates.extend(reversed(read_recent_terminal_events(max_records=10)))
    if history_cfg.get("session_enabled", True):
        candidates.extend(reversed(session_events(max_entries=int(history_cfg.get("session_max_entries", 200) or 200))))
    for command in reversed(history_commands(max_entries=25, max_bytes=65536)):
        candidates.append({"status": 0, "cwd": cwd, "command": command})

    for event in candidates:
        if not event_succeeded(event):
            return ""
        suggestion = followup_suggestion_for_event(event, cwd)
        if not suggestion:
            return ""
        if suggestion.startswith(line) and suggestion != line and not contains_secret(suggestion):
            return suggestion
        return ""
    return ""


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


def build_prompt(line, cwd, history_cfg=None, last_event=None, failed_commands=None):
    command = head_command(line) or ""
    history_cfg = history_cfg or {}
    history = "\n".join(f"- {item}" for item in recent_history(history_cfg=history_cfg))
    weighted_history = ranked_history_matches(line, history_cfg, prefix_only=True)
    if not weighted_history:
        weighted_history = ranked_history_matches(line, history_cfg, prefix_only=False)
    weighted_history = [item for item in weighted_history if suggestion_valid_for_cwd(item.get("command", ""), cwd)]
    weighted_history_text = "\n".join(
        f"- score={item['score']:.1f} count={item['count']}: {item['command']}" for item in weighted_history
    )
    help_text = cached_help(command) if command else ""

    failed_block = "(none)"
    if failed_commands:
        failed_block = "\n".join(f"- {cmd}" for cmd in list(failed_commands)[:10])

    last_command_block = "(none)"
    if last_event:
        tail = last_event.get("output") or ""
        if tail:
            tail_lines = tail.splitlines()
            if len(tail_lines) > 40:
                tail_lines = tail_lines[-40:]
            tail = "\n".join(tail_lines)
            if len(tail) > OUTPUT_TAIL_FOR_PROMPT:
                tail = tail[-OUTPUT_TAIL_FOR_PROMPT:]
        last_command_block = (
            f"cmd: {last_event.get('cmd','')}\n"
            f"exit: {last_event.get('exit', 0)}\n"
            f"cwd: {last_event.get('cwd','')}\n"
            f"output_tail:\n{tail or '(empty)'}"
        )

    system = (
        "You are terminal inline autocomplete. Complete the current terminal command line like "
        "Warp autocomplete. Return only the suffix that should be inserted at the cursor. "
        "Do not repeat the existing prefix. Do not include explanations, markdown, quotes, "
        "or trailing newline. Prefer highly weighted history matches, then exact flags/subcommands "
        "from provided help. When the previous command failed and the current line looks like the "
        "user retrying it, return a completion that yields the corrected command instead of "
        "extending the typo. If no useful completion is likely, return an empty string."
    )
    user = f"""cwd: {cwd}
shell: zsh
line_before_cursor: {redact(line)}
line_after_cursor:
head_command: {command}

last_command:
{last_command_block}

recently_failed_commands_do_not_suggest:
{failed_block}

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
- If last_command failed and the user is retyping it, infer the intended command from output_tail and complete to that, not to the typo.
- Never propose a completion that exactly matches anything in recently_failed_commands_do_not_suggest.
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
            "HTTP-Referer": "https://github.com/infinity-dump/termTab",
            "X-OpenRouter-Title": "termTab inline AI",
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
    elif token_prefix and re.match(r"^(?:~|/|\./|\.\./)", text):
        return ""
    if len(text) > 160:
        return ""
    return line + text


def channel_text(value):
    return (value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def emit_result(value, kind="suggest", output_format="plain"):
    if not value:
        return
    if output_format == "zsh":
        sys.stdout.write(f"{kind}\t{channel_text(value)}")
    else:
        sys.stdout.write(value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--line", required=True)
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--mode", choices=("suggest", "replace"), default="suggest")
    parser.add_argument("--format", choices=("plain", "zsh"), default="plain")
    args = parser.parse_args()

    line = args.line
    if len(line) > 240 or contains_secret(line):
        return 0

    config = load_config()
    if not config.get("inline", {}).get("enabled", True):
        return 0

    history_cfg = config.get("history", {})
    retry_event = find_retry_context(line, args.cwd)

    correction = correction_suggestion(line, args.cwd, retry_event, require_prefix=False)
    if args.mode == "replace":
        emit_result(correction, "replace", args.format)
        return 0

    if correction:
        if correction.startswith(line):
            emit_result(correction, "suggest", args.format)
            return 0
        if args.format == "zsh":
            emit_result(correction, "replace", args.format)
            return 0

    failed_retry = retry_event is not None
    exclude_history = set()
    if failed_retry:
        exclude_history.update(
            recent_failed_commands(
                args.cwd,
                max_entries=int(history_cfg.get("session_max_entries", 200) or 200),
            )
        )
        for event in read_recent_terminal_events(max_records=30):
            if event.get("exit", 0) != 0 and event.get("cmd"):
                exclude_history.add(event["cmd"])
        retry_cmd = retry_event.get("cmd")
        if retry_cmd:
            exclude_history.add(retry_cmd)

    path_suggestion = cd_path_suggestion(line, args.cwd)
    if path_suggestion:
        emit_result(path_suggestion, "suggest", args.format)
        return 0

    next_command = inferred_next_command(line, args.cwd, history_cfg)
    if next_command and next_command not in exclude_history:
        emit_result(next_command, "suggest", args.format)
        return 0

    if len(line.strip()) < 2 and not failed_retry:
        return 0

    history_suggestion = best_history_suggestion(line, history_cfg, exclude=exclude_history or None, cwd=args.cwd)
    if history_suggestion:
        emit_result(history_suggestion, "suggest", args.format)
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

    prompt_event = retry_event
    if not prompt_event:
        last_event = last_terminal_event()
        if last_event and last_event.get("exit", 0) != 0:
            prompt_event = last_event
    system, user = build_prompt(
        line,
        args.cwd,
        history_cfg=history_cfg,
        last_event=prompt_event,
        failed_commands=exclude_history or None,
    )

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
    if suggestion and suggestion in exclude_history:
        return 0
    if suggestion and suggestion.startswith(line) and suggestion_valid_for_cwd(suggestion, args.cwd):
        emit_result(suggestion, "suggest", args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
