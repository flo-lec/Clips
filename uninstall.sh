#!/usr/bin/env bash
# Uninstalls Clips. Recorded clips and the configuration are kept,
# except with --purge (also deletes ~/.config/clips and the cache, never the videos).
set -uo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA/clips"

systemctl --user disable --now clips-recorder.service 2>/dev/null
python3 "$DEST/setup_shortcut.py" --remove 2>/dev/null
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/clips-recorder.service" \
      "$DATA/applications/local.clips.Clips.desktop" "$HOME/.local/bin/clips"
systemctl --user daemon-reload
if [[ "${1:-}" == "--purge" ]]; then
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/clips" "${XDG_CACHE_HOME:-$HOME/.cache}/clips"
fi
rm -rf "$DEST"
echo "Clips is uninstalled. Your clips stay in your Videos folder."
