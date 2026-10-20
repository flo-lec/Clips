#!/usr/bin/env python3
"""Installs Clips' global shortcuts depending on the desktop environment.

  Alt+F10  Save a clip (the last seconds of the replay)
  Alt+F9   Start a recording
  Alt+F11  Stop the recording

KDE Plasma: kglobalaccel (active right away and after a reboot)
GNOME / Cinnamon / Budgie / Unity: gsettings custom shortcuts
Others (Hyprland, Sway, i3, XFCE…): prints the lines to add yourself.

Usage: setup_shortcut.py [--remove]   Exit code 0 = installed, 2 = manual action required.
"""
import ast
import os
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
from clips_common import SHORTCUTS, shortcut_label  # noqa: E402

RECORD = os.path.join(APP_DIR, "record-clip")
# (SHORTCUTS key, identifier, display name, command, F key for KDE)
ACTIONS = [
    ("save", "clips-save", "Save a clip", os.path.join(APP_DIR, "save-clip"), 10),
    ("rec_start", "clips-rec-start", "Start a recording", f"{RECORD} start", 9),
    ("rec_stop", "clips-rec-stop", "Stop a recording", f"{RECORD} stop", 11),
]
GNOME_ROOT = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
QT_ALT, QT_F1 = 0x08000000, 0x01000030   # Qt codes: Alt modifier, F1 key (F2 = F1 + 1…)


def desktop():
    return os.environ.get("XDG_CURRENT_DESKTOP", "").lower()


def kde(remove=False):
    from gi.repository import Gio, GLib
    apps = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "applications")
    os.makedirs(apps, exist_ok=True)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)

    def call(method, sig, args):
        return bus.call_sync("org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel", method,
                             GLib.Variant(sig, args), None, 0, -1, None)

    for _key, ident, name, cmd, fkey in ACTIONS:
        desktop_id = f"net.local.{ident}.desktop"
        path = os.path.join(apps, desktop_id)
        action = [desktop_id, "_launch", name, name]
        if remove:
            call("setForeignShortcut", "(asai)", (action, []))
            call("unRegister", "(as)", (action,))
            if os.path.exists(path):
                os.remove(path)
            continue
        with open(path, "w") as f:
            f.write(f"[Desktop Entry]\nExec={cmd}\nName={name}\nNoDisplay=true\nStartupNotify=false\n"
                    "Type=Application\nX-KDE-GlobalAccel-CommandShortcut=true\n")
        call("doRegister", "(as)", (action,))
        # SetPresent (2) | NoAutoloading (4): without SetPresent, KDE keeps the shortcut inactive.
        call("setShortcut", "(asaiu)", (action, [QT_ALT | (QT_F1 + fkey - 1)], 2 | 4))
    if remove:
        return "KDE shortcuts removed."
    return (f"Shortcuts installed in KDE: {summary()}.\n"
            "System Settings → Shortcuts to change them.")


def gnome(remove=False):
    schema = "org.gnome.settings-daemon.plugins.media-keys"
    cur = subprocess.run(["gsettings", "get", schema, "custom-keybindings"], capture_output=True, text=True).stdout.strip()
    lst = [] if cur.startswith("@") else ast.literal_eval(cur or "[]")
    ours = [f"{GNOME_ROOT}{ident}/" for _k, ident, *_ in ACTIONS]
    lst = [p for p in lst if p not in ours] + ([] if remove else ours)
    subprocess.run(["gsettings", "set", schema, "custom-keybindings", str(lst)], check=True)
    if remove:
        return "GNOME shortcuts removed."
    for (key, ident, name, cmd, _f), path in zip(ACTIONS, ours):
        kb = f"{schema}.custom-keybinding:{path}"
        for k, v in (("name", name), ("command", cmd), ("binding", SHORTCUTS[key])):
            subprocess.run(["gsettings", "set", kb, k, v], check=True)
    return f"Shortcuts installed in GNOME: {summary()}.\nSettings → Keyboard to change them."


def summary():
    return ", ".join(f"{shortcut_label(key)} {name.lower()}" for key, _i, name, _c, _f in ACTIONS)


def manual():
    d = desktop()
    lines = []
    for key, _i, name, cmd, fkey in ACTIONS:
        if "hyprland" in d:
            lines.append(f"bind = ALT, F{fkey}, exec, {cmd}      # {name}")
        elif "sway" in d or "i3" in d:
            lines.append(f"bindsym Mod1+F{fkey} exec {cmd}      # {name}")
        else:
            lines.append(f"{shortcut_label(key)} → {cmd}      ({name})")
    where = ("to ~/.config/hypr/hyprland.conf" if "hyprland" in d else
             "to your sway/i3 config" if ("sway" in d or "i3" in d) else "to your desktop's shortcuts")
    return f"Shortcuts to add {where}:\n  " + "\n  ".join(lines)


def main():
    remove = "--remove" in sys.argv
    d = desktop()
    try:
        if "kde" in d or "plasma" in d:
            print(kde(remove))
            return 0
        if any(x in d for x in ("gnome", "unity", "budgie", "pantheon")):
            print(gnome(remove))
            return 0
    except Exception as e:  # noqa: BLE001 — fall back to the manual explanation
        print(f"Automatic setup failed ({e}).")
    if not remove:
        print(manual())
    return 2


if __name__ == "__main__":
    sys.exit(main())
