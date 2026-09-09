#!/bin/bash
# Downloads a whisper.cpp ggml model into ~/.cache/whisper/
# Usage: ./scripts/download-model.sh [tiny|base|small|medium|large-v3-turbo]   (default: small)
set -e
MODEL="${1:-small}"
DEST="$HOME/.cache/whisper"
FILE="ggml-${MODEL}.bin"
URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/${FILE}"

mkdir -p "$DEST"
if [ -f "$DEST/$FILE" ]; then
  echo "already have $DEST/$FILE"
  exit 0
fi
echo "downloading $FILE ..."
curl -L --fail -o "$DEST/$FILE" "$URL"
echo "saved to $DEST/$FILE"
echo "set CALL_WHISPER_MODEL=$DEST/$FILE  (or it's the default for 'small')"
