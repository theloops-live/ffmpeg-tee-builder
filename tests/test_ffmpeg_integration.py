"""Run the built arguments through a real ffmpeg against local RTMP servers.

Each "platform" is ``ffmpeg -listen 1`` writing what it receives to a file.
Skipped when there is no ffmpeg on PATH. Takes about half a minute.
"""

import shutil
import socket
import subprocess
import threading
import time

import pytest

from ffmpeg_tee_builder import build_tee_spec

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not on PATH"),
]

SECONDS = 4


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Receiver:
    """A local RTMP server that records what it is sent."""

    def __init__(self, tmp_path, name):
        self.port = free_port()
        self.url = f"rtmp://127.0.0.1:{self.port}/live/{name}"
        self.out = tmp_path / f"{name}.flv"
        self.proc = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-listen", "1",
             "-i", self.url, "-c", "copy", "-f", "flv", str(self.out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    break
            time.sleep(0.05)
        # That probe connection ended the listener's first accept on some
        # builds; give it a moment and restart it if it has gone.
        time.sleep(0.2)
        if self.proc.poll() is not None:
            self.proc = subprocess.Popen(self.proc.args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)

    def received_seconds(self, timeout=10):
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        if not self.out.exists() or self.out.stat().st_size == 0:
            return 0.0
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "packet=pts_time", "-select_streams", "v",
             "-of", "csv=p=0", str(self.out)],
            capture_output=True, text=True,
        )
        times = [float(x) for x in r.stdout.split() if x.strip()]
        return max(times) - min(times) if times else 0.0

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


class Staller:
    """A TCP server that accepts a connection and never reads or answers it -
    a platform that holds the socket open and has stopped listening."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.url = f"rtmp://127.0.0.1:{self.port}/live/stalled"
        self.conns = []
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                c, _ = self.sock.accept()
                self.conns.append(c)
            except OSError:
                pass

    def stop(self):
        self._stop = True
        for c in self.conns:
            c.close()
        self.sock.close()


def send(spec, timeout):
    """Encode SECONDS of test pattern once and push it through tee."""
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={SECONDS}",
        "-f", "lavfi", "-i", f"sine=duration={SECONDS}",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-c:a", "aac",
        "-f", "tee", spec,
    ]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


@pytest.fixture
def receivers(tmp_path):
    made = []

    def make(name):
        r = Receiver(tmp_path, name)
        made.append(r)
        return r

    yield make
    for r in made:
        r.stop()


def test_both_destinations_receive_the_stream(receivers):
    a, b = receivers("a"), receivers("b")
    result = send(build_tee_spec([a.url, b.url]), timeout=30)
    assert result is not None and result.returncode == 0, result and result.stderr
    assert a.received_seconds() >= SECONDS - 1
    assert b.received_seconds() >= SECONDS - 1


def test_a_refused_destination_does_not_stop_the_other(receivers):
    a = receivers("a")
    refused = f"rtmp://127.0.0.1:{free_port()}/live/refused"  # nothing listens here
    result = send(build_tee_spec([refused, a.url]), timeout=30)
    assert result is not None
    assert a.received_seconds() >= SECONDS - 1


def test_a_stalled_destination_does_not_stop_the_other(receivers):
    a = receivers("a")
    staller = Staller()
    try:
        # The sender may not exit on its own: the stalled destination's queue
        # waits on a server that never answers. What matters is the healthy one.
        send(build_tee_spec([staller.url, a.url]), timeout=SECONDS + 8)
        assert a.received_seconds(timeout=3) >= SECONDS - 1
    finally:
        staller.stop()


def test_without_the_queue_a_stalled_destination_stops_everything(receivers):
    """The failure this tool exists to prevent, reproduced."""
    a = receivers("a")
    staller = Staller()
    try:
        send(build_tee_spec([staller.url, a.url], fifo=False), timeout=SECONDS + 4)
        assert a.received_seconds(timeout=3) < 1
    finally:
        staller.stop()


def test_a_single_backslash_takes_every_output_down(receivers):
    """Why the backslash is doubled: with one, drop_pkts_on_overflow reaches
    the flv muxer, which rejects it."""
    a, b = receivers("a"), receivers("b")
    spec = build_tee_spec([a.url, b.url]).replace("\\\\:", "\\:")
    result = send(spec, timeout=30)
    assert result is not None and result.returncode != 0
    assert "drop_pkts_on_overflow" in result.stderr
    assert a.received_seconds(timeout=3) < 1
    assert b.received_seconds(timeout=3) < 1


def test_with_a_timeout_the_stalled_destination_is_dropped_and_ffmpeg_exits(receivers):
    a = receivers("a")
    staller = Staller()
    try:
        started = time.time()
        result = send(build_tee_spec([staller.url, a.url], timeout=2), timeout=SECONDS + 10)
        assert result is not None, "ffmpeg did not exit"
        assert result.returncode == 0, result.stderr
        assert time.time() - started < SECONDS + 5
        assert "Slave muxer #0 failed" in result.stderr or "timed out" in result.stderr
        assert a.received_seconds(timeout=3) >= SECONDS - 1
    finally:
        staller.stop()
