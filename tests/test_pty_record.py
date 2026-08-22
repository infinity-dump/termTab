import fcntl
import os
import pathlib
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import textwrap
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RECORDER = ROOT / "src" / "pty_record.py"

# Child that reports its own pty size at startup and again on every SIGWINCH,
# then waits for "q" on stdin before exiting.
SIZE_REPORTER = textwrap.dedent(
    """
    import fcntl, signal, struct, sys, termios

    def size():
        ws = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\\0" * 8)
        rows, cols = struct.unpack("HHHH", ws)[:2]
        return f"{rows}x{cols}"

    print("INIT", size(), flush=True)
    signal.signal(signal.SIGWINCH, lambda *_: print("WINCH", size(), flush=True))
    for line in sys.stdin:
        if line.strip() == "q":
            break
    print("BYE", flush=True)
    """
)


def set_winsize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class PtyRecordTests(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = os.openpty()
        set_winsize(self.slave, 40, 120)
        self.record = tempfile.NamedTemporaryFile(delete=False)
        self.record.close()
        self.addCleanup(os.unlink, self.record.name)
        self.proc = None

    def tearDown(self):
        for fd in (self.master,):
            try:
                os.close(fd)
            except OSError:
                pass
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def spawn(self, child_args, record_path=None, env=None):
        self.proc = subprocess.Popen(
            [sys.executable, str(RECORDER), record_path or self.record.name, "--", *child_args],
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            start_new_session=True,
            env={**os.environ, **env} if env else None,
        )
        os.close(self.slave)
        return self.proc

    def read_until(self, needle, timeout=10.0):
        deadline = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([self.master], [], [], max(remaining, 0))
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if needle.encode() in buf:
                return buf.decode(errors="replace")
        self.fail(f"timed out waiting for {needle!r}; got {buf!r}")

    def test_forwards_winch_to_inner_pty(self):
        proc = self.spawn([sys.executable, "-u", "-c", SIZE_REPORTER])
        self.read_until("INIT 40x120")

        set_winsize(self.master, 30, 100)
        proc.send_signal(signal.SIGWINCH)
        self.read_until("WINCH 30x100")

        os.write(self.master, b"q\n")
        self.read_until("BYE")
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_child_starts_with_outer_winsize(self):
        set_winsize(self.slave, 25, 91)
        proc = self.spawn([sys.executable, "-u", "-c", SIZE_REPORTER])
        self.read_until("INIT 25x91")
        os.write(self.master, b"q\n")
        proc.wait(timeout=10)

    def test_records_output_and_propagates_exit_status(self):
        proc = self.spawn(
            [sys.executable, "-c", "print('hello-record', flush=True); raise SystemExit(3)"]
        )
        self.read_until("hello-record")
        self.assertEqual(proc.wait(timeout=10), 3)
        with open(self.record.name, "rb") as fh:
            self.assertIn(b"hello-record", fh.read())

    def test_missing_command_exits_127(self):
        proc = self.spawn(["/nonexistent/termtab-no-such-binary"])
        self.assertEqual(proc.wait(timeout=10), 127)

    def test_unwritable_record_path_degrades_to_no_capture(self):
        proc = self.spawn(
            [sys.executable, "-c", "print('degraded-ok', flush=True); raise SystemExit(5)"],
            record_path="/nonexistent-termtab-dir/rec.tty",
        )
        self.read_until("degraded-ok")
        self.assertEqual(proc.wait(timeout=10), 5)

    def test_record_cap_stops_recording_but_passes_output_through(self):
        payload = "x" * 200 + "END-MARKER"
        proc = self.spawn(
            [sys.executable, "-c", f"print({payload!r}, flush=True)"],
            env={"TERMTAB_RECORD_MAX_BYTES": "64"},
        )
        out = self.read_until("END-MARKER")
        self.assertIn(payload, out)
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertLessEqual(os.path.getsize(self.record.name), 64)


if __name__ == "__main__":
    unittest.main()
