#!/bin/bash
# Retry only failed segmented chunks and merge recovered text into partial output.
#
# Usage:
#   ./retry_failed_segments.sh <input_audio_file> <partial_output_txt> [failed_segments_file] [recovered_output_txt]
#
# Notes:
# - Uses WHISPER_SEGMENT_TIME from env, or falls back to <partial_output_txt>.recovery_meta if present.
# - Keeps unresolved failures in <recovered_output_txt>.failed_segments.txt

set -euo pipefail

INPUT_FILE="${1:-}"
PARTIAL_OUTPUT="${2:-}"
FAILED_FILE="${3:-}"
RECOVERED_OUTPUT="${4:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR=""
SEGMENT_TIME_ENV="${WHISPER_SEGMENT_TIME:-}"
META_PRESET=""
META_LANGUAGE=""
META_MODEL=""

is_positive_int() {
    case "${1:-}" in
        ''|*[!0-9]*|0) return 1 ;;
        *) return 0 ;;
    esac
}

cleanup() {
    if [ -n "${WORK_DIR:-}" ] && [ -d "$WORK_DIR" ]; then
        rm -rf -- "$WORK_DIR"
    fi
}

usage() {
    echo "Usage: $0 <input_audio_file> <partial_output_txt> [failed_segments_file] [recovered_output_txt]"
}

trap cleanup EXIT INT TERM

if [ -z "$INPUT_FILE" ] || [ -z "$PARTIAL_OUTPUT" ]; then
    usage
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    echo "Error: input file not found: $INPUT_FILE"
    exit 1
fi

if [ ! -f "$PARTIAL_OUTPUT" ]; then
    echo "Error: partial output file not found: $PARTIAL_OUTPUT"
    exit 1
fi

if [ -z "$FAILED_FILE" ]; then
    FAILED_FILE="${PARTIAL_OUTPUT}.failed_segments.txt"
fi

if [ ! -f "$FAILED_FILE" ]; then
    echo "Error: failed segments file not found: $FAILED_FILE"
    exit 1
fi

if [ -z "$RECOVERED_OUTPUT" ]; then
    if [[ "$PARTIAL_OUTPUT" == *.txt ]]; then
        RECOVERED_OUTPUT="${PARTIAL_OUTPUT%.txt}.recovered.txt"
    else
        RECOVERED_OUTPUT="${PARTIAL_OUTPUT}.recovered.txt"
    fi
fi

SEGMENT_TIME="$SEGMENT_TIME_ENV"
if ! is_positive_int "$SEGMENT_TIME"; then
    META_FILE="${PARTIAL_OUTPUT}.recovery_meta"
    if [ -f "$META_FILE" ]; then
        seg_val="$(awk -F= '$1=="SEGMENT_TIME"{print $2}' "$META_FILE" | tail -n 1 | tr -d '[:space:]')"
        META_PRESET="$(awk -F= '$1=="PRESET"{print $2}' "$META_FILE" | tail -n 1 | tr -d '[:space:]')"
        META_LANGUAGE="$(awk -F= '$1=="LANGUAGE"{print $2}' "$META_FILE" | tail -n 1 | tr -d '[:space:]')"
        META_MODEL="$(awk '$0 ~ /^MODEL=/{print substr($0, 7)}' "$META_FILE" | tail -n 1)"
        if is_positive_int "$seg_val"; then
            SEGMENT_TIME="$seg_val"
        fi
    fi
fi

if ! is_positive_int "$SEGMENT_TIME"; then
    echo "Error: WHISPER_SEGMENT_TIME is not set and recovery meta was unavailable."
    echo "Set segment time used by the original run, e.g.: WHISPER_SEGMENT_TIME=120 $0 ..."
    exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Error: ffmpeg not found"
    exit 1
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/whisper_recover.XXXXXX")"
WAV_FILE="$WORK_DIR/audio.wav"
SEGMENTS_DIR="$WORK_DIR/segments"
RECOVER_DIR="$WORK_DIR/recovered"
MERGED_TMP="$WORK_DIR/merged.txt"
REMAINING_FILE="$WORK_DIR/remaining_failed.txt"

mkdir -p "$SEGMENTS_DIR" "$RECOVER_DIR"
: > "$REMAINING_FILE"

echo "[recover] input: $INPUT_FILE"
echo "[recover] partial output: $PARTIAL_OUTPUT"
echo "[recover] failed list: $FAILED_FILE"
echo "[recover] segment_time: ${SEGMENT_TIME}s"

echo "[recover] converting input to 16kHz wav..."
ffmpeg -hide_banner -loglevel error -i "$INPUT_FILE" -ar 16000 -ac 1 -c:a pcm_s16le -y "$WAV_FILE"

echo "[recover] regenerating segments..."
ffmpeg -hide_banner -loglevel error -i "$WAV_FILE" -f segment -segment_time "$SEGMENT_TIME" -c copy "$SEGMENTS_DIR/segment_%03d.wav"

recovered_count=0
failed_count=0

while IFS= read -r seg || [ -n "$seg" ]; do
    seg="$(printf '%s' "$seg" | tr -d '[:space:]')"
    [ -n "$seg" ] || continue
    if [[ ! "$seg" =~ ^segment_[0-9]+$ ]]; then
        continue
    fi

    seg_wav="$SEGMENTS_DIR/${seg}.wav"
    seg_txt="$RECOVER_DIR/${seg}.txt"
    seg_log="$RECOVER_DIR/${seg}.log"

    if [ ! -f "$seg_wav" ]; then
        echo "[recover] missing segment wav: $seg"
        echo "$seg" >> "$REMAINING_FILE"
        failed_count=$((failed_count + 1))
        continue
    fi

    echo "[recover] transcribing $seg ..."
    if (
        export WHISPER_MODE=single-pass
        export WHISPER_USE_VAD="${WHISPER_USE_VAD:-0}"
        export WHISPER_RETRY_COUNT="${WHISPER_RETRY_COUNT:-1}"
        if [ -z "${WHISPER_PRESET:-}" ]; then
            if [ -n "$META_PRESET" ]; then
                export WHISPER_PRESET="$META_PRESET"
            else
                export WHISPER_PRESET="x16"
            fi
        fi
        if [ -z "${WHISPER_LANGUAGE:-}" ] && [ -n "$META_LANGUAGE" ]; then
            export WHISPER_LANGUAGE="$META_LANGUAGE"
        fi
        if [ -z "${WHISPER_MODEL:-}" ] && [ -n "$META_MODEL" ] && [ -f "$META_MODEL" ]; then
            export WHISPER_MODEL="$META_MODEL"
        fi
        "$SCRIPT_DIR/transcribe_workflow.sh" "$seg_wav" "$seg_txt"
    ) > "$seg_log" 2>&1; then
        recovered_count=$((recovered_count + 1))
    else
        echo "[recover] failed: $seg"
        tail -n 30 "$seg_log" || true
        echo "$seg" >> "$REMAINING_FILE"
        failed_count=$((failed_count + 1))
    fi
done < "$FAILED_FILE"

echo "[recover] merging recovered segments into partial output..."
: > "$MERGED_TMP"
while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ ^\[SEGMENT_FAILED\]\ (segment_[0-9]+)$ ]]; then
        seg="${BASH_REMATCH[1]}"
        seg_txt="$RECOVER_DIR/${seg}.txt"
        if [ -f "$seg_txt" ] && [ -s "$seg_txt" ]; then
            cat "$seg_txt" >> "$MERGED_TMP"
        else
            printf '%s\n' "$line" >> "$MERGED_TMP"
        fi
    else
        printf '%s\n' "$line" >> "$MERGED_TMP"
    fi
done < "$PARTIAL_OUTPUT"

cp "$MERGED_TMP" "$RECOVERED_OUTPUT"

if [ -s "$REMAINING_FILE" ]; then
    sort -u "$REMAINING_FILE" > "${RECOVERED_OUTPUT}.failed_segments.txt"
else
    rm -f -- "${RECOVERED_OUTPUT}.failed_segments.txt"
fi

echo "[recover] recovered segments: $recovered_count"
echo "[recover] remaining failed segments: $failed_count"
echo "[recover] output: $RECOVERED_OUTPUT"
if [ -f "${RECOVERED_OUTPUT}.failed_segments.txt" ]; then
    echo "[recover] remaining list: ${RECOVERED_OUTPUT}.failed_segments.txt"
fi
