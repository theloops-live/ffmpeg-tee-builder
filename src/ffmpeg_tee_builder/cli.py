"""Command line: ``ffmpeg-tee-builder [options] URL [URL ...]``.

Stream keys are secrets, and anything typed as an argument lands in the shell's
history. ``--file PATH`` (or ``--file -`` for standard input) reads one URL per
line instead; blank lines and lines starting with ``#`` are skipped.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from typing import List, Optional

from . import (
    DEFAULT_QUEUE_SIZE,
    DestinationError,
    __version__,
    build_command,
    build_tee_spec,
)


def _read_urls(path: str) -> List[str]:
    stream = sys.stdin if path == "-" else open(path, encoding="utf-8")
    try:
        lines = stream.read().splitlines()
    finally:
        if stream is not sys.stdin:
            stream.close()
    # Lines are stripped here, and only here: a file's trailing newline or
    # indentation is formatting, not part of the URL.
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ffmpeg-tee-builder",
        description=(
            "Build the -f tee argument that sends one FFmpeg stream to several "
            "RTMP/RTMPS destinations, with onfail=ignore and a dropping queue per "
            "destination so one dead or stalled server cannot stop the others."
        ),
    )
    p.add_argument("urls", nargs="*", metavar="URL", help="rtmp:// or rtmps:// destination")
    p.add_argument("-f", "--file", help="read URLs from a file, one per line ('-' for stdin)")
    p.add_argument(
        "-c", "--command", action="store_true",
        help="print a complete ffmpeg command instead of just the tee argument",
    )
    p.add_argument("-i", "--input", default="input.mp4", help="input for --command (default: input.mp4)")
    p.add_argument(
        "--copy", action="store_true",
        help="with --command: -c copy instead of an example libx264/aac encode",
    )
    p.add_argument(
        "--queue-size", type=int, default=DEFAULT_QUEUE_SIZE,
        help=f"packets queued per destination before dropping (default: {DEFAULT_QUEUE_SIZE})",
    )
    fifo = p.add_mutually_exclusive_group()
    fifo.add_argument("--fifo", dest="fifo", action="store_true", default=None,
                      help="always queue, even for one destination")
    fifo.add_argument("--no-fifo", dest="fifo", action="store_false",
                      help="never queue (not recommended for more than one destination)")
    p.add_argument(
        "--timeout", type=float, metavar="SECONDS",
        help="give up on a destination that stops answering for this long (sets rw_timeout); "
             "without it a stalled server can keep ffmpeg from exiting",
    )
    p.add_argument("--onfail", choices=("ignore", "abort"), default="ignore",
                   help="what one failed destination does to the rest (default: ignore)")
    p.add_argument("--keep-duplicates", action="store_true", help="do not drop repeated URLs")
    p.add_argument("--json", action="store_true", help="print the result as JSON (argv list for --command)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    urls = list(args.urls)
    if args.file:
        try:
            urls += _read_urls(args.file)
        except OSError as exc:
            print(f"ffmpeg-tee-builder: cannot read {args.file}: {exc.strerror}", file=sys.stderr)
            return 2
    if not urls:
        print("ffmpeg-tee-builder: give at least one URL, or --file", file=sys.stderr)
        return 2

    opts = dict(
        fifo=args.fifo,
        queue_size=args.queue_size,
        onfail=args.onfail,
        timeout=args.timeout,
        dedupe=not args.keep_duplicates,
    )
    try:
        if args.command:
            encode = ("-c", "copy") if args.copy else None
            extra = {"encode": encode} if encode else {}
            result = build_command(urls, input=args.input, **extra, **opts)
        else:
            result = build_tee_spec(urls, **opts)
    except (DestinationError, ValueError) as exc:
        print(f"ffmpeg-tee-builder: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result))
    elif args.command:
        print(shlex.join(result))
    else:
        # Quoted for a POSIX shell, because the doubled backslash and the '|'
        # do not survive being pasted unquoted.
        print(shlex.quote(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
