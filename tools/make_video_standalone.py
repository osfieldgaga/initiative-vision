#!/usr/bin/env python3
"""Stitch a session's frames into an MP4 for quick review.

Standalone copy of src/ivision/frames/make_video.py for a machine that
doesn't have this project set up (e.g. a laptop you scp'd a session to) —
this one file has no dependency on the rest of the project, just Python's
standard library and ffmpeg. If you're already in the project, use the
real module instead (`python -m ivision.frames.make_video ...`); the two
should stay in sync, but this copy is the one to trust on a bare machine.

Not part of the acquisition pipeline — frame capture stays one file per
frame, per the Phase 1 brief (no video library, no codec/container output
while recording). This just turns an already-finished session into a video
for humans to watch.

Needs ffmpeg installed (not part of this project):
    macOS:            brew install ffmpeg
    Debian/Ubuntu/Pi: sudo apt install -y ffmpeg
    Windows:           https://ffmpeg.org/download.html

Usage, from wherever you scp'd the session folder to:
    python3 make_video_standalone.py <session_folder>
    python3 make_video_standalone.py <session_folder> --fps 30
    python3 make_video_standalone.py <session_folder> --out drive.mp4

By default the video is paced using each frame's REAL timestamp, not a
constant rate: a gap where frames were dropped shows up as a brief freeze
in the video instead of being silently smoothed over. Pass --fps to ignore
real timing and use a constant rate instead (smoother-looking, less honest).
"""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
from pathlib import Path

COLUMNS = ("seq", "filename", "timestamp_ns")  # the frames.csv header this project writes


def read_ledger(session_dir) -> list:
    """[(seq, relpath, timestamp_ns), ...] in seq order, read from frames.csv."""
    path = Path(session_dir) / "frames.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. If it's missing but the frames/ folder is still there, "
            f"every frame's filename is <seq>_<timestamp_ns>.jpg -- the ledger can be "
            f"rebuilt from those (see rebuild.py in the main project) before running this."
        )
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None or tuple(header) != COLUMNS:
            raise ValueError(f"{path} header is {header}, expected {list(COLUMNS)}")
        return [(int(seq), name, int(ts)) for seq, name, ts in reader]


def build_concat_file(rows: list, fps: float | None = None) -> str:
    """The ffmpeg concat-demuxer script: one file/duration pair per frame.

    Default (fps=None): each frame's duration is the real gap to the next
    frame's timestamp, so a drop (a bigger gap) holds that frame on screen
    longer instead of the loss being smoothed away. The very last frame has
    no "next" to measure against, so it reuses the gap from the frame
    before it (or 1/30s if there's only one frame total).

    fps given: every frame gets a constant 1/fps duration instead, ignoring
    real timing entirely.

    ffmpeg quirk (documented, not a bug here): the concat demuxer ignores
    the *last* entry's duration line. The standard workaround -- listing the
    final frame a second time, with no duration -- is what makes it actually
    hold on screen for its intended length instead of vanishing instantly.
    """
    if not rows:
        raise ValueError("no frames to stitch")
    lines = []
    for i, (seq, name, ts) in enumerate(rows):
        if fps:
            duration = 1.0 / fps
        elif i + 1 < len(rows):
            duration = (rows[i + 1][2] - ts) / 1e9
        elif len(rows) > 1:
            duration = (ts - rows[i - 1][2]) / 1e9
        else:
            duration = 1.0 / 30
        lines.append(f"file '{name}'")
        lines.append(f"duration {duration:.6f}")
    lines.append(f"file '{rows[-1][1]}'")  # repeated on purpose: see docstring above
    return "\n".join(lines) + "\n"


def run_ffmpeg(session_dir, concat_filename: str, out_path, crf: int = 20, preset: str = "veryfast") -> None:
    """Run ffmpeg with cwd=session_dir, so the concat file's relative frame
    paths (frames/<name>.jpg) resolve against the session, not the caller's cwd."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg is not installed -- see the install commands at the top of this file"
        )
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_filename,
        "-vsync", "vfr", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        str(out_path),
    ]
    subprocess.run(cmd, cwd=session_dir, check=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Stitch a session's frames into an MP4 for review.")
    parser.add_argument("session_dir")
    parser.add_argument("--out", default=None, help="output path (default: <session_dir>/preview.mp4)")
    parser.add_argument("--fps", type=float, default=None,
                        help="constant frame rate; default paces by each frame's real timestamp")
    parser.add_argument("--crf", type=int, default=20,
                        help="x264 quality: lower = better and bigger, 18-28 is the usual range")
    parser.add_argument("--preset", default="veryfast",
                        help="x264 speed/compression trade-off (ultrafast..medium..veryslow); "
                             "veryfast trades some compression efficiency for much faster encoding")
    args = parser.parse_args(argv)
    if args.fps is not None and args.fps <= 0:
        parser.error("--fps must be > 0")

    session_dir = Path(args.session_dir)
    out_path = Path(args.out).resolve() if args.out else (session_dir / "preview.mp4").resolve()
    rows = read_ledger(session_dir)
    print(f"{len(rows)} frames in the ledger")

    concat_path = session_dir / "_make_video_concat.txt"
    concat_path.write_text(build_concat_file(rows, fps=args.fps))
    try:
        run_ffmpeg(session_dir, concat_path.name, out_path, crf=args.crf, preset=args.preset)
    finally:
        concat_path.unlink(missing_ok=True)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
