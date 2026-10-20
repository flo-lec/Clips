#!/usr/bin/env bash
# Installe Clips pour l'utilisateur courant (le mot de passe n'est demandé que pour installer les dépendances manquantes).
#   ./install.sh            installe les dépendances (Arch et dérivées) puis Clips, active le replay et les raccourcis
#                           (Alt+F10 sauvegarder · Alt+F9 / Alt+F11 démarrer / arrêter un enregistrement)
#   ./install.sh --force    installe même s'il manque des dépendances
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA/clips"
APPS="$DATA/applications"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
FORCE=0; [[ "${1:-}" == "--force" ]] && FORCE=1

say() { printf '\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- dépendances
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

# Arch et dérivées (CachyOS, Manjaro, EndeavourOS…) : tout est dans les dépôts officiels, on installe directement.
if ((${#missing[@]})) && ((IS_ARCH)); then
    say "Installation des dépendances (ton mot de passe va être demandé)…"
    sudo pacman -S --needed "${ARCH_PKGS[@]}" || say "L'installation des paquets a échoué."
    check_deps
fi

if ((${#missing[@]})); then
    say "Dépendances manquantes : ${missing[*]}"
    case " ${ID:-} ${ID_LIKE:-} " in
        *" arch "*)
            echo "  sudo pacman -S --needed ${ARCH_PKGS[*]}" ;;
        *" fedora "*|*" rhel "*)
            echo "  sudo dnf install ffmpeg python3-gobject gtk4 libadwaita gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugin-gtk4 libnotify"
            echo "  + gpu-screen-recorder : https://git.dec05eba.com/gpu-screen-recorder (COPR ou Flatpak)" ;;
        *" debian "*|*" ubuntu "*)
            echo "  sudo apt install ffmpeg python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-gtk4 libnotify-bin"
            echo "  + gpu-screen-recorder : https://git.dec05eba.com/gpu-screen-recorder (compilation ou Flatpak)" ;;
        *" opensuse"*|*" suse "*)
            echo "  sudo zypper install ffmpeg python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 gstreamer-plugins-good gstreamer-plugins-bad gstreamer-plugin-gtk4 libnotify-tools gpu-screen-recorder" ;;
        *) echo "  Installe ces paquets avec le gestionnaire de ta distribution." ;;
    esac
    ((FORCE)) || { echo "Relance avec --force pour installer quand même."; exit 1; }
fi

# ---------------------------------------------------------------- fichiers
if [[ "$SRC" != "$DEST" ]]; then
    mkdir -p "$DEST"
    cp "$SRC"/*.py "$SRC"/save-clip "$SRC"/record-clip "$SRC"/install.sh "$SRC"/uninstall.sh "$DEST"/
    cp -r "$SRC"/icons "$DEST"/
fi
chmod +x "$DEST"/clips.py "$DEST"/recorder.py "$DEST"/on_save.py "$DEST"/popup.py "$DEST"/setup_shortcut.py "$DEST"/save-clip "$DEST"/record-clip \
         "$DEST"/install.sh "$DEST"/uninstall.sh
mkdir -p "$APPS" "$UNITS" "$HOME/.local/bin"
# Icône de l'appli : PNG + SVG dans le thème hicolor de l'utilisateur
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
GenericName=Clips de jeu
Comment=Replay, édition et partage de clips (façon Moments)
Exec=$DEST/clips.py
Icon=$DATA/icons/hicolor/256x256/apps/local.clips.Clips.png
Terminal=false
Categories=AudioVideo;Video;Recorder;
StartupWMClass=local.clips.Clips
EOF

cat > "$UNITS/clips-recorder.service" <<'EOF'
[Unit]
Description=Clips — replay en continu (gpu-screen-recorder)
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

# ---------------------------------------------------------------- replay + raccourci
systemctl --user daemon-reload
systemctl --user enable clips-recorder.service >/dev/null 2>&1
systemctl --user restart clips-recorder.service && say "Replay actif." || say "Le replay n'a pas démarré : journalctl --user -u clips-recorder"
python3 "$DEST/setup_shortcut.py" || true
say "Clips est installé. Lance-le depuis le menu des applications ou avec la commande « clips »."
