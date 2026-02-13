#!/bin/bash
# benchmark_matrix.sh: Run matrix benchmarks for whisper-workflow and export CSV results.
# Usage: ./benchmark_matrix.sh <input_audio_file> [output_directory]

set -euo pipefail

INPUT_FILE="${1:-}"
OUTPUT_DIR="${2:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW_SCRIPT="$SCRIPT_DIR/transcribe_workflow.sh"

CPU_CORES="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
WARMUP_RUNS="${BENCHMARK_WARMUP_RUNS:-1}"
MEASURE_RUNS="${BENCHMARK_MEASURE_RUNS:-3}"
PARALLEL_JOBS="${BENCHMARK_JOBS:-2}"
SEGMENT_TIME="${BENCHMARK_SEGMENT_TIME:-60}"
LANGUAGE="${BENCHMARK_LANGUAGE:-${WHISPER_LANGUAGE:-ja}}"
RETRY_COUNT="${BENCHMARK_RETRY_COUNT:-${WHISPER_RETRY_COUNT:-2}}"
RETRY_BACKOFF_SEC="${BENCHMARK_RETRY_BACKOFF_SEC:-${WHISPER_RETRY_BACKOFF_SEC:-1}}"
THREADS_SINGLE="${BENCHMARK_THREADS_SINGLE:-$CPU_CORES}"
THREADS_PARALLEL="${BENCHMARK_THREADS_PARALLEL:-}"

PRESETS=(x1 x4 x8 x16)
MODES=(single parallel)

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

model_for_preset() {
    case "$1" in
        x1) echo "$HOME/.cache/whisper-cpp/ggml-large-v3.bin" ;;
        x4) echo "$HOME/.cache/whisper-cpp/ggml-medium.bin" ;;
        x8) echo "$HOME/.cache/whisper-cpp/ggml-small.bin" ;;
        x16) echo "$HOME/.cache/whisper-cpp/ggml-tiny.bin" ;;
        *) return 1 ;;
    esac
}

extract_seconds() {
    local logfile="$1"
    local label="$2"

    awk -F': ' -v target="$label" '$1 == target { gsub(/s$/, "", $2); print $2 }' "$logfile" | tail -n 1
}

extract_rtf() {
    local logfile="$1"
    awk -F': ' '/^RTF \(transcribe only\):/ { print $2 }' "$logfile" | tail -n 1
}

extract_x_realtime() {
    local logfile="$1"
    awk -F': ' '/^Speed \(transcribe only\):/ { val=$2; gsub(/x realtime$/, "", val); gsub(/^[ \t]+|[ \t]+$/, "", val); print val }' "$logfile" | tail -n 1
}

extract_peak_rss() {
    local logfile="$1"
    awk '/maximum resident set size/ { print $1 }' "$logfile" | tail -n 1
}

median_value() {
    local filtered=()
    local value

    for value in "$@"; do
        if [ -n "$value" ] && [ "$value" != "n/a" ]; then
            filtered+=("$value")
        fi
    done

    if [ "${#filtered[@]}" -eq 0 ]; then
        echo "n/a"
        return
    fi

    printf "%s\n" "${filtered[@]}" | sort -n | awk '
        { arr[NR] = $1 }
        END {
            if (NR % 2 == 1) {
                print arr[(NR + 1) / 2]
            } else {
                printf "%.6f", (arr[NR / 2] + arr[(NR / 2) + 1]) / 2
            }
        }
    '
}

compute_proxy_quality_metrics() {
    local text_file="$1"

    awk '
        function trim(s) {
            gsub(/^[ \t]+|[ \t]+$/, "", s)
            return s
        }

        /^\[[0-9]+:[0-9][0-9]\]$/ {
            if (in_segment && !segment_has_text) {
                empty_segment_count++
            }
            segment_count++
            in_segment = 1
            segment_has_text = 0
            next
        }

        {
            line = trim($0)
            if (line == "") {
                next
            }

            text_line_count++
            if (in_segment) {
                segment_has_text = 1
            }

            normalized = tolower(line)
            if (previous_text != "" && normalized == previous_text) {
                adjacent_duplicate_count++
            }
            previous_text = normalized
        }

        END {
            if (in_segment && !segment_has_text) {
                empty_segment_count++
            }

            if (segment_count > 0) {
                empty_rate = sprintf("%.4f", empty_segment_count / segment_count)
            } else {
                empty_rate = "n/a"
            }

            if (text_line_count > 1) {
                duplicate_rate = sprintf("%.4f", adjacent_duplicate_count / (text_line_count - 1))
            } else {
                duplicate_rate = "n/a"
            }

            printf "%s,%s,%d,%d,%d,%d\n", empty_rate, duplicate_rate, segment_count, empty_segment_count, text_line_count, adjacent_duplicate_count
        }
    ' "$text_file"
}

csv_escape() {
    local value="$1"
    value="${value//\"/\"\"}"
    printf '"%s"' "$value"
}

if [ -z "$INPUT_FILE" ]; then
    echo "Usage: $0 <input_audio_file> [output_directory]"
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    echo "Error: Input file '$INPUT_FILE' not found."
    exit 1
fi

if [ ! -x "$WORKFLOW_SCRIPT" ]; then
    echo "Error: transcribe_workflow.sh is missing or not executable: $WORKFLOW_SCRIPT"
    exit 1
fi

if ! is_positive_int "$WARMUP_RUNS"; then
    echo "Error: BENCHMARK_WARMUP_RUNS must be a positive integer: '$WARMUP_RUNS'"
    exit 1
fi

if ! is_positive_int "$MEASURE_RUNS"; then
    echo "Error: BENCHMARK_MEASURE_RUNS must be a positive integer: '$MEASURE_RUNS'"
    exit 1
fi

if ! is_positive_int "$PARALLEL_JOBS"; then
    echo "Error: BENCHMARK_JOBS must be a positive integer: '$PARALLEL_JOBS'"
    exit 1
fi

if ! is_positive_int "$THREADS_SINGLE"; then
    echo "Error: BENCHMARK_THREADS_SINGLE must be a positive integer: '$THREADS_SINGLE'"
    exit 1
fi

if [ -z "$THREADS_PARALLEL" ]; then
    threads_parallel_default=$((CPU_CORES / PARALLEL_JOBS))
    if [ "$threads_parallel_default" -lt 1 ]; then
        threads_parallel_default=1
    fi
    THREADS_PARALLEL="$threads_parallel_default"
fi

if ! is_positive_int "$THREADS_PARALLEL"; then
    echo "Error: BENCHMARK_THREADS_PARALLEL must be a positive integer: '$THREADS_PARALLEL'"
    exit 1
fi

if ! is_positive_int "$SEGMENT_TIME"; then
    echo "Error: BENCHMARK_SEGMENT_TIME must be a positive integer: '$SEGMENT_TIME'"
    exit 1
fi

if ! is_positive_int "$RETRY_COUNT"; then
    echo "Error: BENCHMARK_RETRY_COUNT must be a positive integer: '$RETRY_COUNT'"
    exit 1
fi

if ! is_non_negative_int "$RETRY_BACKOFF_SEC"; then
    echo "Error: BENCHMARK_RETRY_BACKOFF_SEC must be a non-negative integer: '$RETRY_BACKOFF_SEC'"
    exit 1
fi

for preset in "${PRESETS[@]}"; do
    model_path="$(model_for_preset "$preset")"
    if [ ! -f "$model_path" ]; then
        echo "Error: Required model for '$preset' is missing: $model_path"
        echo "Hint: ./install_models.sh ${preset}"
        exit 1
    fi
done

if [ -z "$OUTPUT_DIR" ]; then
    OUTPUT_DIR="$SCRIPT_DIR/benchmark_runs_$(date +%Y%m%d_%H%M%S)"
fi

mkdir -p "$OUTPUT_DIR"
RUNS_CSV="$OUTPUT_DIR/benchmark.csv"
MEDIAN_CSV="$OUTPUT_DIR/benchmark_median.csv"

cat > "$RUNS_CSV" <<CSV
run_timestamp,preset,mode,jobs,run_type,run_index,total_sec,transcribe_sec,rtf_transcribe,x_realtime_transcribe,peak_rss_bytes,empty_segment_rate,adjacent_duplicate_rate,segment_count,empty_segment_count,text_line_count,adjacent_duplicate_count,output_file,log_file
CSV

cat > "$MEDIAN_CSV" <<CSV
preset,mode,jobs,measure_runs,median_total_sec,median_transcribe_sec,median_rtf_transcribe,median_x_realtime_transcribe,median_peak_rss_bytes,median_empty_segment_rate,median_adjacent_duplicate_rate
CSV

echo "=== Benchmark Matrix ==="
echo "Input: $INPUT_FILE"
echo "Output dir: $OUTPUT_DIR"
echo "Warmup runs: $WARMUP_RUNS"
echo "Measure runs: $MEASURE_RUNS"
echo "Presets: ${PRESETS[*]}"
echo "Modes: ${MODES[*]}"
echo "Parallel jobs: $PARALLEL_JOBS"
echo "------------------------"

for preset in "${PRESETS[@]}"; do
    for mode in "${MODES[@]}"; do
        jobs="1"
        threads="$THREADS_SINGLE"
        if [ "$mode" = "parallel" ]; then
            jobs="$PARALLEL_JOBS"
            threads="$THREADS_PARALLEL"
            case_name="${preset}_${mode}_j${jobs}"
        else
            case_name="${preset}_${mode}"
        fi

        case_dir="$OUTPUT_DIR/$case_name"
        mkdir -p "$case_dir"

        total_values=()
        transcribe_values=()
        rtf_values=()
        x_values=()
        rss_values=()
        empty_rate_values=()
        dup_rate_values=()

        total_runs=$((WARMUP_RUNS + MEASURE_RUNS))
        for run_num in $(seq 1 "$total_runs"); do
            if [ "$run_num" -le "$WARMUP_RUNS" ]; then
                run_type="warmup"
                run_index="$run_num"
            else
                run_type="measure"
                run_index="$((run_num - WARMUP_RUNS))"
            fi

            output_file="$case_dir/${run_type}_${run_index}.txt"
            log_file="$case_dir/${run_type}_${run_index}.log"

            echo "[$case_name] ${run_type} ${run_index}/${MEASURE_RUNS} ..."

            (
                export WHISPER_LANGUAGE="$LANGUAGE"
                export WHISPER_SEGMENT_TIME="$SEGMENT_TIME"
                export WHISPER_RETRY_COUNT="$RETRY_COUNT"
                export WHISPER_RETRY_BACKOFF_SEC="$RETRY_BACKOFF_SEC"
                export WHISPER_THREADS="$threads"

                if [ "$mode" = "parallel" ]; then
                    export WHISPER_PRESET="custom"
                    export WHISPER_MODEL="$(model_for_preset "$preset")"
                    export WHISPER_JOBS="$jobs"
                else
                    export WHISPER_PRESET="$preset"
                    unset WHISPER_MODEL
                    export WHISPER_JOBS="1"
                fi

                /usr/bin/time -l "$WORKFLOW_SCRIPT" "$INPUT_FILE" "$output_file"
            ) > "$log_file" 2>&1

            total_sec="$(extract_seconds "$log_file" "Total time")"
            transcribe_sec="$(extract_seconds "$log_file" "Transcribe time")"
            rtf_transcribe="$(extract_rtf "$log_file")"
            x_realtime="$(extract_x_realtime "$log_file")"
            peak_rss="$(extract_peak_rss "$log_file")"
            quality_csv="$(compute_proxy_quality_metrics "$output_file")"

            IFS=',' read -r empty_rate dup_rate segment_count empty_segment_count text_line_count adjacent_duplicate_count <<EOF_METRICS
$quality_csv
EOF_METRICS

            run_timestamp="$(date +%Y-%m-%dT%H:%M:%S)"
            printf "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n" \
                "$run_timestamp" \
                "$preset" \
                "$mode" \
                "$jobs" \
                "$run_type" \
                "$run_index" \
                "${total_sec:-n/a}" \
                "${transcribe_sec:-n/a}" \
                "${rtf_transcribe:-n/a}" \
                "${x_realtime:-n/a}" \
                "${peak_rss:-n/a}" \
                "${empty_rate:-n/a}" \
                "${dup_rate:-n/a}" \
                "${segment_count:-0}" \
                "${empty_segment_count:-0}" \
                "${text_line_count:-0}" \
                "${adjacent_duplicate_count:-0}" \
                "$(csv_escape "$output_file")" \
                "$(csv_escape "$log_file")" \
                >> "$RUNS_CSV"

            if [ "$run_type" = "measure" ]; then
                total_values+=("${total_sec:-}")
                transcribe_values+=("${transcribe_sec:-}")
                rtf_values+=("${rtf_transcribe:-}")
                x_values+=("${x_realtime:-}")
                rss_values+=("${peak_rss:-}")
                empty_rate_values+=("${empty_rate:-}")
                dup_rate_values+=("${dup_rate:-}")
            fi
        done

        median_total="$(median_value "${total_values[@]}")"
        median_transcribe="$(median_value "${transcribe_values[@]}")"
        median_rtf="$(median_value "${rtf_values[@]}")"
        median_x="$(median_value "${x_values[@]}")"
        median_rss="$(median_value "${rss_values[@]}")"
        median_empty_rate="$(median_value "${empty_rate_values[@]}")"
        median_dup_rate="$(median_value "${dup_rate_values[@]}")"

        printf "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n" \
            "$preset" \
            "$mode" \
            "$jobs" \
            "$MEASURE_RUNS" \
            "$median_total" \
            "$median_transcribe" \
            "$median_rtf" \
            "$median_x" \
            "$median_rss" \
            "$median_empty_rate" \
            "$median_dup_rate" \
            >> "$MEDIAN_CSV"
    done
done

echo "Completed benchmark matrix."
echo "Per-run CSV: $RUNS_CSV"
echo "Median CSV:  $MEDIAN_CSV"
