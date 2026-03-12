#!/usr/bin/env python3
"""mlx-whisper transcription wrapper for whisper-workflow GUI.

Usage:
    python3 transcribe_mlx.py <input_audio> <output.txt>

Environment variables:
    WHISPER_LANGUAGE            Language code (default: ja)
    WHISPER_MLX_MODEL           HuggingFace model ID (default: mlx-community/whisper-large-v3-turbo)
    WHISPER_PREFLIGHT_NORMALIZE 1 to apply loudnorm via ffmpeg before transcription
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time


def log(msg: str) -> None:
    print(msg, flush=True)


def convert_to_wav(input_path: str, normalize: bool) -> str:
    """Convert input audio to 16kHz mono WAV via ffmpeg."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    wav_path = tmp.name

    cmd = ["ffmpeg", "-y", "-i", input_path]
    if normalize:
        cmd += ["-af", "loudnorm"]
    cmd += ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path]

    log("[1/3] Converting to 16kHz WAV...")
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0

    if result.returncode != 0:
        log(f"Error: ffmpeg conversion failed: {result.stderr.strip()}")
        sys.exit(1)

    log(f"Convert time: {elapsed:.1f}s")
    return wav_path


def get_audio_duration(path: str) -> float:
    """Get audio duration in seconds via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", path],
            capture_output=True, text=True,
        )
        return float(result.stdout.strip())
    except (ValueError, OSError):
        return 0.0


def transcribe(wav_path: str, model: str, language: str) -> str:
    """Run mlx-whisper transcription."""
    try:
        import mlx_whisper  # noqa: F811
    except ImportError:
        log("Error: mlx-whisper is not installed.")
        log("Install with: pip install mlx-whisper")
        sys.exit(1)

    log(f"[2/3] Transcribing with mlx-whisper...")
    log(f"Model: {model}")
    log(f"Language: {language}")

    t0 = time.time()
    result = mlx_whisper.transcribe(
        wav_path,
        path_or_hf_repo=model,
        language=language,
        verbose=False,
    )
    elapsed = time.time() - t0
    log(f"Transcribe time: {elapsed:.1f}s")

    text = result.get("text", "")
    return text.strip()


def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <input_audio> <output.txt>", file=sys.stderr)
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    if not os.path.isfile(input_path):
        log(f"Error: Input file not found: {input_path}")
        sys.exit(1)

    language = os.environ.get("WHISPER_LANGUAGE", "ja")
    model = os.environ.get("WHISPER_MLX_MODEL", "mlx-community/whisper-large-v3-turbo")
    normalize = os.environ.get("WHISPER_PREFLIGHT_NORMALIZE", "0") == "1"

    log("=== Whisper Transcription Workflow ===")
    log(f"Input: {input_path}")
    log(f"Output: {output_path}")
    log(f"Preset: turbo")
    log(f"Mode: single-pass")

    audio_duration = get_audio_duration(input_path)
    if audio_duration > 0:
        log(f"Audio duration: {audio_duration:.1f}s")

    script_start = time.time()

    wav_path = convert_to_wav(input_path, normalize)
    try:
        text = transcribe(wav_path, model, language)
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass

    log("[3/3] Writing output...")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text + "\n")

    total_elapsed = time.time() - script_start

    log("=== Performance Summary ===")
    log(f"Total time: {total_elapsed:.1f}s")
    if audio_duration > 0:
        rtf = total_elapsed / audio_duration
        speed = audio_duration / total_elapsed if total_elapsed > 0 else 0
        log(f"RTF (end-to-end): {rtf:.2f}")
        log(f"Speed (end-to-end): {speed:.1f}x realtime")
    log(f"Done! Output saved to: {output_path}")
    log("=== Completed Successfully ===")


if __name__ == "__main__":
    main()
