#!/bin/bash
# Usage: ./preflight.sh <input_audio_file> [accuracy|balanced|speed]

set -euo pipefail

INPUT_FILE="${1:-}"
PRIORITY="${2:-balanced}"

if [ -z "$INPUT_FILE" ]; then
    echo '{"verdict":"NOT_RECOMMENDED","reasons":["input file is required"],"corrections":[],"input":{"container":null,"codec":null,"duration_sec":0,"channels":null,"sample_rate":null,"file_size_bytes":0,"mean_volume_db":null,"max_volume_db":null,"silence_ratio":1,"convertible":false},"recommended_preset":"x16","preset_reason":"no input file"}'
    exit 0
fi

if [ ! -f "$INPUT_FILE" ]; then
    echo '{"verdict":"NOT_RECOMMENDED","reasons":["input file does not exist"],"corrections":[],"input":{"container":null,"codec":null,"duration_sec":0,"channels":null,"sample_rate":null,"file_size_bytes":0,"mean_volume_db":null,"max_volume_db":null,"silence_ratio":1,"convertible":false},"recommended_preset":"x16","preset_reason":"missing input file"}'
    exit 0
fi

if [ "$PRIORITY" != "accuracy" ] && [ "$PRIORITY" != "balanced" ] && [ "$PRIORITY" != "speed" ]; then
    PRIORITY="balanced"
fi

SCAN_SEC="${WHISPER_PREFLIGHT_SCAN_SEC:-60}"

json_escape() {
    local s="${1:-}"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    printf '%s' "$s"
}

is_number() {
    case "${1:-}" in
        ''|*[^0-9.-]*|*.*.*|*--*) return 1 ;;
        -|.) return 1 ;;
        *) return 0 ;;
    esac
}

is_integer() {
    case "${1:-}" in
        ''|*[!0-9-]*|-) return 1 ;;
        *) return 0 ;;
    esac
}

if ! is_integer "$SCAN_SEC" || [ "$SCAN_SEC" -le 0 ]; then
    SCAN_SEC=60
fi
if [ "$SCAN_SEC" -gt 300 ]; then
    SCAN_SEC=300
fi

float_lt() {
    awk -v a="$1" -v b="$2" 'BEGIN { exit !(a < b) }'
}

float_le() {
    awk -v a="$1" -v b="$2" 'BEGIN { exit !(a <= b) }'
}

float_ge() {
    awk -v a="$1" -v b="$2" 'BEGIN { exit !(a >= b) }'
}

float_gt() {
    awk -v a="$1" -v b="$2" 'BEGIN { exit !(a > b) }'
}

format_one_decimal() {
    awk -v n="$1" 'BEGIN { printf "%.1f", n }'
}

format_two_decimal() {
    awk -v n="$1" 'BEGIN { printf "%.2f", n }'
}

json_num_or_null() {
    local v="${1:-}"
    if is_number "$v"; then
        printf '%s' "$v"
    else
        printf 'null'
    fi
}

json_int_or_null() {
    local v="${1:-}"
    if is_integer "$v"; then
        printf '%s' "$v"
    else
        printf 'null'
    fi
}

json_str_or_null() {
    local v="${1:-}"
    if [ -n "$v" ]; then
        printf '"%s"' "$(json_escape "$v")"
    else
        printf 'null'
    fi
}

array_to_json() {
    local first=1
    printf '['
    for item in "$@"; do
        [ -n "$item" ] || continue
        if [ "$first" -eq 0 ]; then
            printf ','
        fi
        first=0
        printf '"%s"' "$(json_escape "$item")"
    done
    printf ']'
}

file_size_bytes=0
if stat -f%z "$INPUT_FILE" >/dev/null 2>&1; then
    file_size_bytes="$(stat -f%z "$INPUT_FILE" 2>/dev/null || echo 0)"
elif stat -c%s "$INPUT_FILE" >/dev/null 2>&1; then
    file_size_bytes="$(stat -c%s "$INPUT_FILE" 2>/dev/null || echo 0)"
fi

container="$(ffprobe -v error -show_entries format=format_name -of default=nokey=1:noprint_wrappers=1 "$INPUT_FILE" 2>/dev/null | head -n1 || true)"
container="${container%%,*}"
codec="$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of default=nokey=1:noprint_wrappers=1 "$INPUT_FILE" 2>/dev/null | head -n1 || true)"
duration_sec="$(ffprobe -v error -show_entries format=duration -of default=nokey=1:noprint_wrappers=1 "$INPUT_FILE" 2>/dev/null | head -n1 || true)"
channels="$(ffprobe -v error -select_streams a:0 -show_entries stream=channels -of default=nokey=1:noprint_wrappers=1 "$INPUT_FILE" 2>/dev/null | head -n1 || true)"
sample_rate="$(ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate -of default=nokey=1:noprint_wrappers=1 "$INPUT_FILE" 2>/dev/null | head -n1 || true)"
stream_count="$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$INPUT_FILE" 2>/dev/null | awk 'NF>0{c++} END{print c+0}' || echo 0)"

if ! is_number "$duration_sec"; then
    duration_sec=0
fi
if ! is_integer "$channels"; then
    channels=""
fi
if ! is_integer "$sample_rate"; then
    sample_rate=""
fi
if ! is_integer "$file_size_bytes"; then
    file_size_bytes=0
fi
if ! is_integer "$stream_count"; then
    stream_count=0
fi

volume_log="$(ffmpeg -hide_banner -loglevel info -t "$SCAN_SEC" -i "$INPUT_FILE" -af volumedetect -f null - 2>&1 || true)"
mean_volume_db="$(printf '%s\n' "$volume_log" | awk -F': ' '/mean_volume:/ {v=$2} END {gsub(/ dB/, "", v); gsub(/^[ \t]+|[ \t]+$/, "", v); print v}')"
max_volume_db="$(printf '%s\n' "$volume_log" | awk -F': ' '/max_volume:/ {v=$2} END {gsub(/ dB/, "", v); gsub(/^[ \t]+|[ \t]+$/, "", v); print v}')"
if ! is_number "$mean_volume_db"; then
    mean_volume_db=""
fi
if ! is_number "$max_volume_db"; then
    max_volume_db=""
fi

silence_log="$(ffmpeg -hide_banner -loglevel info -t "$SCAN_SEC" -i "$INPUT_FILE" -af silencedetect=noise=-40dB:d=3 -f null - 2>&1 || true)"
silence_total_sec="$(printf '%s\n' "$silence_log" | awk -F'silence_duration:' '/silence_duration:/ {v=$2; gsub(/^[ \t]+|[ \t]+$/, "", v); split(v,a," "); sum+=a[1]} END {printf "%.6f", sum+0}')"

silence_ratio=0
silence_base_sec=0
if is_number "$duration_sec" && float_gt "$duration_sec" 0; then
    silence_base_sec="$(awk -v d="$duration_sec" -v n="$SCAN_SEC" 'BEGIN { if (d < n) printf "%.6f", d; else printf "%.6f", n }')"
fi
if is_number "$silence_base_sec" && float_gt "$silence_base_sec" 0; then
    silence_ratio="$(awk -v s="$silence_total_sec" -v d="$silence_base_sec" 'BEGIN { r = (d > 0 ? s / d : 0); if (r < 0) r = 0; if (r > 1) r = 1; printf "%.6f", r }')"
fi

convertible=false
tmp_base="$(mktemp "${TMPDIR:-/tmp}/preflight_convert.XXXXXX")"
tmp_wav="${tmp_base}.wav"
rm -f -- "$tmp_wav"
if ffmpeg -hide_banner -loglevel error -t 5 -i "$INPUT_FILE" -ar 16000 -ac 1 -c:a pcm_s16le -y "$tmp_wav" >/dev/null 2>&1; then
    convertible=true
fi
rm -f -- "$tmp_base" "$tmp_wav"

reasons=()
corrections=()
verdict="OK"

if [ "$file_size_bytes" -le 0 ]; then
    reasons+=("file size is zero")
    verdict="NOT_RECOMMENDED"
fi

if [ "$stream_count" -le 0 ]; then
    reasons+=("no audio stream detected")
    verdict="NOT_RECOMMENDED"
fi

if is_number "$duration_sec" && float_le "$duration_sec" 0; then
    reasons+=("duration is zero")
    verdict="NOT_RECOMMENDED"
fi

if [ "$convertible" != "true" ]; then
    reasons+=("ffmpeg conversion test failed")
    verdict="NOT_RECOMMENDED"
fi

if is_number "$silence_ratio" && float_ge "$silence_ratio" 0.95; then
    reasons+=("silence ratio is too high ($(format_two_decimal "$silence_ratio"))")
    verdict="NOT_RECOMMENDED"
fi

if [ "$verdict" != "NOT_RECOMMENDED" ]; then
    needs_correction=false

    if [ -n "$mean_volume_db" ] && float_lt "$mean_volume_db" -35; then
        reasons+=("mean_volume below -35dB ($(format_one_decimal "$mean_volume_db")dB)")
        corrections+=("volume_normalize")
        needs_correction=true
    elif [ -z "$mean_volume_db" ]; then
        reasons+=("mean_volume unavailable")
        needs_correction=true
    fi

    if is_number "$duration_sec" && float_gt "$duration_sec" 3600; then
        duration_min="$(awk -v d="$duration_sec" 'BEGIN { printf "%d", int(d / 60 + 0.5) }')"
        reasons+=("duration exceeds 60min (${duration_min}min)")
        needs_correction=true
    fi

    if is_number "$silence_ratio" && float_ge "$silence_ratio" 0.8 && float_lt "$silence_ratio" 0.95; then
        reasons+=("silence ratio is high ($(format_two_decimal "$silence_ratio"))")
        needs_correction=true
    fi

    if [ "$needs_correction" = true ]; then
        verdict="NEEDS_CORRECTION"
    else
        verdict="OK"
    fi
fi

base_recommendation="x4"
if is_number "$duration_sec"; then
    if float_le "$duration_sec" 600; then
        base_recommendation="x1"
    elif float_le "$duration_sec" 1800; then
        if [ -n "$mean_volume_db" ] && float_ge "$mean_volume_db" -30; then
            base_recommendation="x1"
        else
            base_recommendation="x4"
        fi
    elif float_le "$duration_sec" 3600; then
        base_recommendation="x4"
    elif float_le "$duration_sec" 7200; then
        base_recommendation="x8"
    else
        base_recommendation="x16"
    fi
fi

apply_priority_bias() {
    local preset="$1"
    local bias="$2"

    if [ "$bias" = "accuracy" ]; then
        case "$preset" in
            x16) echo "x8" ;;
            x8) echo "x4" ;;
            x4) echo "x1" ;;
            *) echo "$preset" ;;
        esac
        return
    fi

    if [ "$bias" = "speed" ]; then
        case "$preset" in
            x1) echo "x4" ;;
            x4) echo "x8" ;;
            x8) echo "x16" ;;
            *) echo "$preset" ;;
        esac
        return
    fi

    echo "$preset"
}

recommended_preset="$(apply_priority_bias "$base_recommendation" "$PRIORITY")"

duration_min_text="$(awk -v d="$duration_sec" 'BEGIN { if (d > 0) printf "%d", int(d / 60 + 0.5); else printf "0" }')"
priority_label="balanced"
if [ "$PRIORITY" = "accuracy" ]; then
    priority_label="accuracy-first"
elif [ "$PRIORITY" = "speed" ]; then
    priority_label="speed-first"
fi

preset_reason="${duration_min_text}min audio: ${recommended_preset} for ${priority_label}"

printf '{'
printf '"verdict":"%s",' "$verdict"
printf '"reasons":%s,' "$(array_to_json "${reasons[@]-}")"
printf '"corrections":%s,' "$(array_to_json "${corrections[@]-}")"
printf '"input":{'
printf '"container":%s,' "$(json_str_or_null "$container")"
printf '"codec":%s,' "$(json_str_or_null "$codec")"
printf '"duration_sec":%s,' "$(json_num_or_null "$duration_sec")"
printf '"channels":%s,' "$(json_int_or_null "$channels")"
printf '"sample_rate":%s,' "$(json_int_or_null "$sample_rate")"
printf '"file_size_bytes":%s,' "$(json_int_or_null "$file_size_bytes")"
printf '"mean_volume_db":%s,' "$(json_num_or_null "$mean_volume_db")"
printf '"max_volume_db":%s,' "$(json_num_or_null "$max_volume_db")"
printf '"silence_ratio":%s,' "$(json_num_or_null "$silence_ratio")"
printf '"convertible":%s' "$convertible"
printf '},'
printf '"recommended_preset":"%s",' "$recommended_preset"
printf '"preset_reason":"%s"' "$(json_escape "$preset_reason")"
printf '}\n'
