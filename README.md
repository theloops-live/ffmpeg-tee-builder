# ffmpeg-tee-builder

Build the `-f tee` argument that sends one FFmpeg stream to several RTMP/RTMPS
servers, so that **one dead or stalled destination cannot stop the others**.

A small Python library and command-line tool with no dependencies. There is
also an in-browser version at
**[theloops.live/tools/ffmpeg-tee-builder](https://theloops.live/tools/ffmpeg-tee-builder)**.

```console
$ ffmpeg-tee-builder rtmp://a.rtmp.youtube.com/live2/YOUR-KEY rtmp://live.twitch.tv/app/YOUR-KEY
'[f=flv:flvflags=+no_duration_filesize:onfail=ignore:use_fifo=1:fifo_options=queue_size=600\\:drop_pkts_on_overflow=1]rtmp://a.rtmp.youtube.com/live2/YOUR-KEY|[f=flv:flvflags=+no_duration_filesize:onfail=ignore:use_fifo=1:fifo_options=queue_size=600\\:drop_pkts_on_overflow=1]rtmp://live.twitch.tv/app/YOUR-KEY'
```

## Why a builder for one string

FFmpeg's [tee muxer](https://ffmpeg.org/ffmpeg-formats.html#tee-1) encodes once
and writes the packets to every destination. Its whole configuration is one
argument, and three parts of it fail without saying so:

| Option | What goes wrong without it |
|---|---|
| `onfail=ignore` | The documented default is `abort`. One destination that refuses the connection (a reset key, an ended event) ends the process, and every other platform goes off air with it. |
| `use_fifo=1` + `drop_pkts_on_overflow=1` | tee writes to every destination from one thread. A server that keeps the socket open and stops reading eventually blocks that write for **all** of them, with no error and every destination still "connected". A queue alone does not help; it has to be allowed to drop. |
| `\\:` (doubled backslash) | tee unescapes the slave options once and the nested `fifo_options` once more. With a single backslash, `drop_pkts_on_overflow` is handed to the flv muxer, which rejects it (`Unknown option 'drop_pkts_on_overflow'`), and every output fails. |

The URLs are checked too. Inside a tee spec, `|` separates destinations, `[` and
`]` delimit options and `\` escapes, so a stream key containing any of them
silently changes the command, and a `|` adds a destination nobody asked for.
Those characters and any whitespace are refused, with an error that names the
destination but never prints the key.

The library also does three smaller things:

* With a single destination, `build_output_args` returns a plain
  `-f flv URL` output, because tee has nothing to add there.
* Exact duplicate URLs are dropped, since a platform refuses a second publish
  on the same key.
* `flvflags=+no_duration_filesize` stops the flv muxer from writing
  `duration: 0` and `filesize: 0` into a live stream's metadata.

The defaults are the ones [TheLoops](https://theloops.live) uses for its own
multi-destination 24/7 streams. The test suite checks every claim above against
a real FFmpeg.

## Install

Python 3.9 or newer.

```bash
pip install git+https://github.com/theloops-live/ffmpeg-tee-builder
```

## Command line

```bash
# just the tee argument, quoted for a POSIX shell
ffmpeg-tee-builder rtmp://a.rtmp.youtube.com/live2/KEY1 rtmps://live-api-s.facebook.com:443/rtmp/KEY2

# a whole ffmpeg command
ffmpeg-tee-builder --command -i show.mp4 --timeout 10 \
  rtmp://a.rtmp.youtube.com/live2/KEY1 rtmp://live.twitch.tv/app/KEY2
```

That prints:

```bash
ffmpeg -hide_banner -re -i show.mp4 -map 0:v:0 -map 0:a:0 -c:v libx264 -preset veryfast -b:v 4500k -maxrate 4500k -bufsize 9000k -g 60 -pix_fmt yuv420p -c:a aac -b:a 160k -ar 44100 -f tee '[f=flv:flvflags=+no_duration_filesize:onfail=ignore:rw_timeout=10000000:use_fifo=1:fifo_options=queue_size=600\\:drop_pkts_on_overflow=1]rtmp://a.rtmp.youtube.com/live2/KEY1|[f=flv:flvflags=+no_duration_filesize:onfail=ignore:rw_timeout=10000000:use_fifo=1:fifo_options=queue_size=600\\:drop_pkts_on_overflow=1]rtmp://live.twitch.tv/app/KEY2'
```

Stream keys are secrets, and arguments end up in your shell history. Put the
URLs in a file instead, one per line (`#` starts a comment), or pipe them in:

```bash
ffmpeg-tee-builder --command -i show.mp4 --file destinations.txt > stream.sh
sh stream.sh
```

| Option | |
|---|---|
| `-c`, `--command` | print a full ffmpeg command (`-i` sets the input; `--copy` uses `-c copy` instead of the example encode) |
| `-f`, `--file PATH` | read URLs from a file, `-` for stdin |
| `--timeout SECONDS` | sets `rw_timeout`: give up on a destination that stops answering (see below) |
| `--queue-size N` | packets each destination may queue before dropping (default 600, about ten seconds of 1080p60) |
| `--fifo` / `--no-fifo` | force the queue on for one destination, or off for several |
| `--onfail abort` | restore FFmpeg's documented default |
| `--keep-duplicates` | do not drop repeated URLs |
| `--json` | machine-readable output |

### `--timeout`: letting FFmpeg exit

The queue keeps a stalled server from holding up the *other destinations*, but
not from holding up FFmpeg's own exit: a server that accepts the connection and
never answers keeps its queue's thread waiting forever. With `--timeout 10`, that
destination fails after ten seconds of silence, tee logs
`Slave muxer #N failed … continuing with 1/2 slaves`, and the stream carries on.
It is off by default because a slow ingest on a bad day can take a few seconds
to answer, and the right number depends on yours.

### Pasting it somewhere else

The `\\:` must reach FFmpeg as two backslashes. The tool prints the argument
in single quotes, which keeps them. Some tools collapse them anyway:

* zsh's built-in `echo` turns `\\` into `\`. Write the output to a file, or use
  `printf '%s\n'`.
* In a JSON or YAML string, every backslash is doubled again: `"...600\\\\:drop..."`.
* From code, pass the argv as a list (`subprocess.run(cmd)`) and no shell is
  involved at all.

## Python

```python
import subprocess
from ffmpeg_tee_builder import build_command, build_tee_spec, DestinationError

urls = [
    "rtmp://a.rtmp.youtube.com/live2/KEY1",
    "rtmp://live.twitch.tv/app/KEY2",
    "rtmps://live-api-s.facebook.com:443/rtmp/KEY3",
]

spec = build_tee_spec(urls, timeout=10)                 # the string after -f tee
cmd = build_command(urls, input="show.mp4", timeout=10)  # a full argv list
subprocess.run(cmd, check=True)

try:
    build_tee_spec(["rtmp://host/app/key|rtmp://elsewhere/app/key"])
except DestinationError as e:
    print(e)          # destination 1: contains '|', which the tee muxer would misread (rtmp://host/app/****)
    print(e.masked)   # safe to log
```

| Function | Returns |
|---|---|
| `build_tee_spec(urls, *, fifo=None, queue_size=600, onfail="ignore", timeout=None, no_duration_filesize=True, dedupe=True)` | the tee argument. `fifo=None` means a queue when there is more than one destination |
| `build_output_args(urls, **same)` | `["-f", "tee", spec]`, or `["-f", "flv", …, url]` for one destination |
| `build_command(urls, *, input, encode=DEFAULT_ENCODE, maps=("0:v:0", "0:a:0"), input_args=("-re",), **same)` | a complete argv. tee selects no streams on its own, so `-map` is always written |
| `validate_url(url)` | the URL, or raises `DestinationError` |
| `mask_url(url)` | the URL with everything after the application name hidden |

Encode once, before the tee: the codec options in `build_command` apply to every
destination. Platforms with different bitrate limits need the lowest common
setting, or separate outputs.

## Tests

```bash
pip install pytest
python -m pytest
```

The unit tests need nothing else. `tests/test_ffmpeg_integration.py` needs
`ffmpeg` and `ffprobe` on `PATH` (it skips without them) and takes about forty
seconds. It starts local RTMP servers with `ffmpeg -listen 1` and checks:

* both destinations receive the whole stream;
* a destination that refuses the connection does not stop the other;
* a destination that accepts and never answers does not stop the other;
* **without** the queue, that same destination stops everything (the failure
  this tool exists to prevent, reproduced);
* with `timeout`, the stalled destination is dropped and FFmpeg exits 0;
* with a single backslash, every output fails.

## Licence

MIT. See [LICENSE](LICENSE).

The background, with the measurements:
[One stalled RTMP destination froze all the others](https://dev.to/aggosh/one-stalled-rtmp-destination-froze-all-the-others-ffmpeg-tee-usefifo-and-a-doubled-backslash-4ckf).
