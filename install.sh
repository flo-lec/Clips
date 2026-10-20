#!/usr/bin/env bash
# Installs Clips for the current user (the password is only asked for to install missing dependencies).
#   ./install.sh            installs the dependencies (Arch and derivatives) then Clips, enables the replay and the shortcuts
#                           (Alt+F10 save · Alt+F9 / Alt+F11 start / stop a recording)
#   ./install.sh --force    installs even if some dependencies are missing
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA/clips"
APPS="$DATA/applications"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
FORCE=0; [[ "${1:-}" == "--force" ]] && FORCE=1

say() { printf '\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- dependencies
ARCH_PKGS=(gpu-screen-recorder ffmpeg python python-gobject gtk4 libadwaita gst-plugins-base gst-plugins-good
           gst-plugins-bad gst-plugin-gtk4 gst-plugin-va libnotify gtk4-layer-shell)

check_deps() {
    missing=()
    for cmd in gpu-screen-recorder ffmpeg ffprobe python3 notify-send systemctl; do
        command -v "$cmd" >/dev/null || missing+=("$cmd")
    done
    python3 - <<'EOF' 2>/dev/null || missing+=("python-gobject + gtk4 + libadwaita + gstreamer (introspection)")
import gi
for ns, v in (("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0"), ("GstController", "1.0")):
    gi.require_version(ns, v)
from gi.repository import Adw, Gst, Gtk  # noqa
EOF
    for el in gtk4paintablesink qtdemux audiomixer decodebin volume; do
        gst-inspect-1.0 "$el" >/dev/null 2>&1 || missing+=("gstreamer:$el")
    done
}

check_deps
. /etc/os-release 2>/dev/null || true
case " ${ID:-} ${ID_LIKE:-} " in *" arch "*) IS_ARCH=1 ;; *) IS_ARCH=0 ;; esac

# Arch and derivatives (CachyOS, Manjaro, EndeavourOS…): everything is in the official repos, install directly.
if ((${#missing[@]})) && ((IS_ARCH)); then
    say "Installing dependencies (you will be asked for your password)…"
    sudo pacman -S --needed "${ARCH_PKGS[@]}" || say "Package installation failed."
    check_deps
fi

if ((${#missing[@]})); then
    say "Missing dependencies: ${missing[*]}"
    case " ${ID:-} ${ID_LIKE:-} " in
        *" arch "*)
            echo "  sudo pacman -S --needed ${ARCH_PKGS[*]}" ;;
        *" fedora "*|*" rhel "*)
            echo "  sudo dnf install ffmpeg python3-gobject gtk4 libadwaita gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugin-gtk4 libnotify"
            echo "  + gpu-screen-recorder: https://git.dec05eba.com/gpu-screen-recorder (COPR or Flatpak)" ;;
        *" debian "*|*" ubuntu "*)
            echo "  sudo apt install ffmpeg python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-gtk4 libnotify-bin"
            echo "  + gpu-screen-recorder: https://git.dec05eba.com/gpu-screen-recorder (build from source or Flatpak)" ;;
        *" opensuse"*|*" suse "*)
            echo "  sudo zypper install ffmpeg python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 gstreamer-plugins-good gstreamer-plugins-bad gstreamer-plugin-gtk4 libnotify-tools gpu-screen-recorder" ;;
        *) echo "  Install these packages with your distribution's package manager." ;;
    esac
    ((FORCE)) || { echo "Run again with --force to install anyway."; exit 1; }
fi

# ---------------------------------------------------------------- files
if [[ "$SRC" != "$DEST" ]]; then
    mkdir -p "$DEST"
    cp "$SRC"/*.py "$SRC"/save-clip "$SRC"/record-clip "$SRC"/install.sh "$SRC"/uninstall.sh "$DEST"/
    cp -r "$SRC"/icons "$DEST"/
fi
chmod +x "$DEST"/clips.py "$DEST"/recorder.py "$DEST"/on_save.py "$DEST"/popup.py "$DEST"/setup_shortcut.py "$DEST"/save-clip "$DEST"/record-clip \
         "$DEST"/install.sh "$DEST"/uninstall.sh
mkdir -p "$APPS" "$UNITS" "$HOME/.local/bin"
# App icon: PNG + SVG in the user's hicolor theme
for d in "$DEST"/icons/hicolor/*/apps; do
    size="$(basename "$(dirname "$d")")"
    mkdir -p "$DATA/icons/hicolor/$size/apps"
    cp "$d"/local.clips.Clips.* "$DATA/icons/hicolor/$size/apps/" 2>/dev/null || true
done
ln -sf "$DEST/clips.py" "$HOME/.local/bin/clips"

cat > "$APPS/local.clips.Clips.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Clips
GenericName=Game clips
Comment=Replay, editing and sharing of clips (Moments-style)
Exec=$DEST/clips.py
Icon=$DATA/icons/hicolor/256x256/apps/local.clips.Clips.png
Terminal=false
Categories=AudioVideo;Video;Recorder;
StartupWMClass=local.clips.Clips
EOF

cat > "$UNITS/clips-recorder.service" <<'EOF'
[Unit]
Description=Clips — continuous replay (gpu-screen-recorder)
After=graphical-session.target pipewire.service
PartOf=graphical-session.target

[Service]
ExecStart=/usr/bin/env python3 %h/.local/share/clips/recorder.py
Restart=on-failure
RestartSec=5
Nice=5

[Install]
WantedBy=graphical-session.target
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true

# ---------------------------------------------------------------- replay + shortcut
systemctl --user daemon-reload
systemctl --user enable clips-recorder.service >/dev/null 2>&1
systemctl --user restart clips-recorder.service && say "Replay running." || say "The replay did not start: journalctl --user -u clips-recorder"
python3 "$DEST/setup_shortcut.py" || true
say "Clips is installed. Launch it from the applications menu or with the \"clips\" command."
