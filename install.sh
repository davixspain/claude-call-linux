#!/usr/bin/env bash
# claude-call — one-shot installer.
# Run it directly:
#   curl -fsSL https://raw.githubusercontent.com/caiovicentino/claude-call/main/install.sh | bash
# or from a clone:
#   ./install.sh
#
# Installs: uv, ffmpeg, whisper.cpp, portaudio, the repo + python deps, a whisper model,
# and a global `claude-call` command. (Claude Code itself you install + log in once.)
set -euo pipefail

REPO_URL="https://github.com/caiovicentino/claude-call"
INSTALL_DIR="${CLAUDE_CALL_DIR:-$HOME/.claude-call}"
MODEL="${CLAUDE_CALL_MODEL:-small}"

say()  { printf "\033[1;36m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!!\033[0m  %s\n" "$*"; }
err()  { printf "\033[1;31mxx\033[0m  %s\n" "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

OS="$(uname -s)"
say "claude-call installer ($OS)"

# --- 1) uv (Python manager) ---
if ! have uv; then
  say "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# --- 2) system dependencies ---
case "$OS" in
  Darwin)
    if ! have brew; then
      err "Homebrew is required on macOS. Install it from https://brew.sh and re-run this."
      exit 1
    fi
    say "Installing ffmpeg, whisper-cpp, portaudio (brew)..."
    brew install ffmpeg whisper-cpp portaudio
    ;;
  Linux)
    if have apt-get; then
      say "Installing system deps (apt)..."
      sudo apt-get update -y
      sudo apt-get install -y ffmpeg portaudio19-dev build-essential cmake git curl
    elif have dnf; then
      say "Installing system deps (dnf)..."
      sudo dnf install -y ffmpeg portaudio-devel gcc-c++ cmake git curl
    elif have pacman; then
      say "Installing system deps (pacman)..."
      sudo pacman -Sy --noconfirm ffmpeg portaudio base-devel cmake git curl
    else
      warn "Unknown package manager — install ffmpeg, portaudio(-dev), cmake and build tools yourself."
    fi
    # whisper.cpp has no common package — build it if missing
    if ! have whisper-server && ! have whisper-cli; then
      say "Building whisper.cpp (one time)..."
      WC="$INSTALL_DIR/whisper.cpp"
      [ -d "$WC/.git" ] || git clone --depth 1 https://github.com/ggerganov/whisper.cpp "$WC"
      cmake -B "$WC/build" -S "$WC"
      cmake --build "$WC/build" -j --config Release
      mkdir -p "$HOME/.local/bin"
      ln -sf "$WC/build/bin/whisper-server" "$HOME/.local/bin/whisper-server"
      ln -sf "$WC/build/bin/whisper-cli"    "$HOME/.local/bin/whisper-cli"
    fi
    ;;
  *) warn "Unsupported OS '$OS' — you may need to install deps manually." ;;
esac

# --- 3) get the repo (use a local clone if we're inside one, else clone) ---
SRC="${BASH_SOURCE[0]:-}"
if [ -n "$SRC" ] && [ -f "$(cd "$(dirname "$SRC")" 2>/dev/null && pwd)/call.py" ]; then
  REPO_DIR="$(cd "$(dirname "$SRC")" && pwd)"
  say "Using local repo: $REPO_DIR"
else
  if [ -d "$INSTALL_DIR/.git" ]; then
    say "Updating claude-call in $INSTALL_DIR..."
    git -C "$INSTALL_DIR" pull --ff-only -q || true
  else
    say "Cloning claude-call into $INSTALL_DIR..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
  REPO_DIR="$INSTALL_DIR"
fi

# --- 4) python deps ---
say "Installing Python deps (uv sync)..."
( cd "$REPO_DIR" && uv sync )

# --- 5) whisper model ---
if [ ! -f "$HOME/.cache/whisper/ggml-${MODEL}.bin" ]; then
  say "Downloading whisper model ($MODEL)..."
  ( cd "$REPO_DIR" && ./scripts/download-model.sh "$MODEL" )
fi

# --- 6) install the global command (CLAUDE_CALL_BIN overrides the location) ---
BIN="${CLAUDE_CALL_BIN:-}"
if [ -z "$BIN" ]; then
  for d in /usr/local/bin "$HOME/.local/bin"; do
    if [ -d "$d" ] && [ -w "$d" ]; then BIN="$d"; break; fi
  done
  [ -z "$BIN" ] && BIN="$HOME/.local/bin"
fi
mkdir -p "$BIN"
ln -sf "$REPO_DIR/call.sh" "$BIN/claude-call"
say "Installed command: $BIN/claude-call"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) warn "Add $BIN to your PATH:  echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
esac

# --- 7) Claude Code (the brain) ---
if ! have claude; then
  warn "Claude Code isn't installed (it's the brain). Get it at https://docs.claude.com/claude-code"
  warn "Then run 'claude' once and log in. claude-call uses your subscription — no API key."
fi

# --- 8) verify + benchmark (so you see 'all green' right away) ---
if [ "${CLAUDE_CALL_NO_DOCTOR:-}" != "1" ]; then
  echo
  say "Verifying your setup..."
  ( cd "$REPO_DIR" && CALL_WHISPER_MODEL="$HOME/.cache/whisper/ggml-${MODEL}.bin" uv run python doctor.py ) || true
fi

echo
say "Done! 🎉  Start a call from any project you've used with Claude Code:"
echo "      cd ~/your-project && claude-call"
