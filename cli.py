#!/usr/bin/env python3
"""HRC Studio Stem command line — the same engine the web UI uses.

    python cli.py song.mp3 --mode 2stem
    python cli.py album/*.flac --mode 6stem --out ./stems
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

from stemkit import __version__, audio
from stemkit.config import DEFAULT_MODE, MODES, MODEL_DIR, get_mode
from stemkit.separator import separate_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stem",
        description="Studio-quality audio stem extraction (lossless 24-bit WAV).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python cli.py track.mp3                          # 2stem: Vocals + Instrumental
  python cli.py track.flac --mode 6stem            # Vocals/Drums/Bass/Guitar/Piano/Other
  python cli.py *.wav --mode 2stem --out ./stems   # batch, one folder per file
""",
    )
    parser.add_argument("inputs", nargs="+", metavar="FILE", help="audio file(s) to separate")
    parser.add_argument(
        "-m",
        "--mode",
        choices=sorted(MODES),
        default=DEFAULT_MODE,
        help=f"separation mode (default: {DEFAULT_MODE})",
    )
    parser.add_argument(
        "-o",
        "--out",
        default="separated",
        metavar="DIR",
        help="output directory (default: ./separated)",
    )
    parser.add_argument("--models-dir", default=str(MODEL_DIR), help="model cache directory")
    parser.add_argument(
        "--single-stem",
        metavar="NAME",
        help="keep only one stem, e.g. Vocals (others are discarded)",
    )
    parser.add_argument("--json", action="store_true", help="print a JSON manifest instead of a table")
    parser.add_argument("--list-modes", action="store_true", help="show modes and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="log audio-separator internals")
    parser.add_argument("--version", action="version", version=f"stem {__version__}")
    return parser


def print_modes() -> None:
    print("\nAvailable modes:\n")
    for cfg in MODES.values():
        print(f"  {cfg['id']:<7} {cfg['name']:<8} {cfg['model']}")
        print(f"          stems: {', '.join(cfg['stems'])}\n")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.list_modes:
        print_modes()
        return 0

    if not audio.have_ffmpeg():
        print("error: ffmpeg is required but was not found on PATH.", file=sys.stderr)
        print("  macOS:  brew install ffmpeg", file=sys.stderr)
        print("  Debian: sudo apt install ffmpeg", file=sys.stderr)
        return 2

    cfg = get_mode(args.mode)
    out_root = Path(args.out).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    missing = [p for p in args.inputs if not Path(p).exists()]
    if missing:
        for path in missing:
            print(f"error: no such file: {path}", file=sys.stderr)
        return 2

    print(f"HRC Studio Stem {__version__} · mode={args.mode} · model={cfg['model']}")
    print(f"output: {out_root}\n")

    manifests: list[dict] = []
    failures = 0

    for index, raw in enumerate(args.inputs, start=1):
        source = Path(raw)
        label = f"[{index}/{len(args.inputs)}] {source.name}"
        print(f"{label}  ({audio.human_duration(audio.probe(source).get('duration'))})")

        job_dir = out_root / source.stem
        job_dir.mkdir(parents=True, exist_ok=True)

        last_line = ""
        started = time.monotonic()

        def on_stage(stage_name: str, fraction: float | None) -> None:
            nonlocal last_line
            if fraction is None:
                line = f"   {stage_name}…"
            else:
                width = 26
                filled = int(round(fraction * width))
                line = f"   {stage_name:<20} [{'#' * filled}{'.' * (width - filled)}] {fraction * 100:5.1f}%"
            if line != last_line:
                sys.stdout.write("\r" + line.ljust(76))
                sys.stdout.flush()
                last_line = line

        try:
            staged = Path(tempfile.mkdtemp(prefix="stem-cli-")) / source.name
            shutil.copy2(source, staged)
            result = separate_file(staged, args.mode, job_dir, on_stage=on_stage)
            shutil.rmtree(staged.parent, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 - reported per file
            failures += 1
            sys.stdout.write("\r" + " " * 76 + "\r")
            print(f"   failed: {exc}\n", file=sys.stderr)
            continue

        sys.stdout.write("\r" + " " * 76 + "\r")
        elapsed = time.monotonic() - started

        stems = result["stems"]
        if args.single_stem:
            wanted = args.single_stem.lower()
            stems = [s for s in stems if wanted in s["name"].lower()]
            if not stems:
                print(f"   no stem matched {args.single_stem!r}", file=sys.stderr)
                failures += 1
                continue

        for stem in stems:
            print(
                f"   ✓ {stem['name']:<13} {stem['bit_depth']}-bit "
                f"{stem['sample_rate'] / 1000:.1f}kHz  {audio.human_size(stem['bytes']):>9}  "
                f"{stem['filename']}"
            )
        print(f"   done in {elapsed:.1f}s → {job_dir}\n")

        manifests.append({**result, "source": str(source), "output_dir": str(job_dir)})

    if args.json:
        print(json.dumps(manifests, indent=2))

    print(f"finished: {len(manifests)} ok, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
