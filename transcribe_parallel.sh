#!/bin/bash
# Parallel transcription script using whisper-cli
# Usage: ./transcribe_parallel.sh <segments_dir> <output_file>

set -e

SEGMENTS_DIR="${1:-segments}"
OUTPUT_FILE="${2:-result_parallel.txt}"
MODEL="${WHISPER_MODEL:-$HOME/.cache/whisper-cpp/ggml-large-v3.bin}"
JOBS=4  # Adjust based on memory (large-v3 needs ~3-4GB per instance)

mkdir -p "${SEGMENTS_DIR}_txt"

echo "Starting parallel transcription with $JOBS jobs..."
echo "Model: $MODEL"
echo "Input: $SEGMENTS_DIR"
echo "Output: $OUTPUT_FILE"
echo "---"

export MODEL
export SEGMENTS_DIR

find "$SEGMENTS_DIR" -name "segment_*.wav" | sort | \
xargs -P "$JOBS" -I {} sh -c '
    file="$1"
    filename=$(basename "$file" .wav)
    txtfile="${SEGMENTS_DIR}_txt/${filename}.txt"

    if [ -f "$txtfile" ]; then
        echo "Skipping $filename (already done)"
        exit 0
    fi

    echo "Processing $filename..."

    # Calculate timestamp from filename (segment_005 -> 5)
    seg_num=${filename#segment_}
    start_min=$((10#$seg_num))

    # Transcribe
    result=$(whisper-cli -l ja -m "$MODEL" -nt --no-prints "$file" 2>/dev/null || echo "")

    if [ -n "$result" ]; then
        echo "[$start_min:00]" > "$txtfile"
        echo "$result" >> "$txtfile"
        echo "" >> "$txtfile"
    fi
' _ {}

echo "---"
echo "Concatenating results..."
> "$OUTPUT_FILE"
for txt in $(find "${SEGMENTS_DIR}_txt" -name "segment_*.txt" | sort); do
    cat "$txt" >> "$OUTPUT_FILE"
done

echo "Done! Saved to $OUTPUT_FILE"
