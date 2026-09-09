#!/bin/bash
# Compiles the optional macOS AEC bridge (only used with CALL_AEC=1).
cd "$(dirname "$0")"
if ! command -v swiftc >/dev/null 2>&1; then
  exit 0   # not macOS / no Swift — fine, AEC is optional
fi
if [ ! -f aecbridge ] || [ aec_bridge.swift -nt aecbridge ]; then
  echo "compiling aecbridge (macOS AEC)..."
  swiftc -O aec_bridge.swift -o aecbridge && echo "aecbridge ok" || echo "aecbridge build failed (AEC optional)"
fi
