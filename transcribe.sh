#!/bin/bash
# whisper-cpp 一括文字起こしスクリプト
# Usage: ./transcribe.sh <segments_dir> <output_file>

set -e

SEGMENTS_DIR="${1:-.}"
OUTPUT_FILE="${2:-transcription.txt}"
MODEL="${WHISPER_MODEL:-$HOME/.cache/whisper-cpp/ggml-large-v3.bin}"

if [ ! -f "$MODEL" ]; then
    echo "Error: モデルファイルが見つかりません: $MODEL"
    echo "以下のコマンドでダウンロードしてください:"
    echo "  curl -L -o ~/.cache/whisper-cpp/ggml-large-v3.bin \\"
    echo "    'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin'"
    exit 1
fi

if [ ! -d "$SEGMENTS_DIR" ]; then
    echo "Error: ディレクトリが見つかりません: $SEGMENTS_DIR"
    exit 1
fi

echo "モデル: $MODEL"
echo "入力: $SEGMENTS_DIR"
echo "出力: $OUTPUT_FILE"
echo "---"

> "$OUTPUT_FILE"

for segment in "$SEGMENTS_DIR"/segment_*.wav; do
    [ -f "$segment" ] || continue

    seg_name=$(basename "$segment" .wav)
    seg_num=${seg_name#segment_}
    start_min=$((10#$seg_num))

    echo "Processing: $seg_name (${start_min}分〜)..."

    result=$(whisper-cli -l ja -m "$MODEL" -nt --no-prints "$segment" 2>/dev/null || echo "")

    if [ -n "$result" ]; then
        echo "[${start_min}:00]" >> "$OUTPUT_FILE"
        echo "$result" >> "$OUTPUT_FILE"
        echo "" >> "$OUTPUT_FILE"
    fi
done

echo "---"
echo "完了: $OUTPUT_FILE"
