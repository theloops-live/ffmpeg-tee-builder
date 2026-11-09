"""Build FFmpeg ``-f tee`` arguments for pushing one stream to several RTMP/RTMPS servers.

The tee muxer takes every destination in a single argument::

    [f=flv:onfail=ignore:use_fifo=1:fifo_options=queue_size=600\\:drop_pkts_on_overflow=1]rtmp://a/app/key|[...]rtmps://b/app/key

Three things in that string are easy to get wrong, and each one fails quietly:

* ``onfail=ignore`` - the documented default is ``abort``: one destination that
  refuses the connection ends the whole process, and every other platform with it.
* ``use_fifo=1`` together with ``drop_pkts_on_overflow=1`` - tee writes to every
  destination from one thread, so a server that keeps the socket open and stops
  reading eventually blocks the write for all of them. A queue alone does not
  help; the queue has to be allowed to drop.
* the doubled backslash in ``queue_size=600\\:drop_pkts_on_overflow=1`` - tee
  unescapes the slave options once and the nested ``fifo_options`` dictionary
  once more. With a single backslash ``drop_pkts_on_overflow`` is handed to the
  flv muxer, which rejects it, and every output fails.

A URL is also checked before it reaches that string: ``|`` would add a
destination nobody asked for, ``[`` and ``]`` open and close option blocks, and
``\\`` is the escape character.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "DestinationError",
    "build_command",
    "build_output_args",
    "build_tee_spec",
    "mask_url",
    "slave_options",
    "validate_url",
]

__version__ = "1.0.0"

#: Packets each destination may queue before it starts dropping. 600 is roughly
#: ten seconds of a 1080p60 stream: long enough to ride out a hiccup, small
#: enough to afford one per destination.
DEFAULT_QUEUE_SIZE = 600

SCHEMES = ("rtmp", "rtmps")

# tee specification syntax. Whitespace and control characters are refused
# separately (see _bad_char).
_TEE_SYNTAX = frozenset("|[]\\")


class DestinationError(ValueError):
    """A destination URL that cannot be placed in a tee specification.

    The message never contains the stream key: ``masked`` is the URL with the
    key replaced, safe to log.
    """

    def __init__(self, reason: str, url: str, index: Optional[int] = None):
        self.reason = reason
        self.masked = mask_url(url)
        self.index = index
        where = f"destination {index + 1}: " if index is not None else ""
        super().__init__(f"{where}{reason} ({self.masked})")


def mask_url(url: str) -> str:
    """Return ``url`` with its stream key (everything after the application) and query hidden.

    ``rtmp://a.rtmp.youtube.com/live2/abcd-efgh`` -> ``rtmp://a.rtmp.youtube.com/live2/****``
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable URL>"
    if not parts.scheme or not parts.netloc:
        return "<not a URL>"
    netloc = parts.hostname or ""
    if parts.username is not None or parts.password is not None:
        netloc = "****@" + netloc
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None:
        netloc += f":{port}"
    # Keep the application (the first path segment) and hide everything after
    # it. Not just the last segment: in a rejected URL the key can sit in the
    # middle, before a '|' that started a second address.
    segments = parts.path.split("/")
    if len(segments) > 2:
        segments = segments[:2] + ["****"]
    masked = f"{parts.scheme}://{netloc}{'/'.join(segments)}"
    if parts.query:
        masked += "?****"
    return masked


def _bad_char(url: str) -> Optional[str]:
    for ch in url:
        if ch in _TEE_SYNTAX:
            return repr(ch)
        if ch.isspace() or ord(ch) < 32 or ord(ch) == 127:
            return "whitespace or a control character"
    return None


def validate_url(url: str, schemes: Sequence[str] = SCHEMES) -> str:
    """Return ``url`` unchanged if it is safe to put in a tee specification.

    Raises :class:`DestinationError` otherwise. Nothing is stripped or
    rewritten: a URL with stray whitespace was pasted wrong, and silently
    fixing it would hide that.
    """
    if not isinstance(url, str) or not url:
        raise DestinationError("empty destination", str(url or ""))
    bad = _bad_char(url)
    if bad:
        raise DestinationError(f"contains {bad}, which the tee muxer would misread", url)
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise DestinationError("not a valid URL", url) from None
    if parts.scheme.lower() not in schemes:
        raise DestinationError(
            f"scheme must be one of {', '.join(s + '://' for s in schemes)}", url
        )
    if not parts.hostname:
        raise DestinationError("missing host", url)
    if not parts.path.strip("/"):
        raise DestinationError("missing application and stream key", url)
    return url


def slave_options(
    *,
    fifo: bool,
    queue_size: int = DEFAULT_QUEUE_SIZE,
    onfail: str = "ignore",
    no_duration_filesize: bool = True,
    timeout: Optional[float] = None,
) -> List[str]:
    """The per-destination options, in order, unescaped for the outer level.

    ``no_duration_filesize`` keeps the flv muxer from writing ``duration: 0``
    and ``filesize: 0`` into the stream's metadata and from trying to seek back
    and rewrite them, which a live connection cannot do.

    ``timeout`` (seconds) sets ``rw_timeout`` on each destination's connection.
    The queue keeps a stalled server from holding up the others, but not from
    holding up FFmpeg's exit: without a timeout, a server that accepts the
    connection and never answers keeps its queue's thread waiting for ever.
    With one, that destination fails after the timeout and tee carries on.
    """
    if onfail not in ("ignore", "abort"):
        raise ValueError("onfail must be 'ignore' or 'abort'")
    if isinstance(queue_size, bool) or not isinstance(queue_size, int) or queue_size < 1:
        raise ValueError("queue_size must be a positive integer")
    opts = ["f=flv"]
    if no_duration_filesize:
        opts.append("flvflags=+no_duration_filesize")
    opts.append(f"onfail={onfail}")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0:
            raise ValueError("timeout must be a positive number of seconds")
        opts.append(f"rw_timeout={int(round(timeout * 1_000_000))}")
    if fifo:
        opts.append("use_fifo=1")
        # The doubled backslash is the point: see the module docstring.
        opts.append(f"fifo_options=queue_size={queue_size}\\\\:drop_pkts_on_overflow=1")
    return opts


@dataclass(frozen=True)
class _Plan:
    urls: List[str]
    fifo: bool


def _plan(urls: Iterable[str], fifo: Optional[bool], dedupe: bool, schemes: Sequence[str]) -> _Plan:
    if isinstance(urls, str):
        raise TypeError("pass a list of URLs, not a single string")
    checked: List[str] = []
    seen = set()
    for i, url in enumerate(urls):
        try:
            validate_url(url, schemes)
        except DestinationError as exc:
            raise DestinationError(exc.reason, url, index=i) from None
        if dedupe:
            if url in seen:
                continue
            seen.add(url)
        checked.append(url)
    if not checked:
        raise ValueError("no destinations")
    return _Plan(checked, fifo=(len(checked) > 1) if fifo is None else fifo)


def build_tee_spec(
    urls: Iterable[str],
    *,
    fifo: Optional[bool] = None,
    queue_size: int = DEFAULT_QUEUE_SIZE,
    onfail: str = "ignore",
    no_duration_filesize: bool = True,
    timeout: Optional[float] = None,
    dedupe: bool = True,
    schemes: Sequence[str] = SCHEMES,
) -> str:
    """Return the argument that follows ``-f tee``.

    ``fifo=None`` (the default) puts a dropping queue in front of each
    destination when there is more than one; with a single destination there is
    nobody to protect and a stall is better surfaced than papered over.
    Exact duplicate URLs are dropped unless ``dedupe=False``: a platform
    refuses the second publish on a key anyway.
    """
    plan = _plan(urls, fifo, dedupe, schemes)
    opts = ":".join(
        slave_options(
            fifo=plan.fifo,
            queue_size=queue_size,
            onfail=onfail,
            no_duration_filesize=no_duration_filesize,
            timeout=timeout,
        )
    )
    return "|".join(f"[{opts}]{url}" for url in plan.urls)


def build_output_args(urls: Iterable[str], **kwargs) -> List[str]:
    """The output half of an FFmpeg command: format and target.

    One destination gets a plain flv output - tee adds nothing there. Several
    get ``-f tee`` with :func:`build_tee_spec`. Keyword arguments are passed on
    to :func:`build_tee_spec`.
    """
    dedupe = kwargs.get("dedupe", True)
    schemes = kwargs.get("schemes", SCHEMES)
    plan = _plan(urls, kwargs.get("fifo"), dedupe, schemes)
    if len(plan.urls) == 1 and not plan.fifo:
        # Same checks on the options as the tee path, so a bad value is
        # refused whichever shape the output takes.
        opts = slave_options(
            fifo=False,
            queue_size=kwargs.get("queue_size", DEFAULT_QUEUE_SIZE),
            onfail=kwargs.get("onfail", "ignore"),
            no_duration_filesize=kwargs.get("no_duration_filesize", True),
            timeout=kwargs.get("timeout"),
        )
        args = ["-f", "flv"]
        for opt in opts:
            key, _, value = opt.partition("=")
            if key in ("flvflags", "rw_timeout"):
                args += [f"-{key}", value]
        return args + [plan.urls[0]]
    return ["-f", "tee", build_tee_spec(plan.urls, **kwargs)]


#: An example encode: once, for every destination. Tune to taste.
DEFAULT_ENCODE = (
    "-c:v", "libx264", "-preset", "veryfast",
    "-b:v", "4500k", "-maxrate", "4500k", "-bufsize", "9000k",
    "-g", "60", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
)


def build_command(
    urls: Iterable[str],
    *,
    input: str = "input.mp4",
    ffmpeg: str = "ffmpeg",
    input_args: Sequence[str] = ("-re",),
    encode: Sequence[str] = DEFAULT_ENCODE,
    maps: Sequence[str] = ("0:v:0", "0:a:0"),
    **kwargs,
) -> List[str]:
    """A complete FFmpeg argv, ready for :func:`subprocess.run` (no shell needed).

    The tee muxer selects no streams by itself, so ``-map`` is always written.
    """
    cmd = [ffmpeg, "-hide_banner", *input_args, "-i", input]
    for m in maps:
        cmd += ["-map", m]
    cmd += list(encode)
    cmd += build_output_args(urls, **kwargs)
    return cmd
