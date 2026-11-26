#!/bin/bash
# Build script for the RS-485 Testbench tool.
# Uses the shared build_app.sh helper; the .neoapp lands in dist/.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCH="${1:-arm64}"
"${SCRIPT_DIR}/../../scripts/build_app.sh" "$SCRIPT_DIR" --arch "$ARCH" --output "$SCRIPT_DIR/dist"
