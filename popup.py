#!/usr/bin/env python3
"""Encart « Clip enregistré » affiché par-dessus le jeu (couche « overlay » de Wayland, gtk4-layer-shell).

Ne prend jamais le focus clavier et laisse passer les clics : le jeu n'est pas dérangé.
Usage : popup.py <chemin du clip> [jeu] [regular|replay]
        popup.py --message <titre> [sous-titre]     (ex. « Enregistrement lancé »)
"""
import os
import sys
from ctypes import CDLL

# gtk4-layer-shell doit être chargé avant GTK.
CDLL("libgtk4-layer-shell.so")

import cairo  # noqa: E402
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clips_common import game_art, load_clip_info, load_config, steam_appid  # noqa: E402

SHOW_MS = 2600
CSS = b"""
window { background: transparent; }
.clip-popup { background: rgba(22, 22, 26, 0.93); color: white; border-radius: 16px; padding: 12px 18px 12px 12px;
              box-shadow: 0 4px 18px rgba(0, 0, 0, 0.45); border: 1px solid rgba(255, 255, 255, 0.08); }
.clip-popup .title { font-weight: 800; font-size: 1.05em; }
.clip-popup .sub { opacity: 0.75; font-size: 0.9em; }
.clip-popup .check { background: #2ec27e; color: white; border-radius: 999px; padding: 6px; }
.clip-popup .rec { background: #e01b24; border-radius: 999px; min-width: 14px; min-height: 14px; margin: 8px; }
.clip-popup picture { border-radius: 8px; }
"""


def duration_label(path):
    try:
        import subprocess
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        d = float(out)
        return f"{int(d // 60)}:{int(d % 60):02d}"
    except (ValueError, OSError, Exception):  # noqa: BLE001
        return None


def main():
    message = len(sys.argv) > 1 and sys.argv[1] == "--message"
    if message:   # simple encart texte (enregistrement manuel lancé…), pastille rouge
        path, game, kind = "", None, None
        title, sub = sys.argv[2] if len(sys.argv) > 2 else "", sys.argv[3] if len(sys.argv) > 3 else ""
    else:
        path = sys.argv[1] if len(sys.argv) > 1 else ""
        game = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
        kind = sys.argv[3] if len(sys.argv) > 3 else "replay"
        title = "Enregistrement sauvegardé" if kind == "regular" else "Clip enregistré"
        sub = None
    cfg = load_config()
    info = load_clip_info(path) if path else {}
    appid = info.get("appid") or (steam_appid(game) if game else None)

    Gtk.init()
    css = Gtk.CssProvider()
    css.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    win = Gtk.Window(title="Clips")
    LayerShell.init_for_window(win)
    LayerShell.set_namespace(win, "clips-popup")
    LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
    LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)   # jamais de focus
    LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
    LayerShell.set_anchor(win, LayerShell.Edge.RIGHT, True)
    LayerShell.set_margin(win, LayerShell.Edge.TOP, 28)
    LayerShell.set_margin(win, LayerShell.Edge.RIGHT, 28)
    monitors = Gdk.Display.get_default().get_monitors()
    for i in range(monitors.get_n_items()):
        m = monitors.get_item(i)
        if m.get_connector() == cfg.get("monitor"):   # l'écran filmé (celui du jeu)
            LayerShell.set_monitor(win, m)

    box = Gtk.Box(spacing=12, css_classes=["clip-popup"])
    icon_path = game_art(appid).get("icon") if appid else None
    if message:
        box.append(Gtk.Box(css_classes=["rec"], valign=Gtk.Align.CENTER))
    elif icon_path:
        box.append(Gtk.Picture(paintable=Gdk.Texture.new_from_filename(icon_path), width_request=44,
                               height_request=44, can_shrink=True, valign=Gtk.Align.CENTER))
    else:
        box.append(Gtk.Image(icon_name="object-select-symbolic", pixel_size=20, css_classes=["check"],
                             valign=Gtk.Align.CENTER))
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
    text.append(Gtk.Label(label=title, xalign=0, css_classes=["title"]))
    parts = [sub] if message else [p for p in (game, duration_label(path)) if p]
    if parts:
        text.append(Gtk.Label(label=" · ".join(parts), xalign=0, css_classes=["sub"]))
    box.append(text)
    win.set_child(box)

    def click_through(*_):
        surface = win.get_surface()
        if surface:
            surface.set_input_region(cairo.Region())   # les clics traversent l'encart

    win.connect("realize", click_through)
    win.set_opacity(0)
    win.present()

    loop = GLib.MainLoop()
    state = {"t": 0}

    def fade():
        state["t"] += 16
        t = state["t"]
        if t < 160:
            win.set_opacity(t / 160)
        elif t < SHOW_MS:
            win.set_opacity(1)
        elif t < SHOW_MS + 300:
            win.set_opacity(1 - (t - SHOW_MS) / 300)
        else:
            win.destroy()
            loop.quit()
            return False
        return True

    GLib.timeout_add(16, fade)
    loop.run()


if __name__ == "__main__":
    main()
