#!/bin/bash
# Install whisper.cpp model files for workflow presets.
# Usage:
#   ./install_models.sh                # install x1/x4/x8/x16
#   ./install_models.sh x4 x8          # install selected presets
#   ./install_models.sh all --force    # reinstall all presets

set -euo pipefail

CACHE_DIR="${WHISPER_MODEL_DIR:-$HOME/.cache/whisper-cpp}"
BASE_URL="${WHISPER_MODEL_BASE_URL:-https://huggingface.co/ggerganov/whisper.cpp/resolve/main}"
FORCE=0

usage() {
    cat <<'EOF'
Usage: ./install_models.sh [x1|x4|x8|x16|all] [--force]

Examples:
  ./install_models.sh
  ./install_models.sh x4 x8
  ./install_models.sh all --force
EOF
}

model_name_for_preset() {
    case "$1" in
        x1) echo "ggml-large-v3.bin" ;;
        x4) echo "ggml-medium.bin" ;;
        x8) echo "ggml-small.bin" ;;
        x16) echo "ggml-tiny.bin" ;;
        *) return 1 ;;
    esac
}

download_model() {
    local preset="$1"
    local model_name="$2"
    local target="$CACHE_DIR/$model_name"
    local url="$BASE_URL/$model_name"
    local tmp_file="${target}.part"

    if [ -f "$target" ] && [ "$FORCE" -eq 0 ]; then
        echo "Skip [$preset]: $model_name (already exists)"
        return 0
    fi

    echo "Download [$preset]: $model_name"
    curl -fL --progress-bar -o "$tmp_file" "$url"
    mv -f "$tmp_file" "$target"
    echo "Saved: $target"
}

if ! command -v curl >/dev/null 2>&1; then
    echo "Error: curl is not installed."
    exit 1
fi

mkdir -p "$CACHE_DIR"

declare -a presets=()
if [ "$#" -eq 0 ]; then
    presets=(x1 x4 x8 x16)
else
    for arg in "$@"; do
        case "$arg" in
            --force)
                FORCE=1
                ;;
            all)
                presets=(x1 x4 x8 x16)
                ;;
            x1|x4|x8|x16)
                presets+=("$arg")
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                echo "Error: Unknown argument '$arg'"
                usage
                exit 1
                ;;
        esac
    done
fi

if [ "${#presets[@]}" -eq 0 ]; then
    echo "Error: No presets selected."
    usage
    exit 1
fi

# Deduplicate while preserving order (bash 3.2 compatible).
declare -a unique_presets=()
for preset in "${presets[@]}"; do
    already_added=0
    for added in "${unique_presets[@]-}"; do
        if [ "$added" = "$preset" ]; then
            already_added=1
            break
        fi
    done
    if [ "$already_added" -eq 0 ]; then
        unique_presets+=("$preset")
    fi
done

for preset in "${unique_presets[@]}"; do
    model_name="$(model_name_for_preset "$preset")" || {
        echo "Error: Unsupported preset '$preset'"
        exit 1
    }
    download_model "$preset" "$model_name"
done

echo "Model installation completed."
