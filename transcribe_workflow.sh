#!/bin/bash
# whisper-workflow: Automated Transcription Script
# Usage: ./transcribe_workflow.sh <input_audio_file> [output_text_file]

set -euo pipefail

INPUT_FILE="${1:-}"
OUTPUT_FILE="${2:-transcription_result.txt}"
WORK_DIR=""
SCRIPT_START_SEC=$SECONDS

LANGUAGE="${WHISPER_LANGUAGE:-ja}"
PRESET="${WHISPER_PRESET:-x1}"              # x1 | x4 | x8 | x16 | custom
PROFILE="${WHISPER_PROFILE:-balanced}"      # legacy mode switch (used when PRESET=custom)
CPU_THREADS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
MODEL="${WHISPER_MODEL:-}"
MODE="segmented"
ACCURACY_HINT=""
AUDIO_DURATION_SEC=""
CONVERT_DURATION_SEC=0
TRANSCRIBE_DURATION_SEC=0
CONCAT_DURATION_SEC=0

# Legacy defaults (used when PRESET=custom and segmented mode).
JOBS="${WHISPER_JOBS:-4}"
THREADS="${WHISPER_THREADS:-4}"
SEGMENT_TIME="${WHISPER_SEGMENT_TIME:-60}"

is_positive_int() {
    case "${1:-}" in
        ''|*[!0-9]*|0) return 1 ;;
        *) return 0 ;;
    esac
}

cleanup() {
    if [ -n "${WORK_DIR:-}" ] && [ "$WORK_DIR" != "/" ] && [ -d "$WORK_DIR" ]; then
        echo "Cleaning up intermediate files..."
        rm -rf -- "$WORK_DIR"
    fi
}

run_whisper_single_pass() {
    local input_wav="$1"
    local err_file="$WORK_DIR/whisper_single.err"
    local result=""
    local rc=0

    set +e
    result="$(whisper-cli -l "$LANGUAGE" -m "$MODEL" -t "$THREADS" -nt --no-prints "$input_wav" 2>"$err_file")"
    rc=$?
    set -e

    if [ "$rc" -ne 0 ]; then
        echo "Warning: whisper-cli GPU path failed. Retrying with CPU (-ng)..."
        set +e
        result="$(whisper-cli -ng -l "$LANGUAGE" -m "$MODEL" -t "$THREADS" -nt --no-prints "$input_wav" 2>>"$err_file")"
        rc=$?
        set -e
    fi

    if [ "$rc" -ne 0 ]; then
        echo "Error: whisper-cli failed."
        echo "---- whisper stderr (last 80 lines) ----"
        tail -n 80 "$err_file" || true
        return "$rc"
    fi

    printf "%s\n" "$result"
}

get_audio_duration_sec() {
    if command -v ffprobe >/dev/null 2>&1; then
        AUDIO_DURATION_SEC="$(
            ffprobe -v error \
                -show_entries format=duration \
                -of default=nokey=1:noprint_wrappers=1 \
                "$WAV_FILE" 2>/dev/null || true
        )"
    fi
}

print_performance_summary() {
    local total_duration_sec="$1"
    echo "=== Performance Summary ==="
    echo "Convert time: ${CONVERT_DURATION_SEC}s"
    echo "Transcribe time: ${TRANSCRIBE_DURATION_SEC}s"
    echo "Concat time: ${CONCAT_DURATION_SEC}s"
    echo "Total time: ${total_duration_sec}s"

    if [ -n "$AUDIO_DURATION_SEC" ]; then
        echo "Audio duration: ${AUDIO_DURATION_SEC}s"
        local transcribe_rtf
        transcribe_rtf="$(awk -v t="$TRANSCRIBE_DURATION_SEC" -v a="$AUDIO_DURATION_SEC" 'BEGIN { if (a > 0) printf "%.3f", t / a; else print "n/a" }')"
        local total_rtf
        total_rtf="$(awk -v t="$total_duration_sec" -v a="$AUDIO_DURATION_SEC" 'BEGIN { if (a > 0) printf "%.3f", t / a; else print "n/a" }')"
        local transcribe_x
        transcribe_x="$(awk -v t="$TRANSCRIBE_DURATION_SEC" -v a="$AUDIO_DURATION_SEC" 'BEGIN { if (t > 0) printf "%.2f", a / t; else print "n/a" }')"
        local total_x
        total_x="$(awk -v t="$total_duration_sec" -v a="$AUDIO_DURATION_SEC" 'BEGIN { if (t > 0) printf "%.2f", a / t; else print "n/a" }')"
        echo "RTF (transcribe only): $transcribe_rtf"
        echo "RTF (end-to-end): $total_rtf"
        echo "Speed (transcribe only): ${transcribe_x}x realtime"
        echo "Speed (end-to-end): ${total_x}x realtime"
    fi
}

apply_preset() {
    case "$PRESET" in
        x1)
            MODE="single-pass"
            [ -n "$MODEL" ] || MODEL="$HOME/.cache/whisper-cpp/ggml-large-v3.bin"
            THREADS="${WHISPER_THREADS:-$CPU_THREADS}"
            JOBS="${WHISPER_JOBS:-1}"
            SEGMENT_TIME="${WHISPER_SEGMENT_TIME:-240}"
            ACCURACY_HINT="best (highest)"
            ;;
        x4)
            MODE="single-pass"
            [ -n "$MODEL" ] || MODEL="$HOME/.cache/whisper-cpp/ggml-medium.bin"
            THREADS="${WHISPER_THREADS:-$CPU_THREADS}"
            JOBS="${WHISPER_JOBS:-1}"
            SEGMENT_TIME="${WHISPER_SEGMENT_TIME:-240}"
            ACCURACY_HINT="high"
            ;;
        x8)
            MODE="single-pass"
            [ -n "$MODEL" ] || MODEL="$HOME/.cache/whisper-cpp/ggml-small.bin"
            THREADS="${WHISPER_THREADS:-$CPU_THREADS}"
            JOBS="${WHISPER_JOBS:-1}"
            SEGMENT_TIME="${WHISPER_SEGMENT_TIME:-240}"
            ACCURACY_HINT="medium"
            ;;
        x16)
            MODE="single-pass"
            [ -n "$MODEL" ] || MODEL="$HOME/.cache/whisper-cpp/ggml-tiny.bin"
            THREADS="${WHISPER_THREADS:-$CPU_THREADS}"
            JOBS="${WHISPER_JOBS:-1}"
            SEGMENT_TIME="${WHISPER_SEGMENT_TIME:-240}"
            ACCURACY_HINT="low-medium"
            ;;
        custom|"")
            [ -n "$MODEL" ] || MODEL="$HOME/.cache/whisper-cpp/ggml-large-v3.bin"
            ACCURACY_HINT="depends on model/profile"

            # Backward-compatible behavior when user selects custom mode.
            if [ "$PROFILE" = "fast-accurate" ]; then
                MODE="single-pass"
                JOBS="${WHISPER_JOBS:-1}"
                THREADS="${WHISPER_THREADS:-$CPU_THREADS}"
                SEGMENT_TIME="${WHISPER_SEGMENT_TIME:-240}"
            fi
            ;;
        *)
            echo "Error: Unsupported WHISPER_PRESET '$PRESET' (use x1, x4, x8, x16, custom)"
            exit 1
            ;;
    esac
}

trap cleanup EXIT INT TERM
apply_preset

if ! is_positive_int "$JOBS"; then
    echo "Error: WHISPER_JOBS must be a positive integer: '$JOBS'"
    exit 1
fi

if ! is_positive_int "$THREADS"; then
    echo "Error: WHISPER_THREADS must be a positive integer: '$THREADS'"
    exit 1
fi

if ! is_positive_int "$SEGMENT_TIME"; then
    echo "Error: WHISPER_SEGMENT_TIME must be a positive integer: '$SEGMENT_TIME'"
    exit 1
fi

# --- Validation ---
if [ -z "$INPUT_FILE" ]; then
    echo "Usage: $0 <input_audio_file> [output_text_file]"
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    echo "Error: Input file '$INPUT_FILE' not found."
    exit 1
fi

if [ ! -f "$MODEL" ]; then
    model_name="$(basename "$MODEL")"
    echo "Error: Model file not found at $MODEL"
    echo "Download example:"
    echo "  mkdir -p ~/.cache/whisper-cpp"
    echo "  curl -L -o \"$MODEL\" \"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$model_name\""
    exit 1
fi

# --- Setup ---
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/whisper_workflow.XXXXXX")"
WAV_FILE="$WORK_DIR/audio.wav"
SEGMENTS_DIR="$WORK_DIR/segments"
TXT_DIR="$WORK_DIR/txt"

mkdir -p "$WORK_DIR"
mkdir -p "$SEGMENTS_DIR"
mkdir -p "$TXT_DIR"

echo "=== Whisper Transcription Workflow ==="
echo "Input:  $INPUT_FILE"
echo "Output: $OUTPUT_FILE"
echo "Model:  $MODEL"
echo "Preset: $PRESET"
echo "Accuracy: $ACCURACY_HINT"
echo "Profile: $PROFILE"
echo "Mode: $MODE"
echo "Language: $LANGUAGE"
echo "Jobs:   $JOBS"
echo "Threads per job: $THREADS"
echo "Segment length: ${SEGMENT_TIME}s"
echo "WorkDir: $WORK_DIR"
echo "------------------------------------"

# --- 1. Conversion ---
echo "[1/4] Converting to 16kHz WAV..."
stage_start_sec=$SECONDS
ffmpeg -i "$INPUT_FILE" -ar 16000 -ac 1 -c:a pcm_s16le "$WAV_FILE" -y -hide_banner -loglevel error
CONVERT_DURATION_SEC=$((SECONDS - stage_start_sec))
get_audio_duration_sec

if [ "$MODE" = "single-pass" ]; then
    echo "[2/2] Transcribing full audio in single pass..."
    stage_start_sec=$SECONDS
    result="$(run_whisper_single_pass "$WAV_FILE")"
    TRANSCRIBE_DURATION_SEC=$((SECONDS - stage_start_sec))
    printf "%s\n" "$result" > "$OUTPUT_FILE"
    CONCAT_DURATION_SEC=0
    total_duration_sec=$((SECONDS - SCRIPT_START_SEC))
    print_performance_summary "$total_duration_sec"
    echo "Done! Output saved to: $OUTPUT_FILE"
    echo "=== Completed Successfully ==="
    exit 0
fi

# --- 2. Segmentation ---
echo "[2/4] Segmenting into ${SEGMENT_TIME}s chunks..."
ffmpeg -i "$WAV_FILE" -f segment -segment_time "$SEGMENT_TIME" -c copy "$SEGMENTS_DIR/segment_%03d.wav" -hide_banner -loglevel error

# --- 3. Parallel Transcription ---
echo "[3/4] Transcribing segments (Parallel)..."

SEGMENT_COUNT=$(find "$SEGMENTS_DIR" -name "segment_*.wav" | wc -l | tr -d ' ')
if [ "$SEGMENT_COUNT" -eq 0 ]; then
    echo "Error: No segments were generated."
    exit 1
fi
echo "Segments: $SEGMENT_COUNT"

export MODEL
export TXT_DIR
export LANGUAGE
export THREADS
export SEGMENT_TIME

stage_start_sec=$SECONDS
find "$SEGMENTS_DIR" -name "segment_*.wav" | sort | \
xargs -P "$JOBS" -I {} sh -c '
    file="$1"
    filename=$(basename "$file" .wav)
    txtfile="$TXT_DIR/${filename}.txt"

    seg_num=${filename#segment_}
    start_sec=$((10#$seg_num * SEGMENT_TIME))
    start_min=$((start_sec / 60))
    start_rem_sec=$((start_sec % 60))
    timestamp=$(printf "[%d:%02d]" "$start_min" "$start_rem_sec")

    echo "[3/4] Processing ${filename}..."
    result=$(whisper-cli -l "$LANGUAGE" -m "$MODEL" -t "$THREADS" -nt --no-prints "$file" || echo "")

    if [ -n "$result" ]; then
        echo "$timestamp" > "$txtfile"
        echo "$result" >> "$txtfile"
        echo "" >> "$txtfile"
    fi
' _ {}
TRANSCRIBE_DURATION_SEC=$((SECONDS - stage_start_sec))

# --- 4. Concatenation ---
echo "[4/4] Concatenating results..."
stage_start_sec=$SECONDS
> "$OUTPUT_FILE"
while IFS= read -r txt; do
    cat "$txt" >> "$OUTPUT_FILE"
done < <(find "$TXT_DIR" -name "segment_*.txt" | sort)
CONCAT_DURATION_SEC=$((SECONDS - stage_start_sec))

total_duration_sec=$((SECONDS - SCRIPT_START_SEC))
print_performance_summary "$total_duration_sec"
echo "Done! Output saved to: $OUTPUT_FILE"
echo "=== Completed Successfully ==="
