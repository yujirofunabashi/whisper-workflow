#!/bin/bash
# whisper-workflow: Automated Transcription Script
# Usage: ./transcribe_workflow.sh <input_audio_file> [output_text_file]

set -euo pipefail

# 色定義
COLOR_INFO='\033[0;34m'      # 青
COLOR_SUCCESS='\033[0;32m'   # 緑
COLOR_WARN='\033[0;33m'      # 黄
COLOR_ERROR='\033[0;31m'     # 赤
COLOR_PROGRESS='\033[0;36m'  # シアン
COLOR_RESET='\033[0m'
COLOR_BOLD='\033[1m'

# 色なしモード（CI環境など）
if [ -t 1 ] && [ "${NO_COLOR:-}" != "1" ]; then
    USE_COLOR=1
else
    USE_COLOR=0
fi

# UI関数
msg_info() { [ "$USE_COLOR" = 1 ] && echo -e "${COLOR_INFO}ℹ $*${COLOR_RESET}" || echo "ℹ $*"; }
msg_success() { [ "$USE_COLOR" = 1 ] && echo -e "${COLOR_SUCCESS}✓ $*${COLOR_RESET}" || echo "✓ $*"; }
msg_warn() { [ "$USE_COLOR" = 1 ] && echo -e "${COLOR_WARN}⚠ $*${COLOR_RESET}" || echo "⚠ $*"; }
msg_error() { [ "$USE_COLOR" = 1 ] && echo -e "${COLOR_ERROR}✗ $*${COLOR_RESET}" || echo "✗ $*"; }

# プログレスバー表示
PROGRESS_WIDTH=40
show_progress() {
    local current=$1
    local total=$2
    local label="${3:-}"
    local percent=0
    local filled=0

    if [ "$total" -gt 0 ]; then
        percent=$((current * 100 / total))
        filled=$((PROGRESS_WIDTH * current / total))
    fi

    local empty=$((PROGRESS_WIDTH - filled))
    local bar=""
    bar="[${COLOR_PROGRESS}"

    for ((i=0; i<filled; i++)); do bar+="█"; done
    for ((i=0; i<empty; i++)); do bar+="░"; done

    bar+="${COLOR_RESET}] ${percent}%"

    if [ -n "$label" ]; then
        bar+=" - $label"
    fi

    printf "\r%s" "$bar"
}

# プログレスバーをクリア
clear_progress() {
    printf "\r%$((PROGRESS_WIDTH + 50))s\r" ""
}

INPUT_FILE="${1:-}"
OUTPUT_FILE="${2:-transcription_result.txt}"
WORK_DIR=""
SCRIPT_START_SEC=$SECONDS
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 統計用変数
SEGMENT_SUCCESSFUL=0
SEGMENT_FAILED=0
ESTIMATED_WORDS=0
ESTIMATED_CHARS=0

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
RETRY_COUNT="${WHISPER_RETRY_COUNT:-2}"
RETRY_BACKOFF_SEC="${WHISPER_RETRY_BACKOFF_SEC:-1}"
PREFLIGHT_NORMALIZE="${WHISPER_PREFLIGHT_NORMALIZE:-0}" # 1 to enable loudnorm during conversion
MODE_OVERRIDE="${WHISPER_MODE:-}"               # optional: single-pass | segmented
USE_VAD="${WHISPER_USE_VAD:-0}"                 # 1 to enable whisper-cli VAD
VAD_MODEL="${WHISPER_VAD_MODEL:-}"
VAD_THRESHOLD="${WHISPER_VAD_THRESHOLD:-0.50}"
VAD_MIN_SPEECH_MS="${WHISPER_VAD_MIN_SPEECH_MS:-250}"
VAD_MIN_SILENCE_MS="${WHISPER_VAD_MIN_SILENCE_MS:-100}"
VAD_MAX_SPEECH_S="${WHISPER_VAD_MAX_SPEECH_S:-}"
VAD_SPEECH_PAD_MS="${WHISPER_VAD_SPEECH_PAD_MS:-30}"
VAD_SAMPLES_OVERLAP="${WHISPER_VAD_SAMPLES_OVERLAP:-0.10}"
VAD_ALLOW_SEGMENTED="${WHISPER_VAD_ALLOW_SEGMENTED:-0}" # 1 to force VAD in segmented mode
VAD_CPU_ONLY="${WHISPER_VAD_CPU_ONLY:-1}"         # 1 to force -ng while VAD is enabled (stability)
WHISPER_CLI_TIMEOUT_SEC="${WHISPER_CLI_TIMEOUT_SEC:-}" # optional hard timeout per whisper-cli call
FORCE_CPU="${WHISPER_FORCE_CPU:-0}"               # 1 to disable GPU path for stability
CURRENT_WHISPER_TIMEOUT_SEC=""
TIMEOUT_CMD=""

if command -v timeout >/dev/null 2>&1; then
    TIMEOUT_CMD="$(command -v timeout)"
elif command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_CMD="$(command -v gtimeout)"
fi

is_positive_int() {
    case "${1:-}" in
        ''|*[!0-9]*|0) return 1 ;;
        *) return 0 ;;
    esac
}

is_non_negative_int() {
    case "${1:-}" in
        ''|*[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

is_number() {
    case "${1:-}" in
        ''|*[^0-9.-]*|*.*.*|*--*) return 1 ;;
        -|.) return 1 ;;
        *) return 0 ;;
    esac
}

resolve_whisper_timeout_sec() {
    local input_wav="$1"
    local duration_sec=""
    local derived=0

    if is_positive_int "${WHISPER_CLI_TIMEOUT_SEC:-}"; then
        echo "$WHISPER_CLI_TIMEOUT_SEC"
        return
    fi

    if command -v ffprobe >/dev/null 2>&1; then
        duration_sec="$(
            ffprobe -v error \
                -show_entries format=duration \
                -of default=nokey=1:noprint_wrappers=1 \
                "$input_wav" 2>/dev/null || true
        )"
    fi

    if is_number "${duration_sec:-}"; then
        derived="$(awk -v d="$duration_sec" 'BEGIN { t = int((d * 3.0) + 60.0); if (t < 120) t = 120; if (t > 7200) t = 7200; print t }')"
    fi

    if is_positive_int "${derived:-}"; then
        echo "$derived"
    else
        echo "0"
    fi
}

cleanup() {
    if [ -n "${WORK_DIR:-}" ] && [ "$WORK_DIR" != "/" ] && [ -d "$WORK_DIR" ]; then
        echo "Cleaning up intermediate files..."
        rm -rf -- "$WORK_DIR"
    fi
}

run_whisper_with_retry() {
    local input_wav="$1"
    local run_label="$2"
    local err_file="$3"
    local result=""
    local rc=0
    local attempt=1
    local vad_before_retry="$USE_VAD"
    local timeout_sec="0"

    timeout_sec="$(resolve_whisper_timeout_sec "$input_wav")"
    CURRENT_WHISPER_TIMEOUT_SEC="$timeout_sec"

    while [ "$attempt" -le "$RETRY_COUNT" ]; do
        set +e
        result="$(run_whisper_once "$input_wav" "$err_file" "gpu")"
        rc=$?
        set -e

        if [ "$rc" -eq 0 ]; then
            printf "%s\n" "$result"
            return 0
        fi

        if [ "$FORCE_CPU" != "1" ] && grep -Eiq 'ggml-metal-device\.m|ggml_assert.*metal|libggml-metal' "$err_file"; then
            echo "Warning: Metal(GPU) error detected; forcing CPU mode for this run."
            FORCE_CPU=1
        fi

        echo "Warning: $run_label GPU attempt $attempt/$RETRY_COUNT failed. Retrying with CPU (-ng)..." >&2
        set +e
        result="$(run_whisper_once "$input_wav" "$err_file" "cpu")"
        rc=$?
        set -e

        if [ "$rc" -eq 0 ]; then
            printf "%s\n" "$result"
            return 0
        fi

        if [ "$attempt" -lt "$RETRY_COUNT" ]; then
            echo "Warning: $run_label attempt $attempt/$RETRY_COUNT failed. Retrying after ${RETRY_BACKOFF_SEC}s..." >&2
            sleep "$RETRY_BACKOFF_SEC"
        fi
        attempt=$((attempt + 1))
    done

    if [ "$vad_before_retry" = "1" ]; then
        if grep -Eiq 'ggml-metal-device\.m|failed to process audio|vad' "$err_file"; then
            echo "Warning: $run_label failed with VAD enabled; retrying once with VAD disabled for stability." >&2
            USE_VAD=0
            : > "$err_file"
            attempt=1

            while [ "$attempt" -le "$RETRY_COUNT" ]; do
                set +e
                result="$(run_whisper_once "$input_wav" "$err_file" "gpu")"
                rc=$?
                set -e

                if [ "$rc" -eq 0 ]; then
                    printf "%s\n" "$result"
                    return 0
                fi

                echo "Warning: $run_label (VAD OFF) GPU attempt $attempt/$RETRY_COUNT failed. Retrying with CPU (-ng)..." >&2
                set +e
                result="$(run_whisper_once "$input_wav" "$err_file" "cpu")"
                rc=$?
                set -e

                if [ "$rc" -eq 0 ]; then
                    printf "%s\n" "$result"
                    return 0
                fi

                if [ "$attempt" -lt "$RETRY_COUNT" ]; then
                    echo "Warning: $run_label (VAD OFF) attempt $attempt/$RETRY_COUNT failed. Retrying after ${RETRY_BACKOFF_SEC}s..." >&2
                    sleep "$RETRY_BACKOFF_SEC"
                fi
                attempt=$((attempt + 1))
            done
        fi
    fi

    echo "Error: $run_label failed after $RETRY_COUNT attempt(s)." >&2
    echo "---- whisper stderr (last 80 lines for $run_label) ----" >&2
    tail -n 80 "$err_file" >&2 || true
    return 1
}

run_whisper_once() {
    local input_wav="$1"
    local err_file="$2"
    local backend="$3" # gpu|cpu
    local effective_backend="$backend"
    local -a cmd=(whisper-cli)

    if [ "$USE_VAD" = "1" ] && [ "$VAD_CPU_ONLY" = "1" ]; then
        effective_backend="cpu"
    fi
    if [ "$FORCE_CPU" = "1" ]; then
        effective_backend="cpu"
    fi

    if [ "$effective_backend" = "cpu" ]; then
        cmd+=(-ng)
    fi

    cmd+=(-l "$LANGUAGE" -m "$MODEL" -t "$THREADS" -nt --no-prints)

    if [ "$USE_VAD" = "1" ]; then
        cmd+=(--vad --vad-model "$VAD_MODEL" --vad-threshold "$VAD_THRESHOLD" --vad-min-speech-duration-ms "$VAD_MIN_SPEECH_MS" --vad-min-silence-duration-ms "$VAD_MIN_SILENCE_MS" --vad-speech-pad-ms "$VAD_SPEECH_PAD_MS" --vad-samples-overlap "$VAD_SAMPLES_OVERLAP")
        if [ -n "$VAD_MAX_SPEECH_S" ]; then
            cmd+=(--vad-max-speech-duration-s "$VAD_MAX_SPEECH_S")
        fi
    fi

    cmd+=("$input_wav")
    if [ -n "$TIMEOUT_CMD" ] && is_positive_int "${CURRENT_WHISPER_TIMEOUT_SEC:-0}"; then
        "$TIMEOUT_CMD" "$CURRENT_WHISPER_TIMEOUT_SEC" "${cmd[@]}" 2>>"$err_file"
    else
        "${cmd[@]}" 2>>"$err_file"
    fi
}

run_whisper_single_pass() {
    local input_wav="$1"
    local err_file="$WORK_DIR/whisper_single.err"

    : > "$err_file"
    run_whisper_with_retry "$input_wav" "single-pass" "$err_file"
}

process_segment_file() {
    local file="$1"
    local filename
    local txtfile
    local seg_num
    local start_sec
    local start_min
    local start_rem_sec
    local timestamp
    local err_file
    local result

    filename="$(basename "$file" .wav)"
    txtfile="$TXT_DIR/${filename}.txt"

    seg_num="${filename#segment_}"
    start_sec=$((10#$seg_num * SEGMENT_TIME))
    start_min=$((start_sec / 60))
    start_rem_sec=$((start_sec % 60))
    timestamp="$(printf "[%d:%02d]" "$start_min" "$start_rem_sec")"
    err_file="$WORK_DIR/${filename}.err"

    : > "$err_file"
    if ! result="$(run_whisper_with_retry "$file" "$filename" "$err_file")"; then
        {
            echo "$timestamp"
            echo "[SEGMENT_FAILED] ${filename}"
            echo ""
        } > "$txtfile"
        msg_warn "${filename} が失敗しました（プレースホルダーを挿入）"
        printf "%s\n" "$result" || true
        return 1
    fi

    {
        echo "$timestamp"
        printf "%s\n" "$result"
        echo ""
    } > "$txtfile"
}

get_audio_duration_sec() {
    if command -v ffprobe >/dev/null 2>&1; then
        local raw
        raw="$(
            ffprobe -v error \
                -show_entries format=duration \
                -of default=nokey=1:noprint_wrappers=1 \
                "$WAV_FILE" 2>/dev/null || true
        )"
        # 浮動小数点を整数に切り捨て (bash は整数演算のみ)
        AUDIO_DURATION_SEC="${raw%%.*}"
    fi
}

# 秒をHH:MM:SS形式に変換
format_duration() {
    local total_sec="$1"
    local hours=$((total_sec / 3600))
    local minutes=$(((total_sec % 3600) / 60))
    local seconds=$((total_sec % 60))
    if [ "$hours" -gt 0 ]; then
        printf "%d時間%d分%d秒" "$hours" "$minutes" "$seconds"
    elif [ "$minutes" -gt 0 ]; then
        printf "%d分%d秒" "$minutes" "$seconds"
    else
        printf "%d秒" "$seconds"
    fi
}

# テキストファイルから単語・文字数を推計
estimate_text_stats() {
    local txt_file="$1"
    if [ ! -f "$txt_file" ]; then
        echo "0 0"
        return
    fi

    # 日本語と英語の混在テキストを簡易カウント
    # 文字数: 全体の文字数（空白・改行除く）
    # 単語数: 空白区切りの数（英語）+ 文字数/3（日本語の概算）
    local char_count=0
    local word_count=0

    char_count="$(tr -d '[:space:]' < "$txt_file" | wc -m | tr -d ' ')"
    word_count="$(wc -w < "$txt_file" | tr -d ' ')"

    # 日本語テキストの場合、文字数/3を単語数の推定に加算
    local jp_words=$((char_count / 3))
    if [ "$jp_words" -gt "$word_count" ]; then
        word_count="$jp_words"
    fi

    echo "$word_count $char_count"
}

print_performance_summary() {
    local total_duration_sec="$1"
    local segment_count="${2:-0}"

    # ボックスの幅
    local box_width=50

    echo ""
    if [ "$USE_COLOR" = 1 ]; then
        echo -e "${COLOR_BOLD}═══════════════════════════════════════════════════════${COLOR_RESET}"
        echo -e "${COLOR_BOLD}               処理結果サマリー${COLOR_RESET}"
        echo -e "${COLOR_BOLD}═══════════════════════════════════════════════════════${COLOR_RESET}"
    else
        echo "==================================================="
        echo "               処理結果サマリー"
        echo "==================================================="
    fi

    # 時間情報
    echo ""
    echo " 📊 処理時間"
    echo " ───────────────────────────────────────────────────"
    echo "  音声長:     $(format_duration "${AUDIO_DURATION_SEC:-0}")"
    echo "  変換:       $(format_duration "$CONVERT_DURATION_SEC")"
    echo "  転記:       $(format_duration "$TRANSCRIBE_DURATION_SEC")"
    echo "  結合:       $(format_duration "$CONCAT_DURATION_SEC")"
    echo "  合計:       $(format_duration "$total_duration_sec")"

    # 速度指標
    if [ -n "$AUDIO_DURATION_SEC" ] && is_number "$AUDIO_DURATION_SEC" && [ "$AUDIO_DURATION_SEC" -gt 0 ]; then
        local transcribe_x
        transcribe_x="$(awk -v t="$TRANSCRIBE_DURATION_SEC" -v a="$AUDIO_DURATION_SEC" 'BEGIN { if (t > 0) printf "%.2f", a / t; else print "0" }')"
        local total_x
        total_x="$(awk -v t="$total_duration_sec" -v a="$AUDIO_DURATION_SEC" 'BEGIN { if (t > 0) printf "%.2f", a / t; else print "0" }')"

        echo ""
        echo " ⚡ 処理速度"
        echo " ───────────────────────────────────────────────────"
        echo "  転記速度:   ${transcribe_x}x リアルタイム"
        echo "  全体速度:   ${total_x}x リアルタイム"
    fi

    # セグメント情報
    if [ "$segment_count" -gt 0 ]; then
        local success_percent=0
        if [ "$segment_count" -gt 0 ]; then
            success_percent=$((SEGMENT_SUCCESSFUL * 100 / segment_count))
        fi

        echo ""
        echo " 📦 セグメント処理"
        echo " ───────────────────────────────────────────────────"
        echo "  セグメント数: $segment_count"
        if [ "$USE_COLOR" = 1 ]; then
            if [ "$SEGMENT_SUCCESSFUL" -eq "$segment_count" ]; then
                echo -e "  成功:         ${COLOR_SUCCESS}${SEGMENT_SUCCESSFUL} (${success_percent}%)${COLOR_RESET}"
            else
                echo -e "  成功:         ${COLOR_SUCCESS}${SEGMENT_SUCCESSFUL}${COLOR_RESET} / ${segment_count} (${success_percent}%)"
            fi
            if [ "$SEGMENT_FAILED" -gt 0 ]; then
                echo -e "  失敗:         ${COLOR_ERROR}${SEGMENT_FAILED} ($((100 - success_percent))%)${COLOR_RESET}"
            fi
        else
            echo "  成功:         ${SEGMENT_SUCCESSFUL} / ${segment_count} (${success_percent}%)"
            if [ "$SEGMENT_FAILED" -gt 0 ]; then
                echo "  失敗:         ${SEGMENT_FAILED} ($((100 - success_percent))%)"
            fi
        fi
    fi

    # テキスト統計
    if [ -f "$OUTPUT_FILE" ] && [ "$OUTPUT_FILE" != "/dev/null" ]; then
        local text_stats
        text_stats="$(estimate_text_stats "$OUTPUT_FILE")"
        local word_count char_count
        read -r word_count char_count <<< "$text_stats"

        echo ""
        echo " 📝 テキスト統計（推計）"
        echo " ───────────────────────────────────────────────────"
        echo "  単語数:     ~$(printf "%'d" "$word_count")"
        echo "  文字数:     ~$(printf "%'d" "$char_count")"
    fi

    # 出力ファイル情報
    echo ""
    echo " 📁 出力ファイル"
    echo " ───────────────────────────────────────────────────"
    echo "  結果:       $OUTPUT_FILE"
    if [ "$SEGMENT_FAILED" -gt 0 ]; then
        echo "  失敗リスト: ${FAILED_SEGMENTS_FILE}"
    fi

    # 区切り線
    if [ "$USE_COLOR" = 1 ]; then
        echo ""
        echo -e "${COLOR_BOLD}═══════════════════════════════════════════════════════${COLOR_RESET}"
    else
        echo ""
        echo "==================================================="
    fi
}

SSD_MODEL_DIR="/Volumes/JIRO SSD 1TB/02_開発資産/ai-models/whisper-cpp"
resolve_model_path() {
    local name="$1"
    if [ -f "$SSD_MODEL_DIR/$name" ]; then
        echo "$SSD_MODEL_DIR/$name"
    else
        echo "$HOME/.cache/whisper-cpp/$name"
    fi
}

apply_preset() {
    case "$PRESET" in
        x1)
            MODE="single-pass"
            [ -n "$MODEL" ] || MODEL="$(resolve_model_path ggml-large-v3.bin)"
            THREADS="${WHISPER_THREADS:-$CPU_THREADS}"
            JOBS="${WHISPER_JOBS:-1}"
            SEGMENT_TIME="${WHISPER_SEGMENT_TIME:-240}"
            ACCURACY_HINT="best (highest)"
            ;;
        x4)
            MODE="single-pass"
            [ -n "$MODEL" ] || MODEL="$(resolve_model_path ggml-medium.bin)"
            THREADS="${WHISPER_THREADS:-$CPU_THREADS}"
            JOBS="${WHISPER_JOBS:-1}"
            SEGMENT_TIME="${WHISPER_SEGMENT_TIME:-240}"
            ACCURACY_HINT="high"
            ;;
        x8)
            MODE="single-pass"
            [ -n "$MODEL" ] || MODEL="$(resolve_model_path ggml-small.bin)"
            THREADS="${WHISPER_THREADS:-$CPU_THREADS}"
            JOBS="${WHISPER_JOBS:-1}"
            SEGMENT_TIME="${WHISPER_SEGMENT_TIME:-240}"
            ACCURACY_HINT="medium"
            ;;
        x16)
            MODE="single-pass"
            [ -n "$MODEL" ] || MODEL="$(resolve_model_path ggml-tiny.bin)"
            THREADS="${WHISPER_THREADS:-$CPU_THREADS}"
            JOBS="${WHISPER_JOBS:-1}"
            SEGMENT_TIME="${WHISPER_SEGMENT_TIME:-240}"
            ACCURACY_HINT="low-medium"
            ;;
        x1-turbo)
            MODE="single-pass"
            [ -n "$MODEL" ] || MODEL="$(resolve_model_path ggml-large-v3-turbo.bin)"
            THREADS="${WHISPER_THREADS:-$CPU_THREADS}"
            JOBS="${WHISPER_JOBS:-1}"
            SEGMENT_TIME="${WHISPER_SEGMENT_TIME:-240}"
            ACCURACY_HINT="high (turbo)"
            ;;
        custom|"")
            [ -n "$MODEL" ] || MODEL="$(resolve_model_path ggml-large-v3.bin)"
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
            echo "Error: Unsupported WHISPER_PRESET '$PRESET' (use x1, x4, x8, x16, x1-turbo, custom)"
            exit 1
            ;;
    esac

    case "$MODE_OVERRIDE" in
        "")
            ;;
        single-pass|segmented)
            MODE="$MODE_OVERRIDE"
            ;;
        *)
            echo "Error: WHISPER_MODE must be 'single-pass' or 'segmented': '$MODE_OVERRIDE'"
            exit 1
            ;;
    esac

    # When mode is forced to segmented and caller did not pass explicit values,
    # restore practical segmented defaults instead of preset single-pass defaults.
    if [ "$MODE" = "segmented" ]; then
        if [ -z "${WHISPER_JOBS+x}" ]; then
            JOBS=4
        fi
        if [ -z "${WHISPER_SEGMENT_TIME+x}" ]; then
            SEGMENT_TIME=60
        fi
        if [ -z "${WHISPER_THREADS+x}" ]; then
            THREADS=$((CPU_THREADS / JOBS))
            if [ "$THREADS" -lt 1 ]; then
                THREADS=1
            fi
        fi
        if [ "$USE_VAD" = "1" ] && [ "$VAD_ALLOW_SEGMENTED" != "1" ]; then
            echo "Info: disabling VAD in segmented mode for stability (set WHISPER_VAD_ALLOW_SEGMENTED=1 to force)."
            USE_VAD=0
        fi
    elif [ "$MODE" = "single-pass" ]; then
        if [ -z "${WHISPER_JOBS+x}" ]; then
            JOBS=1
        fi
        if [ -z "${WHISPER_SEGMENT_TIME+x}" ]; then
            SEGMENT_TIME=240
        fi
    fi

    if [ "$USE_VAD" = "1" ] && [ -z "$VAD_MODEL" ]; then
        for candidate in \
            "$SSD_MODEL_DIR/ggml-silero-v6.2.0.bin" \
            "$SSD_MODEL_DIR/ggml-silero-v5.1.2.bin" \
            "$HOME/.cache/whisper-cpp/ggml-silero-v6.2.0.bin" \
            "$HOME/.cache/whisper-cpp/ggml-silero-v5.1.2.bin" \
            "$SCRIPT_DIR/models/ggml-silero-v6.2.0.bin" \
            "$SCRIPT_DIR/models/ggml-silero-v5.1.2.bin"
        do
            if [ -f "$candidate" ]; then
                VAD_MODEL="$candidate"
                break
            fi
        done
    fi

    if [ "$USE_VAD" = "1" ] && [ ! -f "$VAD_MODEL" ]; then
        echo "Warning: VAD model not found; disabling VAD."
        echo "Hint: download ggml-silero-v6.2.0.bin into ~/.cache/whisper-cpp/"
        USE_VAD=0
        VAD_MODEL=""
    fi
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

if ! is_positive_int "$RETRY_COUNT"; then
    echo "Error: WHISPER_RETRY_COUNT must be a positive integer: '$RETRY_COUNT'"
    exit 1
fi

if ! is_non_negative_int "$RETRY_BACKOFF_SEC"; then
    echo "Error: WHISPER_RETRY_BACKOFF_SEC must be a non-negative integer: '$RETRY_BACKOFF_SEC'"
    exit 1
fi

# --- Validation ---
if [ -z "$INPUT_FILE" ]; then
    msg_error "使用方法: $0 <音声ファイル> [出力テキストファイル]"
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    msg_error "入力ファイルが見つかりません: $INPUT_FILE"
    exit 1
fi

if [ ! -f "$MODEL" ]; then
    model_name="$(basename "$MODEL")"
    msg_error "モデルファイルが見つかりません: $MODEL"
    echo "ダウンロード例:"
    echo "  mkdir -p ~/.cache/whisper-cpp"
    echo "  curl -L -o \"$MODEL\" \"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$model_name\""
    exit 1
fi

# --- Setup ---
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/whisper_workflow.XXXXXX")"
WAV_FILE="$WORK_DIR/audio.wav"
SEGMENTS_DIR="$WORK_DIR/segments"
TXT_DIR="$WORK_DIR/txt"
FAILED_SEGMENTS_FILE="${OUTPUT_FILE}.failed_segments.txt"
RECOVERY_META_FILE="${OUTPUT_FILE}.recovery_meta"

mkdir -p "$WORK_DIR"
mkdir -p "$SEGMENTS_DIR"
mkdir -p "$TXT_DIR"
mkdir -p "$(dirname "$OUTPUT_FILE")"
rm -f -- "$FAILED_SEGMENTS_FILE"
rm -f -- "$RECOVERY_META_FILE"

if [ "$MODE" = "segmented" ]; then
    {
        echo "SEGMENT_TIME=$SEGMENT_TIME"
        echo "PRESET=$PRESET"
        echo "LANGUAGE=$LANGUAGE"
        echo "INPUT_FILE=$INPUT_FILE"
        echo "MODEL=$MODEL"
        echo "JOBS=$JOBS"
        echo "THREADS=$THREADS"
    } > "$RECOVERY_META_FILE"
fi

# ヘッダー表示
echo ""
if [ "$USE_COLOR" = 1 ]; then
    echo -e "${COLOR_BOLD}═══════════════════════════════════════════════════════${COLOR_RESET}"
    echo -e "${COLOR_BOLD}              Whisper 文字起こしワークフロー              ${COLOR_RESET}"
    echo -e "${COLOR_BOLD}═══════════════════════════════════════════════════════${COLOR_RESET}"
else
    echo "==================================================="
    echo "              Whisper 文字起こしワークフロー"
    echo "==================================================="
fi
echo ""
echo " 📁 入力ファイル:  $INPUT_FILE"
echo " 📄 出力ファイル:  $OUTPUT_FILE"
echo " 🧠 モデル:       $MODEL"
echo " ⚙️  プリセット:    $PRESET ($ACCURACY_HINT)"
echo " 🌐 言語:         $LANGUAGE"
echo " 🔧 モード:       $MODE"
echo " 🔄 並列ジョブ:   $JOBS"
echo " 🧵 スレッド/ジョブ: $THREADS"
echo " ⏱️  セグメント長:  ${SEGMENT_TIME}秒"
echo " 🔁 リトライ回数:  $RETRY_COUNT"
if [ "$USE_VAD" = "1" ]; then
    echo " 🎤 VAD:          有効 (閾値: $VAD_THRESHOLD)"
fi
echo ""

# --- 1. Conversion ---
msg_info "[1/4] 音声ファイルを変換中 (16kHz WAV)..."
stage_start_sec=$SECONDS
if [ "$PREFLIGHT_NORMALIZE" = "1" ]; then
    echo "   音量正規化適用中 (I=-16, TP=-1.5, LRA=11)..."
    ffmpeg -i "$INPUT_FILE" -af "loudnorm=I=-16:TP=-1.5:LRA=11" -ar 16000 -ac 1 -c:a pcm_s16le "$WAV_FILE" -y -hide_banner -loglevel error
else
    ffmpeg -i "$INPUT_FILE" -ar 16000 -ac 1 -c:a pcm_s16le "$WAV_FILE" -y -hide_banner -loglevel error
fi
CONVERT_DURATION_SEC=$((SECONDS - stage_start_sec))
get_audio_duration_sec
msg_success "変換完了 ($(format_duration "$CONVERT_DURATION_SEC"))"

if [ "$MODE" = "single-pass" ]; then
    msg_info "[2/2] 音声全体を一度に文字起こし中..."
    stage_start_sec=$SECONDS
    if ! result="$(run_whisper_single_pass "$WAV_FILE")"; then
        printf "%s\n" "$result"
        msg_error "シングルパス文字起こしが失敗しました"
        exit 1
    fi
    TRANSCRIBE_DURATION_SEC=$((SECONDS - stage_start_sec))
    printf "%s\n" "$result" > "$OUTPUT_FILE"
    SEGMENT_SUCCESSFUL=1
    CONCAT_DURATION_SEC=0
    total_duration_sec=$((SECONDS - SCRIPT_START_SEC))
    print_performance_summary "$total_duration_sec" "1"
    echo ""
    msg_success "完了: $OUTPUT_FILE"
    if [ "$USE_COLOR" = 1 ]; then
        echo -e "${COLOR_SUCCESS}═══════════════════════════════════════════════════════${COLOR_RESET}"
        echo -e "${COLOR_SUCCESS}                    ✓ 成功                           ${COLOR_RESET}"
        echo -e "${COLOR_SUCCESS}═══════════════════════════════════════════════════════${COLOR_RESET}"
    else
        echo "==================================================="
        echo "                    ✓ 成功"
        echo "==================================================="
    fi
    exit 0
fi

# --- 2. Segmentation ---
msg_info "[2/4] セグメント分割中 (${SEGMENT_TIME}秒単位)..."
ffmpeg -i "$WAV_FILE" -f segment -segment_time "$SEGMENT_TIME" -c copy "$SEGMENTS_DIR/segment_%03d.wav" -hide_banner -loglevel error

# --- 3. Parallel Transcription ---
SEGMENT_COUNT=$(find "$SEGMENTS_DIR" -name "segment_*.wav" | wc -l | tr -d ' ')
if [ "$SEGMENT_COUNT" -eq 0 ]; then
    msg_error "セグメントが生成されませんでした"
    exit 1
fi
msg_info "[3/4] セグメントを並列文字起こし中 ($SEGMENT_COUNT 個のセグメント, $JOBS 並列)..."
echo "Segments: $SEGMENT_COUNT"

# セグメント処理関数（進捗表示付き）
process_segment_with_progress() {
    local file="$1"
    local filename
    local txtfile

    filename="$(basename "$file" .wav)"
    txtfile="$TXT_DIR/${filename}.txt"

    if process_segment_file "$file"; then
        echo "[3/4] Completed $filename"
        # 成功時にカウンターを更新
        if [ -f "$WORK_DIR/.success_count" ]; then
            local count
            count=$(cat "$WORK_DIR/.success_count")
            echo $((count + 1)) > "$WORK_DIR/.success_count"
        else
            echo 1 > "$WORK_DIR/.success_count"
        fi
        return 0
    else
        # 失敗時にカウンターを更新
        if [ -f "$WORK_DIR/.fail_count" ]; then
            local count
            count=$(cat "$WORK_DIR/.fail_count")
            echo $((count + 1)) > "$WORK_DIR/.fail_count"
        else
            echo 1 > "$WORK_DIR/.fail_count"
        fi
        return 1
    fi
}

export MODEL
export TXT_DIR
export LANGUAGE
export THREADS
export SEGMENT_TIME
export WORK_DIR
export RETRY_COUNT
export RETRY_BACKOFF_SEC
export USE_VAD
export FORCE_CPU
export VAD_MODEL
export VAD_THRESHOLD
export VAD_MIN_SPEECH_MS
export VAD_MIN_SILENCE_MS
export VAD_MAX_SPEECH_S
export VAD_SPEECH_PAD_MS
export VAD_SAMPLES_OVERLAP
export -f is_positive_int
export -f is_number
export -f resolve_whisper_timeout_sec
export -f run_whisper_once
export -f run_whisper_with_retry
export -f process_segment_file
export -f process_segment_with_progress

# カウンター初期化
echo 0 > "$WORK_DIR/.success_count"
echo 0 > "$WORK_DIR/.fail_count"
echo 0 > "$WORK_DIR/.processed_count"

# 並列処理実行（バックグラウンドで進捗監視）
stage_start_sec=$SECONDS

# 進捗監視プロセス
(
    while true; do
        if [ -f "$WORK_DIR/.processed_count" ]; then
            processed=$(cat "$WORK_DIR/.processed_count" 2>/dev/null || echo 0)
            show_progress "$processed" "$SEGMENT_COUNT" "処理中"
        fi
        sleep 0.3
        # すべてのtxtファイルが生成されたら終了
        txt_count=$(find "$TXT_DIR" -name "segment_*.txt" 2>/dev/null | wc -l | tr -d ' ')
        if [ "$txt_count" -ge "$SEGMENT_COUNT" ]; then
            break
        fi
    done
) &
PROGRESS_PID=$!

# xargsによる並列処理（処理完了カウンター付き）
xargs_rc=0
set +e
find "$SEGMENTS_DIR" -name "segment_*.wav" | sort | \
xargs -P "$JOBS" -I {} bash -c '
    process_segment_with_progress "$1"
    processed=$(cat "$WORK_DIR/.processed_count" 2>/dev/null || echo 0)
    echo $((processed + 1)) > "$WORK_DIR/.processed_count"
' _ {}
xargs_rc=$?
set -e

# 進捗監視を停止
kill "$PROGRESS_PID" 2>/dev/null || true
wait "$PROGRESS_PID" 2>/dev/null || true
clear_progress

TRANSCRIBE_DURATION_SEC=$((SECONDS - stage_start_sec))

# 統計を集計
SEGMENT_SUCCESSFUL=$(cat "$WORK_DIR/.success_count" 2>/dev/null || echo 0)
SEGMENT_FAILED=$(cat "$WORK_DIR/.fail_count" 2>/dev/null || echo 0)

if [ "$SEGMENT_SUCCESSFUL" -gt 0 ]; then
    msg_success "文字起こし完了 (${SEGMENT_SUCCESSFUL}/${SEGMENT_COUNT} セグメント)"
fi

# --- 4. Concatenation ---
msg_info "[4/4] 結果を結合中..."
stage_start_sec=$SECONDS
> "$OUTPUT_FILE"
while IFS= read -r txt; do
    cat "$txt" >> "$OUTPUT_FILE"
done < <(find "$TXT_DIR" -name "segment_*.txt" | sort)
CONCAT_DURATION_SEC=$((SECONDS - stage_start_sec))
msg_success "結合完了"

total_duration_sec=$((SECONDS - SCRIPT_START_SEC))
print_performance_summary "$total_duration_sec" "$SEGMENT_COUNT"

if [ "$xargs_rc" -ne 0 ]; then
    grep -h "^\[SEGMENT_FAILED\]" "$TXT_DIR"/segment_*.txt 2>/dev/null | \
        sed 's/^\[SEGMENT_FAILED\] //' | sort -u > "$FAILED_SEGMENTS_FILE" || true
    failed_count="$(wc -l < "$FAILED_SEGMENTS_FILE" 2>/dev/null | tr -d ' ' || echo 0)"
    if [ "${failed_count:-0}" -gt 0 ]; then
        msg_warn "${failed_count} 個のセグメントが失敗しました。部分的なテキストが保存されました。"
        echo "失敗リスト: $FAILED_SEGMENTS_FILE"
        echo ""
        msg_success "完了: $OUTPUT_FILE"
        if [ "$USE_COLOR" = 1 ]; then
            echo -e "${COLOR_WARN}⚠ 警告ありで完了${COLOR_RESET}"
        else
            echo "⚠ 警告ありで完了"
        fi
        exit 0
    fi
    msg_error "セグメント文字起こしが失敗しました"
    exit 1
fi

echo ""
msg_success "完了: $OUTPUT_FILE"
if [ "$USE_COLOR" = 1 ]; then
    echo -e "${COLOR_SUCCESS}═══════════════════════════════════════════════════════${COLOR_RESET}"
    echo -e "${COLOR_SUCCESS}                    ✓ 成功                           ${COLOR_RESET}"
    echo -e "${COLOR_SUCCESS}═══════════════════════════════════════════════════════${COLOR_RESET}"
else
    echo "==================================================="
    echo "                    ✓ 成功"
    echo "==================================================="
fi
