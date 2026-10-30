#!/usr/bin/env python3
"""gif2mp4: turn a GIF into an MP4 or WebM that plays everywhere, with ffmpeg.

Fixes the usual problems in one command: one steady frame rate that matches
the GIF's own timing, even width and height for H.264, yuv420p, faststart,
and see-through areas flattened onto a background color. Standard library
only.
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from fractions import Fraction

__version__ = "1.0.0"

FORMATS = ("mp4", "webm")
EVEN_MODES = ("pad", "scale")
DEFAULT_CRF = {"mp4": 23, "webm": 32}
CRF_MAX = {"mp4": 51, "webm": 63}
MAX_FPS = 60          # above this the exact frame rate is not used
FALLBACK_FPS = 50     # used when the GIF delays do not share a clean rate
MAX_LOOPS = 1000


class Gif2Mp4Error(Exception):
    """A problem the user can act on. Printed without a traceback."""


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

def human_size(n):
    if n >= 1024 ** 2:
        return "%.2f MB" % (n / 1024 ** 2)
    if n >= 1024:
        return "%.1f KB" % (n / 1024)
    return "%d B" % n


def _int_range(lo, hi, name):
    def conv(text):
        try:
            v = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError("%s must be a whole number" % name)
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(
                "%s must be between %d and %d" % (name, lo, hi))
        return v
    return conv


def _positive_float(name):
    def conv(text):
        try:
            v = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError("%s must be a number" % name)
        if not v > 0 or math.isinf(v):
            raise argparse.ArgumentTypeError("%s must be above zero" % name)
        return v
    return conv


def parse_color(text):
    """'white', '#ff8800', 'ff8800', '0xff8800' -> a string ffmpeg accepts."""
    s = str(text).strip()
    m = re.fullmatch(r"(?:#|0x)?([0-9a-fA-F]{6})", s)
    if m:
        return "0x" + m.group(1).lower()
    if re.fullmatch(r"[a-zA-Z]+", s):
        return s.lower()
    raise argparse.ArgumentTypeError(
        "bad color %r, use a name (white, black), or hex like #ff8800" % text)


def format_fps(fps):
    """Fraction -> '10', '25', '100/3'."""
    if fps.denominator == 1:
        return str(fps.numerator)
    return "%d/%d" % (fps.numerator, fps.denominator)


# ----------------------------------------------------------------------------
# ffmpeg / ffprobe
# ----------------------------------------------------------------------------

def find_tools():
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        missing = " and ".join(n for n, p in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not p)
        raise Gif2Mp4Error(
            "%s not found on your PATH. Install ffmpeg first:\n"
            "  macOS:          brew install ffmpeg\n"
            "  Debian/Ubuntu:  sudo apt install ffmpeg\n"
            "  Windows:        winget install ffmpeg" % missing)
    return ffmpeg, ffprobe


def sniff(path):
    """Return 'gif', 'webp' or None from the first bytes of the file."""
    with open(path, "rb") as fh:
        head = fh.read(12)
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def probe(ffprobe, path):
    """Return dict(width, height, delays) where delays are frame durations in
    seconds, as exact Fractions rounded to a millisecond."""
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height:packet=pts_time,duration_time",
           "-of", "json", path]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise Gif2Mp4Error("ffprobe could not read %s:\n%s" % (path, res.stderr.strip()))
    try:
        data = json.loads(res.stdout)
        stream = data["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
    except (ValueError, KeyError, IndexError):
        raise Gif2Mp4Error("%s has no video stream ffmpeg can read." % path)

    pts, dur = [], []
    for p in data.get("packets", []):
        try:
            pts.append(Fraction(round(float(p["pts_time"]) * 1000), 1000))
        except (KeyError, ValueError):
            continue
        try:
            dur.append(Fraction(round(float(p["duration_time"]) * 1000), 1000))
        except (KeyError, ValueError):
            dur.append(None)
    if not pts:
        raise Gif2Mp4Error("no frames found in %s." % path)

    delays = []
    for i, d in enumerate(dur):
        if d is None or d <= 0:
            # fall back to the gap to the next frame, else to the one before
            if i + 1 < len(pts):
                d = pts[i + 1] - pts[i]
            elif delays:
                d = delays[-1]
            else:
                d = Fraction(1, 10)
        delays.append(d)
    delays = [d if d > 0 else Fraction(1, 100) for d in delays]
    return {"width": width, "height": height, "delays": delays}


def normalize_delays(delays, keep_short=False):
    """GIFs often store 0 or 1 cs, meaning 'as fast as you can'. Viewers commonly play
    anything under 2 cs as 10 cs, so do the same unless asked not to."""
    if keep_short:
        return list(delays)
    return [Fraction(1, 10) if d < Fraction(2, 100) else d for d in delays]


def choose_fps(delays):
    """One steady frame rate for a list of frame delays (seconds).

    Returns (fps, kind). kind is:
      "exact"    the delays share a common step (all 10 cs, or 4 cs and 8 cs), so
                 every frame lasts exactly as long as in the GIF;
      "average"  the delays only differ by rounding (15 fps cannot be written in
                 hundredths of a second, so a GIF alternates 6 cs and 7 cs), so the
                 average rate is used and the frame count stays the same;
      "fallback" anything else: FALLBACK_FPS, which keeps every frame (a GIF frame
                 is never shorter than 2 cs) and the total length.
    """
    ms = [int(d * 1000) for d in delays]
    g = 0
    for v in ms:
        g = math.gcd(g, v)
    g = max(g, 1)
    exact = Fraction(1000, g)
    if exact <= MAX_FPS:
        return exact, "exact"
    total = sum(delays, Fraction(0))
    mean = total / len(delays)
    avg = Fraction(len(delays)) / total
    if avg <= MAX_FPS and all(abs(d - mean) <= Fraction(1, 100) for d in delays):
        return avg.limit_denominator(1001), "average"
    return Fraction(FALLBACK_FPS), "fallback"


def even_up(n):
    return n + (n % 2)


def plan_timing(delays, loops, max_seconds):
    """Work out frame rate, loop count and output length.

    Returns dict(fps, exact, one_play, loops, seconds, frames, capped).
    `loops` of 0 means repeat as often as it takes to fill max_seconds.
    """
    fps, kind = choose_fps(delays)
    one_play = sum(delays, Fraction(0))
    capped = False
    if loops == 0:
        n_loops = max(1, math.ceil(Fraction(max_seconds).limit_denominator(1000) / one_play))
    else:
        n_loops = loops
    seconds = one_play * n_loops
    if max_seconds is not None:
        cap = Fraction(max_seconds).limit_denominator(1000)
        if seconds > cap:
            seconds = cap
            capped = True
    frames = max(1, int(round(seconds * fps)))
    seconds = Fraction(frames) / fps
    # loops actually started
    started = max(1, math.ceil(seconds / one_play)) if one_play > 0 else 1
    return {"fps": fps, "kind": kind, "one_play": one_play, "loops": n_loops,
            "started": min(started, n_loops), "seconds": seconds, "frames": frames,
            "capped": capped}


def build_filter(width, height, fps, bg, even):
    """filter_complex: flatten onto a solid background of even size, tag as BT.709."""
    ew, eh = even_up(width), even_up(height)
    # Shift the frames half an output frame earlier, so each output frame lands in the
    # middle of its GIF frame and a little rounding in the delays cannot skip a frame.
    fg = "[0:v]format=rgba,setpts=PTS-%d/(2*%d*TB)" % (fps.denominator, fps.numerator)
    if even == "scale" and (ew != width or eh != height):
        fg += ",scale=%d:%d:flags=lanczos" % (ew, eh)
    fg += "[fg]"
    bgsrc = "color=c=%s:s=%dx%d:r=%s,format=rgb24[bg]" % (bg, ew, eh, format_fps(fps))
    out = ("[bg][fg]overlay=0:0:format=gbrp:eof_action=repeat,"
           "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p[v]")
    return ";".join([fg, bgsrc, out])


def build_command(ffmpeg, src, dst, info, plan, fmt, crf, bg, even):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if plan["loops"] > 1:
        cmd += ["-stream_loop", str(plan["loops"] - 1)]
    cmd += ["-i", src,
            "-filter_complex", build_filter(info["width"], info["height"], plan["fps"], bg, even),
            "-map", "[v]", "-an", "-sn", "-dn", "-map_metadata", "-1"]
    if fmt == "mp4":
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                "-profile:v", "high", "-movflags", "+faststart"]
    else:
        cmd += ["-c:v", "libvpx-vp9", "-crf", str(crf), "-b:v", "0",
                "-row-mt", "1", "-cpu-used", "2"]
    cmd += ["-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-color_range", "tv",
            "-r", format_fps(plan["fps"]), "-fps_mode", "cfr",
            "-frames:v", str(plan["frames"]),
            "-f", "mp4" if fmt == "mp4" else "webm", dst]
    return cmd


def run_ffmpeg(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        tail = "\n".join(res.stderr.strip().splitlines()[-6:])
        raise Gif2Mp4Error("ffmpeg failed:\n%s" % tail)


def check_decodes(ffmpeg, path):
    """Animated WebP is only readable by newer ffmpeg builds. Test one frame."""
    res = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                          "-i", path, "-frames:v", "1", "-f", "null", "-"],
                         capture_output=True, text=True)
    return res.returncode == 0


def probe_output(ffprobe, path):
    cmd = [ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
           "-show_entries", "stream=codec_name,width,height,r_frame_rate,nb_read_frames:format=duration",
           "-of", "json", path]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout)
        s = data["streams"][0]
        return {"codec": s["codec_name"], "width": int(s["width"]), "height": int(s["height"]),
                "fps": s["r_frame_rate"], "frames": int(s["nb_read_frames"]),
                "duration": float(data["format"]["duration"])}
    except (ValueError, KeyError, IndexError):
        return None


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="gif2mp4",
        description="Turn a GIF (or animated WebP) into an MP4 or WebM that plays everywhere.",
        epilog="Examples:\n"
               "  gif2mp4 cat.gif\n"
               "  gif2mp4 cat.gif --loops 3 --max-seconds 10\n"
               "  gif2mp4 logo.gif --bg '#101010' --format webm -o logo.webm\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="the GIF (or animated WebP) to convert")
    p.add_argument("-o", "--output", help="output file (default: input name with .mp4 or .webm)")
    p.add_argument("--format", choices=FORMATS,
                   help="mp4 (H.264) or webm (VP9). Default: from the -o extension, else mp4")
    p.add_argument("--crf", type=_int_range(0, 63, "--crf"),
                   help="quality, lower is better and bigger. Default 23 for mp4, 32 for webm. "
                        "mp4 allows 1-51, webm 0-63")
    p.add_argument("--bg", type=parse_color, default="white", metavar="COLOR",
                   help="color for see-through parts: a name or hex like #ff8800. Default white")
    p.add_argument("--loops", type=_int_range(0, MAX_LOOPS, "--loops"), default=1, metavar="N",
                   help="play the GIF N times. Default 1. 0 repeats until --max-seconds is filled")
    p.add_argument("--max-seconds", type=_positive_float("--max-seconds"), metavar="S",
                   help="never make a video longer than S seconds")
    p.add_argument("--even", choices=EVEN_MODES, default="pad",
                   help="how to fix an odd width or height: pad by 1 px with the background "
                        "color (default) or scale up by 1 px")
    p.add_argument("--keep-short-delays", action="store_true",
                   help="keep frame delays under 2 cs as they are (default: play them as 10 cs)")
    p.add_argument("--overwrite", action="store_true", help="replace the output file if it exists")
    p.add_argument("--version", action="version", version="gif2mp4 " + __version__)
    return p


def resolve_output(args):
    fmt = args.format
    out = args.output
    if out:
        ext = os.path.splitext(out)[1].lower().lstrip(".")
        if fmt is None:
            if ext in FORMATS:
                fmt = ext
            else:
                raise Gif2Mp4Error(
                    "cannot tell the format from %r. Use .mp4 or .webm, or pass --format." % out)
        elif ext != fmt:
            raise Gif2Mp4Error("--format %s does not match the extension of %r." % (fmt, out))
    else:
        fmt = fmt or "mp4"
        out = os.path.splitext(args.input)[0] + "." + fmt
    return fmt, out


def run(args):
    ffmpeg, ffprobe = find_tools()
    if not os.path.isfile(args.input):
        raise Gif2Mp4Error("input file not found: %s" % args.input)
    kind = sniff(args.input)
    if kind is None:
        raise Gif2Mp4Error("%s is not a GIF or WebP file." % args.input)
    fmt, out = resolve_output(args)
    crf = args.crf if args.crf is not None else DEFAULT_CRF[fmt]
    if crf > CRF_MAX[fmt]:
        raise Gif2Mp4Error("--crf %d is too high for %s (maximum %d)." % (crf, fmt, CRF_MAX[fmt]))
    if fmt == "mp4" and crf == 0:
        raise Gif2Mp4Error("--crf 0 is lossless H.264, which many players cannot play. Use 1 or higher.")
    if args.loops == 0 and args.max_seconds is None:
        raise Gif2Mp4Error("--loops 0 means 'repeat to fill', so it needs --max-seconds.")
    if os.path.abspath(out) == os.path.abspath(args.input):
        raise Gif2Mp4Error("the output would replace the input file.")
    if os.path.exists(out) and not args.overwrite:
        raise Gif2Mp4Error("%s already exists. Pass --overwrite to replace it." % out)
    if kind == "webp" and not check_decodes(ffmpeg, args.input):
        raise Gif2Mp4Error(
            "this ffmpeg cannot decode animated WebP (%s). Update ffmpeg, or convert the file "
            "to a GIF first." % args.input)

    info = probe(ffprobe, args.input)
    info["delays"] = normalize_delays(info["delays"], args.keep_short_delays)
    plan = plan_timing(info["delays"], args.loops, args.max_seconds)
    before = os.path.getsize(args.input)

    tmp = out + ".part"
    cmd = build_command(ffmpeg, args.input, tmp, info, plan, fmt, crf, args.bg, args.even)
    try:
        run_ffmpeg(cmd)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    after = os.path.getsize(out)

    res = probe_output(ffprobe, out)
    ew, eh = even_up(info["width"]), even_up(info["height"])
    print("input:  %s  %s, %d x %d, %d frames, %.2f s" % (
        args.input, kind.upper(), info["width"], info["height"],
        len(info["delays"]), float(plan["one_play"])))
    note = {"exact": "",
            "average": " (average of the GIF's delays, which are rounded to 1/100 s)",
            "fallback": " (the delays do not share a clean rate)"}[plan["kind"]]
    print("output: %s  %s, %d x %d, %s fps%s" % (
        out, "H.264" if fmt == "mp4" else "VP9", ew, eh, format_fps(plan["fps"]), note))
    if (ew, eh) != (info["width"], info["height"]):
        print("        odd size fixed by %s of 1 px" % ("padding" if args.even == "pad" else "scaling"))
    loops_txt = "%d play%s" % (plan["started"], "" if plan["started"] == 1 else "s")
    if plan["capped"]:
        loops_txt += ", cut at %s s by --max-seconds" % _trim(float(args.max_seconds))
    print("        %.2f s, %s" % (float(plan["seconds"]), loops_txt))
    ratio = after / before if before else 0
    print("size before: %s (%d bytes)" % (human_size(before), before))
    print("size after:  %s (%d bytes)" % (human_size(after), after))
    if before:
        if after <= before:
            print("             %.1f%% of the GIF, %.1f%% smaller" % (ratio * 100, (1 - ratio) * 100))
        else:
            print("             %.1f%% of the GIF, %.1f%% bigger" % (ratio * 100, (ratio - 1) * 100))
    if res and res["frames"] != plan["frames"]:
        print("warning: expected %d frames but the file has %d." % (plan["frames"], res["frames"]),
              file=sys.stderr)
    return 0


def _trim(x):
    return ("%.3f" % x).rstrip("0").rstrip(".")


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Gif2Mp4Error as exc:
        print("gif2mp4: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("gif2mp4: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
