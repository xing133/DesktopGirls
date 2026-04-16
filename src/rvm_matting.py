#!/usr/bin/env python3
from __future__ import annotations

"""
Use RobustVideoMatting (RVM) to extract a person from an MP4 into a transparent
PNG sequence.

Notes:
- The upstream RVM converter defaults to `output_type="video"`.
- This script defaults to PNG sequence output because the existing desktop
  player already consumes `frame_*.png`, and plain MP4 is not a portable alpha
  container.
- If you already have a local clone of RVM, pass `--rvm-repo` to avoid a
  runtime GitHub fetch.
"""

import argparse
import json
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Iterator

import numpy as np
import torch
from PIL import Image
from torchvision.transforms.functional import to_tensor

LANCZOS = getattr(Image, "Resampling", Image).LANCZOS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run RobustVideoMatting on an MP4 and export transparent RGBA PNG "
            "frames for the existing desktop-dancer player."
        )
    )
    parser.add_argument("--input", required=True, help="Input MP4 file")
    parser.add_argument(
        "--frames-dir",
        required=True,
        help="Output directory. PNG frames and metadata.json are written here.",
    )
    parser.add_argument(
        "--variant",
        default="mobilenetv3",
        choices=["mobilenetv3", "resnet50"],
        help="RVM backbone. mobilenetv3 is faster, resnet50 is heavier.",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cpu", "cuda"],
        help="Inference device. auto prefers CUDA when available.",
    )
    parser.add_argument(
        "--rvm-repo",
        default=None,
        help="Optional local path to a RobustVideoMatting checkout.",
    )
    parser.add_argument(
        "--checkpoint",
        default=None,
        help=(
            "Optional RVM checkpoint path. If omitted, torch.hub loads the "
            "pretrained checkpoint."
        ),
    )
    parser.add_argument(
        "--display-height",
        default=None,
        type=int,
        help=(
            "Optional output frame height in pixels. Width keeps aspect ratio. "
            "By default the source resolution is preserved."
        ),
    )
    parser.add_argument(
        "--downsample-ratio",
        default=None,
        type=float,
        help=(
            "RVM downsample ratio. If omitted, it follows the upstream auto "
            "rule: largest side down to about 512px."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing frame_*.png and metadata.json in frames-dir.",
    )
    parser.add_argument(
        "--save-alpha-dir",
        default=None,
        help="Optional directory for grayscale alpha preview PNGs.",
    )
    parser.add_argument(
        "--save-foreground-dir",
        default=None,
        help="Optional directory for RGB foreground preview PNGs.",
    )
    return parser.parse_args()


def require_binary(name: str) -> str:
    binary = shutil.which(name)
    if binary is None:
        raise SystemExit(
            f"Missing required binary '{name}'. Install ffmpeg/ffprobe and keep "
            "them on PATH."
        )
    return binary


def probe_video(ffprobe_bin: str, video_path: Path) -> tuple[float, int, int, int]:
    out = subprocess.check_output(
        [
            ffprobe_bin,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            str(video_path),
        ]
    )
    data = json.loads(out)
    video_stream = next(
        stream for stream in data["streams"] if stream["codec_type"] == "video"
    )

    fps = float(Fraction(video_stream["r_frame_rate"]))
    width = int(video_stream["width"])
    height = int(video_stream["height"])
    frame_count = int(
        video_stream.get("nb_frames")
        or round(float(video_stream["duration"]) * fps)
    )
    return fps, frame_count, width, height


def iter_raw_frames(
    ffmpeg_bin: str,
    video_path: Path,
    width: int,
    height: int,
) -> Iterator[np.ndarray]:
    frame_bytes = width * height * 3
    proc = subprocess.Popen(
        [
            ffmpeg_bin,
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    try:
        if proc.stdout is None:
            raise RuntimeError("ffmpeg stdout pipe is unavailable")

        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            yield np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait()
        if proc.returncode not in (0, None):
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")


def auto_downsample_ratio(height: int, width: int) -> float:
    return min(512 / max(height, width), 1.0)


def choose_device(requested: str) -> str:
    if requested == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if requested == "cuda" and not torch.cuda.is_available():
        raise SystemExit("CUDA requested but torch.cuda.is_available() is false.")
    return requested


def compute_output_size(
    source_width: int,
    source_height: int,
    display_height: int | None,
) -> tuple[int, int]:
    if display_height is None:
        return source_width, source_height
    if display_height <= 0:
        raise SystemExit("--display-height must be a positive integer.")
    display_width = int(round(source_width / source_height * display_height))
    return display_width, display_height


def ensure_clean_output_dir(frames_dir: Path, overwrite: bool) -> None:
    existing_frames = sorted(frames_dir.glob("frame_*.png"))
    metadata_path = frames_dir / "metadata.json"

    if (existing_frames or metadata_path.exists()) and not overwrite:
        raise SystemExit(
            f"'{frames_dir}' already contains generated output. Use --overwrite "
            "to replace it."
        )

    frames_dir.mkdir(parents=True, exist_ok=True)
    if not overwrite:
        return

    for path in existing_frames:
        path.unlink()
    if metadata_path.exists():
        metadata_path.unlink()


def load_model(
    variant: str,
    device: str,
    repo_path: Path | None,
    checkpoint_path: Path | None,
) -> torch.nn.Module:
    source = "local" if repo_path is not None else "github"
    load_target = str(repo_path) if repo_path is not None else "PeterL1n/RobustVideoMatting"
    load_kwargs = {
        "source": source,
        "pretrained": checkpoint_path is None,
    }
    if source == "github":
        load_kwargs["trust_repo"] = True

    model = torch.hub.load(load_target, variant, **load_kwargs)
    if checkpoint_path is not None:
        state_dict = torch.load(str(checkpoint_path), map_location="cpu")
        model.load_state_dict(state_dict)
    return model.eval().to(device)


def save_image(image_tensor: torch.Tensor, output_path: Path) -> None:
    image = image_tensor.clamp(0, 1).mul(255).byte().permute(1, 2, 0).cpu().numpy()
    Image.fromarray(image).save(output_path)


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    frames_dir = Path(args.frames_dir).expanduser().resolve()
    repo_path = Path(args.rvm_repo).expanduser().resolve() if args.rvm_repo else None
    checkpoint_path = (
        Path(args.checkpoint).expanduser().resolve() if args.checkpoint else None
    )
    alpha_dir = (
        Path(args.save_alpha_dir).expanduser().resolve()
        if args.save_alpha_dir
        else None
    )
    foreground_dir = (
        Path(args.save_foreground_dir).expanduser().resolve()
        if args.save_foreground_dir
        else None
    )

    if not input_path.is_file():
        raise SystemExit(f"Input video not found: {input_path}")
    if repo_path is not None and not repo_path.is_dir():
        raise SystemExit(f"Local RVM repo not found: {repo_path}")
    if checkpoint_path is not None and not checkpoint_path.is_file():
        raise SystemExit(f"Checkpoint not found: {checkpoint_path}")
    if args.downsample_ratio is not None and not (0 < args.downsample_ratio <= 1):
        raise SystemExit("--downsample-ratio must be in the range (0, 1].")

    ffmpeg_bin = require_binary("ffmpeg")
    ffprobe_bin = require_binary("ffprobe")
    ensure_clean_output_dir(frames_dir, overwrite=args.overwrite)
    if alpha_dir is not None:
        alpha_dir.mkdir(parents=True, exist_ok=True)
    if foreground_dir is not None:
        foreground_dir.mkdir(parents=True, exist_ok=True)

    fps, frame_count, source_width, source_height = probe_video(
        ffprobe_bin, input_path
    )
    output_width, output_height = compute_output_size(
        source_width, source_height, args.display_height
    )
    downsample_ratio = args.downsample_ratio or auto_downsample_ratio(
        output_height, output_width
    )
    device = choose_device(args.device)

    print(f"Input: {input_path}")
    print(
        f"Source: {source_width}x{source_height} @ {fps:.3f}fps, "
        f"{frame_count} frames"
    )
    print(
        f"Output PNG sequence: {output_width}x{output_height}, "
        f"downsample_ratio={downsample_ratio:.4f}, device={device}"
    )
    print("Upstream RVM default output_type is 'video'; this wrapper uses PNG.")

    try:
        model = load_model(
            variant=args.variant,
            device=device,
            repo_path=repo_path,
            checkpoint_path=checkpoint_path,
        )
    except Exception as exc:
        hint = (
            "Model load failed. If this machine cannot reach GitHub, clone "
            "RobustVideoMatting locally and pass --rvm-repo /path/to/repo."
        )
        if checkpoint_path is not None:
            hint += " You can also pass --checkpoint /path/to/rvm_*.pth."
        raise SystemExit(f"{hint}\nOriginal error: {exc}") from exc

    rec = [None] * 4
    processed = 0

    with torch.inference_mode():
        for index, frame_np in enumerate(
            iter_raw_frames(ffmpeg_bin, input_path, source_width, source_height),
            start=1,
        ):
            image = Image.fromarray(frame_np)
            if (output_width, output_height) != (source_width, source_height):
                image = image.resize((output_width, output_height), LANCZOS)

            src = to_tensor(image).unsqueeze(0).unsqueeze(0).to(device)
            fgr, pha, *rec = model(src, *rec, downsample_ratio)

            # Follow the upstream png_sequence path: save RGBA, with RGB masked
            # by non-zero alpha to reduce edge bleed in premultiplied viewers.
            foreground = (fgr[0, 0] * pha[0, 0].gt(0)).cpu()
            alpha = pha[0, 0].cpu()
            rgba = torch.cat([foreground, alpha], dim=0)

            save_image(rgba, frames_dir / f"frame_{index:04d}.png")

            if foreground_dir is not None:
                save_image(
                    foreground,
                    foreground_dir / f"foreground_{index:04d}.png",
                )
            if alpha_dir is not None:
                alpha_image = alpha.repeat(3, 1, 1)
                save_image(
                    alpha_image,
                    alpha_dir / f"alpha_{index:04d}.png",
                )

            processed = index
            if processed % 10 == 0 or processed == frame_count:
                print(f"  [{processed:4d}/{frame_count}] {processed / frame_count * 100:5.1f}%")

    if processed == 0:
        raise SystemExit("No frames were decoded from the input video.")

    metadata = {
        "fps": fps,
        "frame_count": processed,
        "width": output_width,
        "height": output_height,
        "source_video": str(input_path),
        "matting_backend": "RobustVideoMatting",
        "rvm_variant": args.variant,
        "rvm_checkpoint": str(checkpoint_path) if checkpoint_path else "torch.hub pretrained",
        "rvm_official_default_output_type": "video",
        "output_type": "png_sequence",
        "device": device,
        "downsample_ratio": downsample_ratio,
    }
    metadata_path = frames_dir / "metadata.json"
    with metadata_path.open("w", encoding="utf-8") as file:
        json.dump(metadata, file, indent=2, ensure_ascii=False)

    print(f"Done. Wrote {processed} RGBA PNG frames to {frames_dir}")
    print(f"Metadata: {metadata_path}")


if __name__ == "__main__":
    main()
