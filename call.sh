#!/bin/bash
# claude-call — start a voice call with your Claude Code session.
# Run it from inside the project whose session you want to resume.
set -e

# Resolve the real script dir (works through symlinks, e.g. /usr/local/bin/claude-call)
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"; SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

# Subcommands
if [ "${1:-}" = "config" ]; then
  cd "$SCRIPT_DIR"; [ -f .env ] || cp .env.example .env
  exec uv run python configure.py
fi
if [ "${1:-}" = "doctor" ]; then
  cd "$SCRIPT_DIR"
  exec uv run python doctor.py
fi
if [ "${1:-}" = "transcripts" ]; then
  cd "$SCRIPT_DIR"
  if [ ! -d transcripts ] || [ -z "$(ls -A transcripts 2>/dev/null)" ]; then
    echo "Nenhum transcript ainda — faça uma call primeiro."; exit 0
  fi
  echo "📄 Transcripts (mais recentes primeiro) em $SCRIPT_DIR/transcripts:"
  ls -t transcripts/*.md 2>/dev/null | while IFS= read -r f; do
    first=$(grep -m1 -A1 '^\*\*Você\*\*' "$f" 2>/dev/null | tail -1 | cut -c1-60)
    printf "  %s   %s\n" "$(basename "$f" .md)" "$first"
  done
  if [ "${2:-}" = "last" ]; then exec ${PAGER:-less} "$(ls -t transcripts/*.md | head -1)"; fi
  [ "$(uname)" = "Darwin" ] && open transcripts/ 2>/dev/null
  echo "  (aberto a pasta · 'claude-call transcripts last' abre o último no terminal)"
  exit 0
fi

# The session to resume = the directory you called from
export CALL_CWD="${CALL_CWD:-$PWD}"

cd "$SCRIPT_DIR"
[ -f .env ] || cp .env.example .env
./build.sh 2>/dev/null || true        # compila o aecbridge se for usar CALL_AEC=1

echo "📞 claude-call — resuming the Claude Code session in: $CALL_CWD"
exec uv run python call.py
