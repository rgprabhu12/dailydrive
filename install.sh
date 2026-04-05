#!/bin/bash
# =============================================================================
# Daily Drive — Quick Installer
# =============================================================================
# Run this once to set everything up on your machine.
#
# Usage:  chmod +x install.sh && ./install.sh
# =============================================================================

set -e

echo ""
echo "Daily Drive — Installer"
echo "=========================="
echo ""

# --- Check for Python 3 ---
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required but was not found."
    echo "Install it first, then rerun this installer."
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1)
echo "Python found: $PYTHON_VERSION"

# --- Install Python dependencies ---
echo ""
echo "Installing Python dependencies..."
python3 -m pip install --user -r requirements.txt
echo "Dependencies installed"

# --- Create config if needed ---
if [ ! -f config.yaml ]; then
    if [ -f config.example.yaml ]; then
        cp config.example.yaml config.yaml
        echo ""
        echo "Created config.yaml from template"
        echo "   Edit it now:  nano config.yaml"
    else
        echo ""
        echo "config.example.yaml not found, so config.yaml was not created automatically"
    fi
else
    echo ""
    echo "config.yaml already exists"
fi

echo ""
echo "=========================="
echo "Installation complete!"
echo "=========================="
echo ""
echo "Next steps:"
echo "  1. Create a Spotify app at https://developer.spotify.com/dashboard"
echo "     - Set redirect URI to: http://127.0.0.1:8888/callback"
echo "     - Enable Web API and Web Playback SDK"
echo "     - Add your Spotify email in Settings > User Management"
echo "  2. Edit config.yaml with your Spotify credentials and preferences"
echo "  3. Run: python3 setup.py          (one-time Spotify login)"
echo "  4. Run: python3 daily_drive.py    (build your playlist!)"
echo "  5. Optional: Set up auto-refresh (see README.md)"
echo ""
