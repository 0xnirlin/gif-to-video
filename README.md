# gif2mp4

Convert a GIF to an MP4 or WebM that plays everywhere, with ffmpeg. One Python file, no dependencies.

```
gif2mp4 cat.gif
gif2mp4 cat.gif --loops 3 --max-seconds 10
gif2mp4 logo.gif --bg '#101010' --format webm -o logo.webm
```

If you searched for "gif to mp4", "convert gif to video" or "ffmpeg gif to mp4 not playing", this is the command you were trying to remember.

## What it does

- Keeps the timing. GIF delays are counted in hundredths of a second. gif2mp4 reads every delay and picks one steady frame rate that matches them: 10 fps for 10 cs frames, 25 fps for 4 cs frames, 15 fps for the 6 cs and 7 cs mix a 15 fps GIF is really stored as.
- Makes H.264 happy: even width and height (it pads by 1 px, or scales by 1 px with `--even scale`), `yuv420p`, BT.709 color tags and `faststart`, so the video starts playing before it has fully downloaded.
- Flattens see-through areas onto a color you choose (`--bg`, default white), because MP4 and VP9 here have no transparency.
- Plays the GIF N times with `--loops`, and never goes over a total length you set with `--max-seconds`.
- Writes `mp4` (H.264) or `webm` (VP9), with `--crf` for quality.
- Prints the file size before and after.
- Runs on your machine. Nothing is uploaded.

## Install

You need Python 3.8 or newer and ffmpeg (with ffprobe, libx264 and libvpx-vp9) on your PATH.

```
# ffmpeg
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Debian, Ubuntu
winget install ffmpeg          # Windows

# gif2mp4
pipx install git+https://github.com/0xnirlin/gif-to-video
```

Or skip the install. The tool is the single file `gif2mp4.py`. Clone the repo and run it directly:

```
git clone https://github.com/0xnirlin/gif-to-video
cd gif-to-video
./gif2mp4 in.gif
python3 gif2mp4.py in.gif
```

## Examples

```
# Default: in.mp4 next to the GIF, H.264, CRF 23, plays once
gif2mp4 in.gif

# WebM instead (the format comes from the extension)
gif2mp4 in.gif -o in.webm

# Smaller file, lower quality
gif2mp4 in.gif --crf 28

# Play the GIF 5 times, but stop at 10 seconds
gif2mp4 in.gif --loops 5 --max-seconds 10

# Repeat the GIF until it fills exactly 30 seconds
gif2mp4 in.gif --loops 0 --max-seconds 30

# Transparent GIF on a dark page
gif2mp4 sticker.gif --bg '#111111'

# Replace an existing file
gif2mp4 in.gif -o in.mp4 --overwrite
```

Example output (the 3 second test GIF from the table below, played three times and cut at 8 seconds):

```
input:  mandel.gif  GIF, 320 x 180, 45 frames, 3.00 s
output: m3.mp4  H.264, 320 x 180, 15 fps (average of the GIF's delays, which are rounded to 1/100 s)
        8.00 s, 3 plays, cut at 8 s by --max-seconds
size before: 1.28 MB (1343854 bytes)
size after:  288.3 KB (295186 bytes)
             22.0% of the GIF, 78.0% smaller
```

To show the video on a web page the way a GIF behaves:

```html
<video autoplay loop muted playsinline src="in.mp4"></video>
```

### All options

| Option | Meaning |
| --- | --- |
| `-o, --output FILE` | Output file. Default is the input name with `.mp4` or `.webm`. |
| `--format mp4\|webm` | `mp4` is H.264, `webm` is VP9. Default: from the `-o` extension, otherwise `mp4`. |
| `--crf N` | Quality. Lower is better and bigger. Default 23 for mp4 (1 to 51) and 32 for webm (0 to 63). |
| `--bg COLOR` | Color shown where the GIF is see-through. A name (`white`, `black`) or hex like `#ff8800`. Default `white`. |
| `--loops N` | Play the GIF N times. Default 1. `0` repeats it until `--max-seconds` is full. |
| `--max-seconds S` | Never make a video longer than S seconds. |
| `--even pad\|scale` | How to fix an odd width or height. `pad` adds a 1 px strip of the background color (default). `scale` stretches the picture up by 1 px. |
| `--keep-short-delays` | Keep frame delays under 2 cs as written. By default they play as 10 cs. |
| `--overwrite` | Replace the output file if it exists. |

Exit codes: 0 on success, 1 for an error (missing file, no ffmpeg, ffmpeg failed, output exists), 2 for bad options. The output is written to `<name>.part` first and renamed when ffmpeg finishes, so a failed run never leaves a broken video.

## Why GIF to MP4

A GIF stores every frame as a separate picture, limited to 256 colors. MP4 and WebM store only what changes between frames. For anything with smooth movement the video is a fraction of the size, and phones, editors and players all handle it.

These numbers are measured, not estimated. Three synthetic 3 second clips, 320x180, 15 fps, made with ffmpeg 8.0.1 and turned into GIFs with a palette built from the clip itself (the good way to make a GIF):

```
ffmpeg -f lavfi -i "testsrc2=size=320x180:rate=15:duration=3" -vf "split[a][b];[a]palettegen[p];[b][p]paletteuse" testsrc.gif
ffmpeg -f lavfi -i "gradients=size=320x180:rate=15:duration=3:speed=0.02" -vf "split[a][b];[a]palettegen[p];[b][p]paletteuse" gradients.gif
ffmpeg -f lavfi -i "mandelbrot=size=320x180:rate=15:end_scale=0.05,trim=duration=3" -vf "split[a][b];[a]palettegen[p];[b][p]paletteuse" mandel.gif
```

`testsrc2` is a busy test pattern with sharp text. `gradients` is a slow, smooth color blend. `mandelbrot` is a zoom with a lot of fine detail. Sizes are in KB (1024 bytes).

| Clip | GIF | MP4 (default, CRF 23) | WebM (default, CRF 32) | MP4 `--crf 28` |
| --- | --- | --- | --- | --- |
| testsrc2 | 264.1 KB | 63.9 KB (24.2%) | 62.7 KB (23.7%) | 41.7 KB (15.8%) |
| gradients | 340.7 KB | 6.3 KB (1.9%) | 2.4 KB (0.7%) | 4.7 KB (1.4%) |
| mandelbrot | 1312.4 KB | 111.6 KB (8.5%) | 118.5 KB (9.0%) | 56.8 KB (4.3%) |

Percentages are of the GIF's size.

What to read from this:

- The video was between about 1 percent and 24 percent of the GIF's size at the default settings. The smooth clip, which is the worst case for GIF, shrank the most.
- MP4 and WebM came out close. WebM was smaller on two clips and bigger on the mandelbrot clip. Try both on your file.
- Your footage will behave differently. These clips are synthetic. Real screen recordings, cartoons and photos each land somewhere else, and a GIF made with a poor palette is already small and ugly. Run your own file, and use `--crf` to trade quality for size.
- Video is lossy. GIF is lossless inside its 256 colors, so the video is not pixel for pixel the same. For most clips you will not see it, and `--crf 18` gets closer if you do.

## Why a plain `ffmpeg -i in.gif out.mp4` can go wrong

Checked with ffmpeg 8.0.1 on small test GIFs:

- **Wrong pixel format.** On a GIF it chose `yuv444p` and the High 4:4:4 Predictive profile. That is a file many players and phones do not accept. gif2mp4 forces `yuv420p` and the High profile.
- **Odd sizes.** A 65x47 GIF converted to `yuv420p` fails with "width not divisible by 2" and writes no usable file. gif2mp4 pads to 66x48.
- **Slow start.** The plain MP4 had its index (`moov`) at the end of the file, after the video data. gif2mp4 moves it to the front with `faststart`, so playback can start before the download finishes.
- **Frame rate.** GIF delays are whole hundredths of a second, so a 15 fps GIF is stored as 6 cs and 7 cs frames. gif2mp4 reads every delay and picks one steady rate: the exact step when the delays share one, the average when they only differ by rounding, and 50 fps when they do neither (that keeps every frame and the total length).
- **Transparency.** MP4 and WebM (VP9, yuv420p) have no see-through pixels, so gif2mp4 paints them onto your `--bg` color on purpose, instead of leaving it to chance.
- **Color.** The video is tagged BT.709, so players do not have to guess the color matrix.

## FAQ

**Does it keep the sound?**
GIFs have no sound, so the video has none.

**My GIF has a loop count. Is it kept?**
No. A video has no loop flag, so the GIF's own setting is ignored. You choose with `--loops`. To loop forever on a web page, use the `loop` attribute on the `<video>` tag (see Examples).

**Can I get transparency in the video?**
No. Both outputs are flattened onto `--bg`. If you need transparency, keep the GIF or use an animated format with an alpha channel.

**Why is my video 50 fps?**
The GIF's frame delays do not share a common step and are not close to equal, for example 3 cs and 7 cs mixed. 50 fps keeps every frame and the exact total length, with a small timing error of up to one hundredth of a second on some frames. The output line tells you when this happens.

**What happens with a 0 or 1 cs delay?**
Those usually mean "as fast as you can". gif2mp4 plays them as 10 cs, the way GIF viewers commonly do. Use `--keep-short-delays` to leave them alone.

**Does it work on animated WebP?**
Only if your ffmpeg can decode animated WebP. The ffmpeg 8.0.1 build used to write this (Homebrew, macOS) could not: it fails with "image data not found". In that case gif2mp4 stops with a clear message and writes nothing. If your build can read it, the same command works on the `.webp` file. Check with `ffmpeg -i in.webp -f null -`.

**Why `--crf 0` is refused for mp4?**
CRF 0 is lossless H.264, which switches to a profile many players cannot play. Use 1 or higher.

**It says ffmpeg is not found.**
Install ffmpeg (see Install) and open a new terminal so the PATH updates. Check with `ffmpeg -version` and `ffprobe -version`.

**Which Python?**
3.8 or newer. There are no third-party packages.

## Prefer a browser?

If you do not want to install anything, there is [a free browser version](https://getziztu.com/tools/gif-to-video?utm_source=github&utm_medium=readme&utm_campaign=gif-to-video). It needs no account, takes a GIF of up to 50 MB, plays it 1 to 10 times in an MP4 of at most 60 seconds, and allows 10 conversions an hour.

## Development

```
python3 -m unittest discover -s tests -v
```

The tests build tiny GIFs with ffmpeg (`lavfi` sources, 64x48 pixels) and check the output duration, even dimensions, codec, frame rate, loop count, faststart, transparency and the timing of uneven delays. They need ffmpeg on your PATH. The animated WebP test is skipped when `gif2webp` is not installed.

## License

MIT. Copyright (c) 2026 Ziztu.
