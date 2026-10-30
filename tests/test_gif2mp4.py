"""Tests for gif2mp4. Needs ffmpeg and ffprobe on PATH.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import gif2mp4  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + list(args), check=True)


def make_gif(path, delays_cs=(10, 10, 10, 10, 10), size="64x48"):
    """A tiny GIF with the given per-frame delays (in 1/100 s), made from a test pattern."""
    pts, t = [], 0
    for d in delays_cs:
        pts.append(t)
        t += d
    expr = "0"
    for i in range(len(pts) - 1, 0, -1):
        expr = "if(eq(N,%d),%d,%s)" % (i, pts[i], expr)
    # The last frame keeps the 1/rate duration of the source, so use rate = 100 / last delay.
    rate = Fraction(100, delays_cs[-1])
    ff("-f", "lavfi", "-i", "testsrc2=size=%s:rate=%s:duration=%s,format=yuv444p,hue=h=n*70,format=rgb24" % (
        size, rate, float(len(delays_cs) / rate)),
       "-enc_time_base", "1/100", "-vf", "settb=1/100,setpts='%s'" % expr,
       "-fps_mode", "passthrough", path)


def make_transparent_gif(path):
    """A 64x48 GIF with a red square in the middle and a see-through border."""
    ff("-filter_complex",
       "color=c=black@0:s=64x48:r=10:d=0.5,format=rgba[a];"
       "color=c=red:s=24x20:r=10:d=0.5,format=rgba[b];"
       "[a][b]overlay=20:14:format=auto,split[s0][s1];"
       "[s0]palettegen=reserve_transparent=1[p];[s1][p]paletteuse",
       "-gifflags", "+transdiff", path)


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = gif2mp4.main(list(args))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def stream_info(path):
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries",
         "stream=codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate,nb_read_frames:format=duration",
         "-of", "json", path], capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    s = data["streams"][0]
    s["duration"] = float(data["format"]["duration"])
    s["nb_read_frames"] = int(s["nb_read_frames"])
    return s


def top_level_boxes(path):
    boxes = []
    with open(path, "rb") as fh:
        while True:
            head = fh.read(8)
            if len(head) < 8:
                break
            size, kind = struct.unpack(">I4s", head)
            boxes.append(kind.decode("latin1"))
            if size == 1:
                size = struct.unpack(">Q", fh.read(8))[0]
                fh.seek(size - 16, 1)
            elif size == 0:
                break
            else:
                fh.seek(size - 8, 1)
    return boxes


def frames_rgb(path):
    """Every frame of a tiny video as raw RGB bytes."""
    info = stream_info(path)
    size = info["width"] * info["height"] * 3
    res = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True)
    data = res.stdout
    return [data[i:i + size] for i in range(0, len(data), size)]


def diff(a, b):
    """Mean absolute difference between two frames, 0 to 255. Lossy coding adds a little noise."""
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def pixel_rgb(path, x, y):
    """Colour of one pixel of the first frame (the files are tiny, so decode it whole)."""
    info = stream_info(path)
    w, h = info["width"], info["height"]
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True)
    off = (y * w + x) * 3
    return tuple(res.stdout[off:off + 3])


class ParseTests(unittest.TestCase):
    def test_colors(self):
        self.assertEqual(gif2mp4.parse_color("white"), "white")
        self.assertEqual(gif2mp4.parse_color("#FF8800"), "0xff8800")
        self.assertEqual(gif2mp4.parse_color("ff8800"), "0xff8800")
        self.assertEqual(gif2mp4.parse_color("0x00ff00"), "0x00ff00")

    def test_bad_color(self):
        for bad in ("#fff", "red;rm", "", "12"):
            with self.assertRaises(argparse.ArgumentTypeError):
                gif2mp4.parse_color(bad)

    def test_fps_exact_when_delays_share_a_step(self):
        d = lambda *cs: [Fraction(c, 100) for c in cs]
        self.assertEqual(gif2mp4.choose_fps(d(10, 10, 10)), (Fraction(10), "exact"))
        self.assertEqual(gif2mp4.choose_fps(d(4, 4, 4)), (Fraction(25), "exact"))
        self.assertEqual(gif2mp4.choose_fps(d(3, 3)), (Fraction(100, 3), "exact"))
        self.assertEqual(gif2mp4.choose_fps(d(4, 8, 12)), (Fraction(25), "exact"))
        self.assertEqual(gif2mp4.choose_fps(d(10, 20, 10)), (Fraction(10), "exact"))

    def test_fps_fallback_when_step_is_too_fine(self):
        d = lambda *cs: [Fraction(c, 100) for c in cs]
        self.assertEqual(gif2mp4.choose_fps(d(3, 7, 3)), (Fraction(50), "fallback"))

    def test_fps_average_when_delays_only_differ_by_rounding(self):
        d = lambda *cs: [Fraction(c, 100) for c in cs]
        self.assertEqual(gif2mp4.choose_fps(d(6, 7, 7) * 5), (Fraction(15), "average"))
        self.assertEqual(gif2mp4.choose_fps(d(3, 4, 3, 3))[1], "average")

    def test_plan_loops_and_cap(self):
        delays = [Fraction(1, 10)] * 5          # 0.5 s at 10 fps
        plan = gif2mp4.plan_timing(delays, 3, None)
        self.assertEqual(plan["frames"], 15)
        self.assertEqual(float(plan["seconds"]), 1.5)
        capped = gif2mp4.plan_timing(delays, 10, 2)
        self.assertEqual(capped["frames"], 20)
        self.assertTrue(capped["capped"])
        filled = gif2mp4.plan_timing(delays, 0, 2)
        self.assertEqual(filled["frames"], 20)

    def test_even_up(self):
        self.assertEqual(gif2mp4.even_up(321), 322)
        self.assertEqual(gif2mp4.even_up(320), 320)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are required")
class ConvertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gif2mp4-test-")
        cls.gif = os.path.join(cls.tmp, "five.gif")
        make_gif(cls.gif)                       # 5 frames x 10 cs = 0.5 s

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def out(self, name):
        return os.path.join(self.tmp, name)

    def test_defaults_mp4(self):
        out = self.out("default.mp4")
        code, stdout, _ = run_cli(self.gif, "-o", out)
        self.assertEqual(code, 0)
        s = stream_info(out)
        self.assertEqual(s["codec_name"], "h264")
        self.assertEqual(s["pix_fmt"], "yuv420p")
        self.assertEqual((s["width"], s["height"]), (64, 48))
        self.assertEqual(s["r_frame_rate"], "10/1")
        self.assertEqual(s["avg_frame_rate"], "10/1")
        self.assertEqual(s["nb_read_frames"], 5)
        self.assertAlmostEqual(s["duration"], 0.5, places=2)
        self.assertIn("size before:", stdout)
        self.assertIn("size after:", stdout)

    def test_faststart(self):
        out = self.out("fast.mp4")
        self.assertEqual(run_cli(self.gif, "-o", out)[0], 0)
        boxes = top_level_boxes(out)
        self.assertIn("moov", boxes)
        self.assertIn("mdat", boxes)
        self.assertLess(boxes.index("moov"), boxes.index("mdat"))

    def test_loops_set_the_length(self):
        for loops in (1, 3, 4):
            out = self.out("loops%d.mp4" % loops)
            self.assertEqual(run_cli(self.gif, "-o", out, "--loops", str(loops))[0], 0)
            s = stream_info(out)
            self.assertEqual(s["nb_read_frames"], 5 * loops)
            self.assertAlmostEqual(s["duration"], 0.5 * loops, places=2)

    def test_each_loop_repeats_the_same_frames(self):
        out = self.out("repeat.mp4")
        self.assertEqual(run_cli(self.gif, "-o", out, "--loops", "2", "--crf", "10")[0], 0)
        f = frames_rgb(out)
        self.assertEqual(len(f), 10)
        for i in range(5):
            self.assertLess(diff(f[i], f[i + 5]), 3, i)       # same picture one loop later
        self.assertGreater(diff(f[0], f[1]), 8)               # but the frames within a loop differ

    def test_max_seconds_caps_the_length(self):
        out = self.out("cap.mp4")
        code, stdout, _ = run_cli(self.gif, "-o", out, "--loops", "10", "--max-seconds", "1.2")
        self.assertEqual(code, 0)
        s = stream_info(out)
        self.assertEqual(s["nb_read_frames"], 12)
        self.assertAlmostEqual(s["duration"], 1.2, places=2)
        self.assertIn("cut at 1.2 s", stdout)

    def test_loops_zero_fills_max_seconds(self):
        out = self.out("fill.mp4")
        self.assertEqual(run_cli(self.gif, "-o", out, "--loops", "0", "--max-seconds", "2")[0], 0)
        s = stream_info(out)
        self.assertAlmostEqual(s["duration"], 2.0, places=2)
        self.assertEqual(s["nb_read_frames"], 20)

    def test_loops_zero_needs_max_seconds(self):
        code, _, err = run_cli(self.gif, "-o", self.out("x.mp4"), "--loops", "0")
        self.assertEqual(code, 1)
        self.assertIn("--max-seconds", err)

    def test_webm(self):
        out = self.out("clip.webm")
        self.assertEqual(run_cli(self.gif, "-o", out)[0], 0)      # format from extension
        s = stream_info(out)
        self.assertEqual(s["codec_name"], "vp9")
        self.assertEqual(s["pix_fmt"], "yuv420p")
        self.assertEqual(s["nb_read_frames"], 5)
        self.assertAlmostEqual(s["duration"], 0.5, places=1)

    def test_format_flag_picks_default_name(self):
        local = os.path.join(self.tmp, "named.gif")
        shutil.copy(self.gif, local)
        self.assertEqual(run_cli(local, "--format", "webm")[0], 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "named.webm")))
        self.assertEqual(run_cli(local)[0], 0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "named.mp4")))

    def test_format_and_extension_must_agree(self):
        code, _, err = run_cli(self.gif, "-o", self.out("a.mp4"), "--format", "webm")
        self.assertEqual(code, 1)
        self.assertIn("does not match", err)

    def test_crf_changes_the_size(self):
        low = self.out("crf15.mp4")
        high = self.out("crf40.mp4")
        self.assertEqual(run_cli(self.gif, "-o", low, "--crf", "15")[0], 0)
        self.assertEqual(run_cli(self.gif, "-o", high, "--crf", "40")[0], 0)
        self.assertGreater(os.path.getsize(low), os.path.getsize(high))

    def test_crf_zero_refused_for_mp4(self):
        code, _, err = run_cli(self.gif, "-o", self.out("lossless.mp4"), "--crf", "0")
        self.assertEqual(code, 1)
        self.assertIn("lossless", err)

    def test_crf_limit_per_format(self):
        code, _, err = run_cli(self.gif, "-o", self.out("hi.mp4"), "--crf", "60")
        self.assertEqual(code, 1)
        self.assertIn("maximum 51", err)

    def test_refuses_to_overwrite(self):
        out = self.out("exists.mp4")
        self.assertEqual(run_cli(self.gif, "-o", out)[0], 0)
        code, _, err = run_cli(self.gif, "-o", out)
        self.assertEqual(code, 1)
        self.assertIn("--overwrite", err)
        self.assertEqual(run_cli(self.gif, "-o", out, "--overwrite")[0], 0)

    def test_no_part_file_left_on_failure(self):
        out = self.out("badcolor.mp4")
        code, _, _ = run_cli(self.gif, "-o", out, "--bg", "notacolorname")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(out + ".part"))

    def test_missing_and_wrong_input(self):
        code, _, err = run_cli(self.out("nope.gif"))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)
        txt = self.out("text.gif")
        with open(txt, "w") as fh:
            fh.write("not a gif")
        code, _, err = run_cli(txt)
        self.assertEqual(code, 1)
        self.assertIn("not a GIF", err)

    def test_bad_options_exit_2(self):
        for argv in (["x.gif", "--loops", "-1"], ["x.gif", "--max-seconds", "0"],
                     ["x.gif", "--format", "avi"]):
            code, _, _ = run_cli(*argv)
            self.assertEqual(code, 2)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are required")
class TimingAndSizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gif2mp4-test-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_gif_has_the_delays_we_asked_for(self):
        gif = self.path("check.gif")
        make_gif(gif, (10, 20, 10))
        delays = gif2mp4.probe(shutil.which("ffprobe"), gif)["delays"]
        self.assertEqual(delays, [Fraction(1, 10), Fraction(1, 5), Fraction(1, 10)])

    def test_uneven_delays_keep_their_timing(self):
        # frame 2 lasts twice as long, so it must be shown for two output frames
        gif = self.path("uneven.gif")
        make_gif(gif, (10, 20, 10))
        out = self.path("uneven.mp4")
        self.assertEqual(run_cli(gif, "-o", out, "--crf", "10")[0], 0)
        s = stream_info(out)
        self.assertEqual(s["r_frame_rate"], "10/1")
        self.assertEqual(s["nb_read_frames"], 4)
        self.assertAlmostEqual(s["duration"], 0.4, places=2)
        f = frames_rgb(out)
        self.assertLess(diff(f[1], f[2]), 3)       # the long frame is shown twice
        self.assertGreater(diff(f[0], f[1]), 8)
        self.assertGreater(diff(f[2], f[3]), 8)

    def test_fractional_rate_is_kept(self):
        gif = self.path("three.gif")                   # 3 cs frames = 33.333 fps
        make_gif(gif, (3,) * 10)
        out = self.path("three.mp4")
        self.assertEqual(run_cli(gif, "-o", out)[0], 0)
        s = stream_info(out)
        self.assertEqual(s["r_frame_rate"], "100/3")
        self.assertEqual(s["nb_read_frames"], 10)
        self.assertAlmostEqual(s["duration"], 0.3, places=2)

    def test_rounded_delays_use_the_average_rate(self):
        # a 15 fps GIF is stored as 6 cs, 7 cs, 7 cs, ... so the video should be 15 fps, not 50
        gif = self.path("fifteen.gif")
        make_gif(gif, (6, 7, 7) * 5)                       # 15 frames in 1.00 s
        out = self.path("fifteen.mp4")
        code, stdout, _ = run_cli(gif, "-o", out, "--crf", "10")
        self.assertEqual(code, 0)
        s = stream_info(out)
        self.assertEqual(s["r_frame_rate"], "15/1")
        self.assertEqual(s["nb_read_frames"], 15)
        self.assertAlmostEqual(s["duration"], 1.0, places=2)
        f = frames_rgb(out)
        changes = sum(1 for i in range(1, len(f)) if diff(f[i - 1], f[i]) > 8)
        self.assertEqual(changes, 14)                      # every GIF frame shown once
        self.assertIn("average", stdout)

    def test_fine_delays_fall_back_to_50_fps_without_dropping_frames(self):
        gif = self.path("fine.gif")                    # 3 cs and 7 cs: no common step above 1 cs
        make_gif(gif, (3, 7, 3, 7, 3, 7))
        out = self.path("fine.mp4")
        code, stdout, _ = run_cli(gif, "-o", out, "--crf", "10")
        self.assertEqual(code, 0)
        s = stream_info(out)
        self.assertEqual(s["r_frame_rate"], "50/1")
        self.assertEqual(s["nb_read_frames"], 15)
        self.assertAlmostEqual(s["duration"], 0.3, places=2)
        f = frames_rgb(out)
        # GIF frames show for 3, 7, 3, 7, 3, 7 cs = 1.5, 3.5, ... output frames at 50 fps,
        # so each of the 6 pictures must show up at least once
        changes = sum(1 for i in range(1, len(f)) if diff(f[i - 1], f[i]) > 8)
        self.assertEqual(changes, 5)
        self.assertIn("50 fps", stdout)

    def test_odd_size_is_padded_to_even(self):
        gif = self.path("odd.gif")
        make_gif(gif, size="65x47")
        out = self.path("odd.mp4")
        self.assertEqual(run_cli(gif, "-o", out)[0], 0)
        s = stream_info(out)
        self.assertEqual((s["width"], s["height"]), (66, 48))
        self.assertEqual(s["pix_fmt"], "yuv420p")

    def test_odd_size_scaled_to_even(self):
        gif = self.path("odd2.gif")
        make_gif(gif, size="65x47")
        out = self.path("odd2.mp4")
        self.assertEqual(run_cli(gif, "-o", out, "--even", "scale")[0], 0)
        s = stream_info(out)
        self.assertEqual((s["width"], s["height"]), (66, 48))

    def test_odd_size_webm(self):
        gif = self.path("odd3.gif")
        make_gif(gif, size="33x31")
        out = self.path("odd3.webm")
        self.assertEqual(run_cli(gif, "-o", out)[0], 0)
        s = stream_info(out)
        self.assertEqual((s["width"], s["height"]), (34, 32))

    def test_transparency_is_flattened_onto_white_by_default(self):
        gif = self.path("clear.gif")
        make_transparent_gif(gif)
        out = self.path("clear.mp4")
        self.assertEqual(run_cli(gif, "-o", out)[0], 0)
        r, g, b = pixel_rgb(out, 2, 2)
        self.assertTrue(r > 240 and g > 240 and b > 240, (r, g, b))
        r, g, b = pixel_rgb(out, 32, 24)
        self.assertTrue(r > 200 and g < 60 and b < 60, (r, g, b))

    def test_transparency_with_chosen_background(self):
        gif = self.path("clear2.gif")
        make_transparent_gif(gif)
        out = self.path("clear2.mp4")
        self.assertEqual(run_cli(gif, "-o", out, "--bg", "#0000ff")[0], 0)
        r, g, b = pixel_rgb(out, 2, 2)
        self.assertTrue(r < 40 and g < 40 and b > 200, (r, g, b))

    def test_output_is_tagged_bt709(self):
        gif = self.path("tag.gif")
        make_gif(gif)
        out = self.path("tag.mp4")
        self.assertEqual(run_cli(gif, "-o", out)[0], 0)
        res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=color_space,color_range", "-of", "csv=p=0", out],
                             capture_output=True, text=True, check=True)
        self.assertEqual(sorted(res.stdout.strip().split(",")), ["bt709", "tv"])

    def test_zero_ish_delays_play_as_ten_cs(self):
        gif = self.path("fast.gif")
        make_gif(gif, (1, 1, 1, 1))
        out = self.path("fast.mp4")
        self.assertEqual(run_cli(gif, "-o", out)[0], 0)
        s = stream_info(out)
        self.assertEqual(s["r_frame_rate"], "10/1")
        self.assertEqual(s["nb_read_frames"], 4)
        self.assertAlmostEqual(s["duration"], 0.4, places=2)

    def test_single_frame_gif(self):
        gif = self.path("still.gif")
        ff("-f", "lavfi", "-i", "testsrc2=size=64x48:rate=10:duration=0.1", "-frames:v", "1", gif)
        out = self.path("still.mp4")
        self.assertEqual(run_cli(gif, "-o", out, "--loops", "3")[0], 0)
        s = stream_info(out)
        self.assertEqual(s["nb_read_frames"], 3)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are required")
class WebpTests(unittest.TestCase):
    """Animated WebP only works on ffmpeg builds that can decode it."""

    def test_webp_works_or_fails_cleanly(self):
        tool = shutil.which("gif2webp")
        if not tool:
            self.skipTest("gif2webp (libwebp tools) not installed, cannot make a test WebP")
        tmp = tempfile.mkdtemp(prefix="gif2mp4-test-")
        try:
            gif = os.path.join(tmp, "a.gif")
            make_gif(gif)
            webp = os.path.join(tmp, "a.webp")
            subprocess.run([tool, "-q", "80", gif, "-o", webp], check=True,
                           capture_output=True)
            out = os.path.join(tmp, "a.mp4")
            code, _, err = run_cli(webp, "-o", out)
            if code == 0:
                s = stream_info(out)
                self.assertEqual(s["nb_read_frames"], 5)
            else:
                self.assertEqual(code, 1)
                self.assertIn("cannot decode animated WebP", err)
                self.assertFalse(os.path.exists(out))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
