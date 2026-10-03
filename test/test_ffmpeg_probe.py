"""
util/ffmpeg/probe.py —— 从 FFmpeg 的输出里取视频编码、分辨率、动态范围与音频编码声道。

只测纯文本解析：把 parse_streams 与 subprocess 分开，正是为了让它可以在
没有 FFmpeg 可执行文件的机器上验证。用例里的输出片段照抄真实 `ffmpeg -i` 的
stderr，形状（语言括号、轨道 id、色彩信息、行尾的 (default)）都来自实测。
"""

from util.ffmpeg.probe import parse_audio_stream, parse_streams, parse_video_stream

import pytest


class TestParseAudioStream:
    def test_aac_stereo(self):
        output = (
            "Input #0, matroska,webm, from 'x.mkv':\n"
            "  Metadata:\n"
            "    ENCODER         : Lavf61.7.100\n"
            "  Stream #0:0: Video: h264 (High), yuv420p, 1920x1080, 30 fps\n"
            "  Stream #0:1: Audio: aac (LC), 44100 Hz, stereo, fltp, 193 kb/s\n"
        )

        assert parse_audio_stream(output) == ("AAC", "2.0")

    def test_language_and_track_id_are_ignored(self):
        # 实测的两种修饰写法：语言在括号里、轨道 id 在方括号里，顺序也可能互换
        for tag in ("(und)", "[0x2]", "(und)[0x2]", "[0x2](und)"):
            output = f"  Stream #0:1{tag}: Audio: aac (LC), 44100 Hz, stereo, fltp, 193 kb/s\n"

            assert parse_audio_stream(output) == ("AAC", "2.0"), tag

    def test_disposition_suffix_does_not_break_channels(self):
        # 默认轨的行尾会多一段 (default)，它不能被当成声道
        output = "  Stream #0:1: Audio: aac (LC), 44100 Hz, stereo, fltp, 193 kb/s (default)\n"

        assert parse_audio_stream(output) == ("AAC", "2.0")

    def test_first_audio_stream_wins(self):
        output = (
            "  Stream #0:1: Audio: aac (LC), 44100 Hz, stereo, fltp, 193 kb/s\n"
            "  Stream #0:2: Audio: eac3, 48000 Hz, 5.1(side), fltp, 640 kb/s\n"
        )

        assert parse_audio_stream(output) == ("AAC", "2.0")

    @pytest.mark.parametrize("raw, expected", [
        ("eac3", "EAC3"),
        ("ac3", "AC3"),
        ("flac", "FLAC"),
        ("opus", "OPUS"),
        ("pcm_s16le", "PCM"),
    ])
    def test_codec_names(self, raw, expected):
        assert parse_audio_stream(f"  Stream #0:1: Audio: {raw}, 48000 Hz, stereo\n") == (expected, "2.0")

    def test_unknown_codec_falls_back_to_upper_case(self):
        # 认不出的编码照原样大写，不编一个名字出来
        assert parse_audio_stream("  Stream #0:1: Audio: foo, 48000 Hz, stereo\n") == ("FOO", "2.0")

    @pytest.mark.parametrize("layout, expected", [
        ("mono", "1.0"),
        ("stereo", "2.0"),
        ("5.1(side)", "5.1"),
        ("7.1", "7.1"),
    ])
    def test_channel_layouts(self, layout, expected):
        assert parse_audio_stream(f"  Stream #0:1: Audio: aac, 48000 Hz, {layout}, fltp\n") == ("AAC", expected)

    def test_channel_count_without_a_named_layout(self):
        output = "  Stream #0:1: Audio: aac, 48000 Hz, 2 channels, fltp\n"

        assert parse_audio_stream(output) == ("AAC", "2.0")

    def test_unknown_layout_leaves_channels_blank(self):
        # 认不出就不写声道，宁可空着也不编
        assert parse_audio_stream("  Stream #0:1: Audio: aac, 48000 Hz, fltp, 193 kb/s\n") == ("AAC", "")

    def test_video_only_input_returns_none(self):
        output = "  Stream #0:0: Video: h264 (High), yuv420p, 1920x1080, 30 fps\n"

        assert parse_audio_stream(output) is None

    def test_empty_output_returns_none(self):
        assert parse_audio_stream("") is None


class TestParseVideoStream:
    def test_sdr_avc_1080p(self):
        # 实测的 SDR 行：色彩信息 `(tv, bt709)` 三项相同时 FFmpeg 只写一个值
        output = (
            "  Stream #0:0: Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709), "
            "1920x1080 [SAR 1:1 DAR 16:9], 1743 kb/s, 30 fps, 30 tbr, 1k tbn (default)\n"
        )

        assert parse_video_stream(output) == ("AVC", "1920x1080", "SDR")

    @pytest.mark.parametrize("raw, expected", [
        ("h264", "AVC"),
        ("hevc", "HEVC"),
        ("av1", "AV1"),
        ("vp9", "VP9"),
    ])
    def test_video_codec_names(self, raw, expected):
        output = f"  Stream #0:0: Video: {raw} (Main), yuv420p(tv, bt709), 1920x1080, 30 fps\n"

        assert parse_video_stream(output) == (expected, "1920x1080", "SDR")

    def test_hdr10(self):
        output = (
            "  Stream #0:0: Video: hevc (Main 10) (hev1 / 0x31657668), "
            "yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x2160 [SAR 1:1 DAR 16:9], 24 fps\n"
        )

        assert parse_video_stream(output) == ("HEVC", "3840x2160", "HDR10")

    def test_hlg(self):
        output = (
            "  Stream #0:0: Video: hevc (Main 10), "
            "yuv420p10le(tv, bt2020nc/bt2020/arib-std-b67), 3840x2160, 25 fps\n"
        )

        assert parse_video_stream(output) == ("HEVC", "3840x2160", "HLG")

    def test_dolby_vision_wins_over_transfer_curve(self):
        # 杜比视界同样走 smpte2084，只能靠编码标签区分，判定顺序不能反
        output = (
            "  Stream #0:0: Video: hevc (Main 10) (dvhe / 0x65687664), "
            "yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x2160, 24 fps\n"
        )

        assert parse_video_stream(output) == ("HEVC", "3840x2160", "DV")

    def test_no_color_info_is_sdr(self):
        # 读不到色彩信息时按 SDR，不要因为读不到就当 HDR
        output = "  Stream #0:0: Video: h264 (High), yuv420p, 1920x1080, 30 fps\n"

        assert parse_video_stream(output) == ("AVC", "1920x1080", "SDR")

    def test_hdr10_with_scan_type_suffix(self):
        # 括号里色彩信息后面还会跟扫描方式：`(tv, bt2020nc/bt2020/smpte2084, progressive)`。
        # 不先把 `progressive` 截掉，传输特性就变成 `smpte2084, progressive`，
        # 匹配不上任何曲线，HDR 会被静默判成 SDR —— 实测合成素材时踩到过
        output = (
            "  Stream #0:0: Video: h264 (High 4:4:4 Predictive) (avc1 / 0x31637661), "
            "yuv444p(tv, bt2020nc/bt2020/smpte2084, progressive), 1280x720 [SAR 1:1 DAR 16:9], 1 fps\n"
        )

        assert parse_video_stream(output) == ("AVC", "1280x720", "HDR10")

    def test_hlg_with_scan_type_suffix(self):
        output = (
            "  Stream #0:0: Video: hevc (Main 10), "
            "yuv420p10le(tv, bt2020nc/bt2020/arib-std-b67, progressive), 3840x2160, 25 fps\n"
        )

        assert parse_video_stream(output) == ("HEVC", "3840x2160", "HLG")

    def test_sdr_transfer_with_scan_type_suffix(self):
        # 同一个坑的反面：带 progressive 的 SDR 也要照旧判成 SDR，别修出个反向 bug
        output = (
            "  Stream #0:0: Video: h264 (High), "
            "yuv420p(tv, bt709, progressive), 852x480 [SAR 640:639 DAR 16:9], 25 fps\n"
        )

        assert parse_video_stream(output) == ("AVC", "852x480", "SDR")

    def test_profile_parenthesis_is_not_taken_as_color_info(self):
        # `h264 (High)` 是 profile，不能被当成色彩信息读走；认不出就落 SDR
        output = "  Stream #0:0: Video: mpeg4 (Simple Profile) (mp4v / 0x7634706d), yuv420p, 640x480\n"

        assert parse_video_stream(output) == ("MPEG4", "640x480", "SDR")

    def test_hex_codec_tag_is_not_taken_as_resolution(self):
        # `0x31637661` 是编码标签，不能被当成 WxH
        output = "  Stream #0:0: Video: h264 (avc1 / 0x31637661), yuv420p(tv, bt709)\n"

        assert parse_video_stream(output) == ("AVC", "", "SDR")

    def test_cover_image_is_skipped(self):
        # 内嵌封面也是一条视频流；取错会把分辨率与动态范围读成封面尺寸
        output = (
            "  Stream #0:0: Video: mjpeg (Baseline), yuvj420p(pc, bt470bg/unknown/unknown), 320x240, 90k tbr\n"
            "  Stream #0:1: Video: h264 (High), yuv420p(tv, bt709), 1920x1080, 30 fps\n"
        )

        assert parse_video_stream(output) == ("AVC", "1920x1080", "SDR")

    def test_audio_only_input_returns_none(self):
        assert parse_video_stream("  Stream #0:0: Audio: aac (LC), 44100 Hz, stereo\n") is None

    def test_empty_output_returns_none(self):
        assert parse_video_stream("") is None


class TestParseStreams:
    def test_reads_both_tracks_in_one_pass(self):
        output = (
            "Input #0, matroska,webm, from 'x.mkv':\n"
            "  Metadata:\n"
            "    ENCODER         : Lavf61.7.100\n"
            "  Stream #0:0: Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709), "
            "1920x1080 [SAR 1:1 DAR 16:9], 1743 kb/s, 30 fps (default)\n"
            "  Stream #0:1(und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 193 kb/s (default)\n"
        )

        streams = parse_streams(output)

        assert streams.audio_codec == "AAC"
        assert streams.audio_channels == "2.0"
        assert streams.video_codec == "AVC"
        assert streams.video_resolution == "1920x1080"
        assert streams.video_dynamic_range == "SDR"
        assert streams

    def test_video_only_stream_is_still_truthy(self):
        # 纯视频任务（无音轨）也要能通过真值判断，否则改名前的回填会被整段跳过
        streams = parse_streams("  Stream #0:0: Video: h264 (High), yuv420p(tv, bt709), 1920x1080\n")

        assert streams.audio_codec == ""
        assert streams.video_resolution == "1920x1080"
        assert streams

    def test_audio_only_stream_is_still_truthy(self):
        streams = parse_streams("  Stream #0:0: Audio: flac, 48000 Hz, stereo, s16\n")

        assert streams.audio_codec == "FLAC"
        assert streams.video_codec == ""
        assert streams

    def test_empty_output_is_falsy(self):
        # 五项全空代表这条命令行没提供任何可用信息，调用方据此保留原文件名
        assert not parse_streams("")
        assert not parse_streams("Input #0, matroska,webm, from 'x.mkv':\n  Duration: 00:03:33.00\n")
