import json
import shlex

import pytest

from ffmpeg_tee_builder import (
    DestinationError,
    build_command,
    build_output_args,
    build_tee_spec,
    mask_url,
    slave_options,
    validate_url,
)
from ffmpeg_tee_builder.cli import main

YT = "rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl-mnop"
TW = "rtmp://live.twitch.tv/app/live_123_secret"
FB = "rtmps://live-api-s.facebook.com:443/rtmp/FB-1-secret"

FIFO = r"use_fifo=1:fifo_options=queue_size=600\\:drop_pkts_on_overflow=1"


def test_two_destinations_exact_spec():
    spec = build_tee_spec([YT, TW])
    opts = f"f=flv:flvflags=+no_duration_filesize:onfail=ignore:{FIFO}"
    assert spec == f"[{opts}]{YT}|[{opts}]{TW}"


def test_backslash_is_doubled_in_the_string_ffmpeg_receives():
    spec = build_tee_spec([YT, TW])
    # Two backslash characters, then the colon - not one.
    assert "queue_size=600\\\\:drop_pkts_on_overflow=1" in spec
    assert "600\\:drop" not in spec.replace("600\\\\:drop", "")


def test_single_destination_gets_no_queue_by_default():
    assert build_tee_spec([YT]) == f"[f=flv:flvflags=+no_duration_filesize:onfail=ignore]{YT}"


def test_fifo_can_be_forced_either_way():
    assert "use_fifo=1" in build_tee_spec([YT], fifo=True)
    assert "use_fifo" not in build_tee_spec([YT, TW], fifo=False)


def test_queue_size_and_onfail():
    spec = build_tee_spec([YT, TW], queue_size=120, onfail="abort")
    assert "queue_size=120\\\\:drop_pkts_on_overflow=1" in spec
    assert "onfail=abort" in spec


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "600"])
def test_queue_size_must_be_positive_int(bad):
    with pytest.raises(ValueError):
        slave_options(fifo=True, queue_size=bad)


def test_onfail_values():
    with pytest.raises(ValueError):
        slave_options(fifo=False, onfail="retry")


def test_rtmps_with_port_is_accepted_unchanged():
    # The ':' in ':443' reaches tee unescaped, and that is fine: tee splits
    # slaves on '|' and options only inside '[...]'.
    assert build_tee_spec([FB, YT]).endswith(f"]{YT}")
    assert f"]{FB}|" in build_tee_spec([FB, YT])


def test_query_string_is_allowed():
    url = "rtmp://ingest.example.com/live/key?token=abc&x=1"
    assert validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "rtmp://a.example.com/live/key|rtmp://evil.example.com/live/k",
        "rtmp://a.example.com/live/[f=mpegts]key",
        "rtmp://a.example.com/live/key]",
        "rtmp://a.example.com/live/ke\\y",
        "rtmp://a.example.com/live/key ",
        " rtmp://a.example.com/live/key",
        "rtmp://a.example.com/live/k ey",
        "rtmp://a.example.com/live/key\n",
        "rtmp://a.example.com/live/key\t",
        "rtmp://a.example.com/live/key\r",
        "rtmp://a.example.com/live/key\x00",
        "rtmp://a.example.com/live/key ",
    ],
)
def test_tee_syntax_and_whitespace_are_rejected(url):
    with pytest.raises(DestinationError):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://a.example.com/live/key",
        "srt://a.example.com:9000",
        "a.rtmp.youtube.com/live2/key",
        "rtmp:///live/key",
        "rtmp://a.example.com",
        "rtmp://a.example.com/",
        "rtmp://a.example.com:notaport/live/key",
    ],
)
def test_malformed_urls_are_rejected(url):
    with pytest.raises(DestinationError):
        validate_url(url)


def test_error_names_the_destination_and_hides_the_key():
    with pytest.raises(DestinationError) as exc:
        build_tee_spec([YT, "rtmp://live.twitch.tv/app/live_123_secret|rtmp://x/y/z"])
    msg = str(exc.value)
    assert msg.startswith("destination 2:")
    assert "secret" not in msg
    assert exc.value.index == 1


def test_mask_url():
    assert mask_url(YT) == "rtmp://a.rtmp.youtube.com/live2/****"
    assert mask_url(FB) == "rtmps://live-api-s.facebook.com:443/rtmp/****"
    assert mask_url("rtmp://u:p@h.example.com/app/key?t=1") == "rtmp://****@h.example.com/app/****?****"
    assert "abc" not in mask_url("not a url abc")


def test_duplicates_dropped_in_order():
    spec = build_tee_spec([YT, TW, YT])
    assert spec.count(YT) == 1
    assert spec.index(YT) < spec.index(TW)
    assert build_tee_spec([YT, YT], dedupe=False).count(YT) == 2


def test_duplicates_collapsing_to_one_drop_the_queue():
    assert "use_fifo" not in build_tee_spec([YT, YT])


def test_no_destinations():
    with pytest.raises(ValueError):
        build_tee_spec([])


def test_a_bare_string_is_not_a_list():
    with pytest.raises(TypeError):
        build_tee_spec(YT)


def test_output_args_single_is_plain_flv():
    assert build_output_args([YT]) == ["-f", "flv", "-flvflags", "+no_duration_filesize", YT]


def test_output_args_several_is_tee():
    args = build_output_args([YT, TW])
    assert args[:2] == ["-f", "tee"]
    assert args[2] == build_tee_spec([YT, TW])


def test_command_maps_streams_explicitly():
    cmd = build_command([YT, TW], input="in.mp4")
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-i") + 1] == "in.mp4"
    assert cmd.count("-map") == 2
    assert cmd[-3:-1] == ["-f", "tee"]


def test_cli_prints_a_shell_quoted_spec(capsys):
    assert main([YT, TW]) == 0
    out = capsys.readouterr().out.strip()
    # What the shell hands ffmpeg after unquoting is exactly the spec.
    assert shlex.split(out) == [build_tee_spec([YT, TW])]


def test_cli_command_round_trips_through_the_shell(capsys):
    assert main(["--command", "-i", "my file.mp4", "--copy", YT, TW]) == 0
    argv = shlex.split(capsys.readouterr().out)
    assert argv == build_command([YT, TW], input="my file.mp4", encode=("-c", "copy"))


def test_cli_reads_a_file_and_skips_comments(tmp_path, capsys):
    f = tmp_path / "dest.txt"
    f.write_text(f"# my destinations\n{YT}\n\n  {TW}  \n")
    assert main(["--file", str(f), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == build_tee_spec([YT, TW])


def test_cli_reads_stdin(monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(f"{YT}\n{TW}\n"))
    assert main(["-f", "-", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == build_tee_spec([YT, TW])


def test_cli_error_is_masked_and_exit_1(capsys):
    assert main([YT, "rtmp://live.twitch.tv/app/sec ret"]) == 1
    err = capsys.readouterr().err
    assert "destination 2" in err and "sec" not in err.split("(")[-1]


def test_cli_no_urls(capsys):
    assert main([]) == 2


def test_cli_missing_file(capsys):
    assert main(["--file", "/nonexistent/dest.txt"]) == 2


def test_timeout_becomes_rw_timeout_in_microseconds():
    spec = build_tee_spec([YT, TW], timeout=10)
    assert "onfail=ignore:rw_timeout=10000000:use_fifo=1" in spec
    assert "rw_timeout=2500000" in build_tee_spec([YT], timeout=2.5)
    assert "rw_timeout" not in build_tee_spec([YT, TW])


@pytest.mark.parametrize("bad", [0, -1, True, "10"])
def test_timeout_must_be_positive(bad):
    with pytest.raises(ValueError):
        build_tee_spec([YT, TW], timeout=bad)


def test_single_output_carries_the_timeout():
    assert build_output_args([YT], timeout=5) == [
        "-f", "flv", "-flvflags", "+no_duration_filesize", "-rw_timeout", "5000000", YT,
    ]


def test_single_output_still_checks_options():
    with pytest.raises(ValueError):
        build_output_args([YT], onfail="nope")


def test_single_output_with_forced_queue_uses_tee():
    assert build_output_args([YT], fifo=True)[:2] == ["-f", "tee"]


def test_cli_timeout(capsys):
    assert main(["--timeout", "10", "--json", YT, TW]) == 0
    assert json.loads(capsys.readouterr().out) == build_tee_spec([YT, TW], timeout=10)
