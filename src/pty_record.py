#!/usr/bin/env python3
"""script(1)-style terminal output recorder that keeps TUIs intact.

macOS BSD script(1) sizes its inner pty once at startup and never forwards
window resizes, so after any resize a fullscreen TUI (claude, vim, htop)
paints against stale rows/cols and the terminal mangles the output. This
recorder mirrors stdin <-> pty traffic, tees child output to the record
file (flushed per write, like `script -q -F -t 0`), and re-syncs the inner
pty winsize from stdin on startup and on every SIGWINCH.

Usage: pty_record.py <record-file> -- <command> [args...]
Exits with the child's exit status (128+N if the child died on signal N).
"""

import fcntl
import os
import select
import signal
import sys
import termios
import tty

READ_SIZE = 65536


def get_winsize(fd):
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
    except OSError:
        return None


def set_winsize(fd, ws):
    if ws is None:
        return
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, ws)
    except OSError:
        pass


def write_all(fd, data):
    while data:
        try:
            n = os.write(fd, data)
        except OSError:
            return
        data = data[n:]


def main(argv):
    if len(argv) < 4 or argv[2] != "--":
        print("usage: pty_record.py <record-file> -- <command> [args...]", file=sys.stderr)
        return 2
    record_path, cmd = argv[1], argv[3:]

    # Open the record file before forking: the shell already exec'd into us,
    # so an unwritable cache must degrade to "no capture", not kill the tab.
    try:
        record = open(record_path, "ab", buffering=0)
    except OSError:
        try:
            os.execvp(cmd[0], cmd)
        except OSError as exc:
            print(f"pty_record: {cmd[0]}: {exc}", file=sys.stderr)
            return 127

    try:
        record_max = int(os.environ.get("TERMTAB_RECORD_MAX_BYTES", 256 * 1024 * 1024))
    except ValueError:
        record_max = 256 * 1024 * 1024

    stdin_fd = 0
    master, slave = os.openpty()
    # Size the inner pty before forking so the child never sees a stale size.
    set_winsize(slave, get_winsize(stdin_fd))

    pid = os.fork()
    if pid == 0:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        os.close(master)
        if slave > 2:
            os.close(slave)
        try:
            os.execvp(cmd[0], cmd)
        except OSError as exc:
            print(f"pty_record: {cmd[0]}: {exc}", file=sys.stderr)
        os._exit(127)

    os.close(slave)

    def sync_winsize(*_):
        set_winsize(master, get_winsize(stdin_fd))

    signal.signal(signal.SIGWINCH, sync_winsize)

    old_attrs = None
    try:
        old_attrs = termios.tcgetattr(stdin_fd)
    except termios.error:
        pass

    recorded = record.tell()

    def record_write(data):
        # Recording must never take the shell down: stop on write errors
        # (e.g. ENOSPC) and cap runaway transcripts, but keep passing
        # output through either way.
        nonlocal record, recorded
        if record is None:
            return
        if recorded + len(data) > record_max:
            try:
                record.close()
            except OSError:
                pass
            record = None
            return
        try:
            record.write(data)
            recorded += len(data)
        except OSError:
            record = None

    stdin_open = True
    try:
        if old_attrs is not None:
            tty.setraw(stdin_fd)
        while True:
            rfds = [master] + ([stdin_fd] if stdin_open else [])
            ready, _, _ = select.select(rfds, [], [])
            if master in ready:
                try:
                    data = os.read(master, READ_SIZE)
                except OSError:  # EIO: child exited and slave side closed
                    data = b""
                if not data:
                    break
                write_all(1, data)
                record_write(data)
            if stdin_fd in ready:
                try:
                    data = os.read(stdin_fd, READ_SIZE)
                except OSError:
                    data = b""
                if not data:
                    # Outer terminal went away; keep draining child output.
                    stdin_open = False
                    continue
                write_all(master, data)
    finally:
        if record is not None:
            try:
                record.close()
            except OSError:
                pass
        if old_attrs is not None:
            # TCSANOW: never block on exit waiting for the outer tty to drain.
            termios.tcsetattr(stdin_fd, termios.TCSANOW, old_attrs)
        try:
            os.close(master)
        except OSError:
            pass

    _, wait_status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(wait_status)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
