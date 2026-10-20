#!/usr/bin/env python3
"""Clips — replay, library, editor and sharing of game clips (à la SteelSeries Moments).

Modules: clips_common (config) · media (ffmpeg/GStreamer) · widgets (GPU drawing) · editor · prefs.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# GTK rendering with OpenGL: with a 60 fps video, the Vulkan renderer costs ~6× more CPU
# (measured: ~50 % of a core vs ~8 %). Can still be overridden through the environment variable.
os.environ.setdefault("GSK_RENDERER", "gl")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gst, Gtk  # noqa: E402

from clips_common import (APP_DIR, CACHE_DIR, SERVICE, clip_angles, clip_screens, companion_files,  # noqa: E402
                          exports_dir, game_art, load_clip_info, load_config,
                          manual_recording_path, manual_recording_started, meta_path, rename_clip, save_config,
                          set_clip_favorite, set_clip_game, shortcut_label, steam_appid, steam_installed_games)
from editor import EditorPage, clip_tracks, share_gains  # noqa: E402
from media import (SHARE, ENCODER_DETECTOR, JOBS, Exporter, Player, cache_key, probe, run_async,  # noqa: E402
                   video_plan)
from prefs import Preferences  # noqa: E402
from widgets import PlayGlyph, file_content_provider, fmt_size, open_uri, show_in_folder, texture  # noqa: E402

APP_ID = "local.clips.Clips"
VIDEO_EXT = (".mp4", ".mkv", ".mov")
THUMB_DIR = os.path.join(CACHE_DIR, "thumbs")
SHORTCUT_LABEL = shortcut_label("save")

CSS = """
.clip-video { background: black; border-radius: 12px; }
.clip-video.fullscreen { border-radius: 0; }
.fs-bar { border-radius: 16px; padding: 6px 14px; transition: opacity 250ms; }
.fs-title { border-radius: 12px; padding: 6px 12px; transition: opacity 250ms; }
.clip-card { border-radius: 12px; padding: 6px; transition: background 150ms; }
.clip-card:hover { background: alpha(currentColor, 0.07); }
.clip-card picture { border-radius: 8px; }
.duration-badge { background: alpha(black, 0.72); color: white; border-radius: 6px;
                  padding: 1px 6px; margin: 6px; font-size: 0.85em; font-weight: bold; }
.play-hover { color: white; background: alpha(black, 0.45); border-radius: 999px; padding: 10px;
              opacity: 0; transition: opacity 150ms; }
.clip-card:hover .play-hover { opacity: 1; }
.big-play { min-width: 78px; min-height: 78px; padding: 0; border-radius: 999px;
            background: alpha(black, 0.48); border: 1.5px solid alpha(white, 0.22);
            box-shadow: 0 8px 28px alpha(black, 0.35); transition: background 160ms, border-color 160ms; }
.big-play:hover { background: alpha(@accent_bg_color, 0.92); border-color: alpha(white, 0.35); }
.big-play:active { background: @accent_bg_color; }
.replay-pill { border-radius: 999px; padding: 2px 4px 2px 10px; background: alpha(currentColor, 0.08); }
.rec-dot { min-width: 9px; min-height: 9px; border-radius: 999px; background: alpha(currentColor, 0.35); }
.rec-dot.on { background: #e01b24; animation: pulse 1.6s ease-in-out infinite; }
.rec-veil { background: alpha(black, 0.62); border-radius: 8px; color: white; }
.clip-card.recording picture { filter: grayscale(1) brightness(0.6); }
.manual-rec-pill { border-radius: 999px; padding: 2px 2px 2px 10px; background: alpha(#e01b24, 0.18); }
@keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.35; } 100% { opacity: 1; } }
.share-tile { padding: 10px; }
.share-tile picture { border-radius: 6px; }
.share-tile.disabled { opacity: 0.5; }
.track-row { padding: 3px 10px; }
.game-icon { border-radius: 4px; }
.track-dot { min-width: 4px; border-radius: 4px; }
.kind-game { background: rgb(54,133,227); }
.kind-chat { background: rgb(145,97,235); }
.kind-mic { background: rgb(46,194,126); }
.kind-app0 { background: rgb(245,120,0); }
.kind-app1 { background: rgb(224,61,140); }
.kind-app2 { background: rgb(28,179,191); }
.kind-app3 { background: rgb(237,194,33); }
.kind-app4 { background: rgb(158,115,77); }
.kind-other { background: rgb(230,166,10); }
.game-chip { background: alpha(black, 0.72); color: white; border-radius: 6px; padding: 2px 7px 2px 3px; margin: 6px;
             font-size: 0.8em; font-weight: bold; }
.game-chip picture { border-radius: 3px; }
.filter-bar { padding: 6px 6px 0 6px; }
.hero { border-radius: 16px; }
.hero-fade { background-image: linear-gradient(to top, alpha(black, 0.78), alpha(black, 0.05) 75%),
                               linear-gradient(to right, alpha(black, 0.45), transparent 65%); }
.hero-text { color: white; text-shadow: 0 1px 6px alpha(black, 0.6); }
.hero-stats { font-weight: 600; }
.sidebar-count { font-size: 0.85em; }
/* Favorites star: white outline (empty inside) on hover, golden and filled once checked.
   No GTK background or state effect (no white flash on click). */
.fav-btn, .fav-btn:hover, .fav-btn:active, .fav-btn:checked, .fav-btn:checked:hover {
    min-width: 30px; min-height: 30px; padding: 0; border-radius: 999px; background: alpha(black, 0.55);
    box-shadow: none; border: none; opacity: 0; transition: opacity 150ms, background 150ms; }
.fav-btn:hover, .fav-btn:checked:hover { background: alpha(black, 0.75); }
.clip-card:hover .fav-btn { opacity: 0.9; }
.clip-card:hover .fav-btn:hover { opacity: 1; }
.fav-btn.on, .fav-btn.on:hover, .fav-btn.on:checked { opacity: 1; }
.fav-hero { border-radius: 16px; min-height: 150px;
            background-image: linear-gradient(120deg, #7a4a00 0%, #c27c0e 45%, #f6d32d 100%); }
.fav-hero .hero-text { color: white; }
.section-game picture { border-radius: 5px; }
.card-btn, .card-btn:hover, .card-btn:active, .card-btn:checked {
    min-width: 30px; min-height: 30px; padding: 0; border-radius: 999px; box-shadow: none;
    background: alpha(black, 0.55); color: white; opacity: 0; transition: opacity 150ms, background 150ms; }
.clip-card:hover .card-btn { opacity: 0.95; }
.card-btn:hover { background: alpha(black, 0.75); }
/* Selection checkbox: visible on hover, always visible in selection mode or once checked. */
.sel-btn { padding: 4px; border-radius: 8px; background: alpha(black, 0.45); opacity: 0;
           transition: opacity 150ms; }
.sel-btn check { margin: 0; }
.sel-btn:not(:checked) check { background: alpha(black, 0.35); box-shadow: inset 0 0 0 2px alpha(white, 0.85); }
.clip-card:hover .sel-btn, .sel-btn:checked, .selecting .sel-btn { opacity: 1; }
.card-play, .card-play:hover, .card-play:active { min-width: 52px; min-height: 52px; padding: 0;
    border-radius: 999px; background: alpha(black, 0.5); border: 1.5px solid alpha(white, 0.25);
    box-shadow: none; opacity: 0; transition: opacity 150ms, background 150ms; }
.clip-card:hover .card-play { opacity: 1; }
.card-play:hover { background: alpha(@accent_bg_color, 0.92); }
.card-scrub { margin: 0 6px 2px 6px; }
.previewing .duration-badge, .previewing .game-chip { margin-bottom: 26px; }
.card-scrub trough { min-height: 4px; }
.clip-card.selected { background: alpha(@accent_bg_color, 0.18); box-shadow: inset 0 0 0 2px @accent_bg_color; }
.selection-bar { padding: 4px 8px; }
flowboxchild, flowboxchild:hover, flowboxchild:active, flowboxchild:selected { background: none; padding: 0; }
.fav-toggle.on { color: #f6d32d; }
.play-btn { min-width: 34px; min-height: 34px; padding: 0; }
.compact { min-width: 24px; min-height: 24px; padding: 0; }
.lane-header .heading { font-size: 0.92em; }
.lane-header { border-radius: 8px; padding: 0 6px; background: alpha(currentColor, 0.05); }
.section-count { margin-left: 8px; }
.export-progress progress { min-height: 4px; }
.export-progress trough { min-height: 4px; }
.summary { font-size: 0.95em; }
.side-handle { min-width: 9px; }
.side-handle separator { transition: background 150ms; }
.side-handle:hover separator, .side-handle.dragging separator { background: @accent_bg_color; min-width: 3px; }
.help-pop { padding: 6px 4px; }
.listen-box { border-radius: 999px; padding: 2px 8px 2px 2px; }
.stale-badge { background: #e66100; color: white; border-radius: 999px; padding: 4px;
               box-shadow: 0 1px 4px alpha(black, 0.5); }
.share-tile.stale { box-shadow: inset 0 0 0 2px alpha(#e66100, 0.9); }
.audio-thumb { border-radius: 8px; background: alpha(currentColor, 0.08); }
.preview-pick { border-radius: 999px; padding: 2px; }
.preview-pick toggle { padding: 2px 10px; min-height: 24px; }
.preview-note { border-radius: 12px; padding: 8px 14px; }
.lanes-bar slider { min-height: 18px; margin: 0; }
.lanes-bar trough { margin: 0; }
.v-handle { min-height: 14px; }
.v-handle separator { transition: background 150ms; }
.v-handle:hover separator, .v-handle.dragging separator { background: @accent_bg_color; min-height: 3px; }
"""


class Thumb(Gtk.Picture):
    """16:9 thumbnail with a fixed natural size (272 px): the grid fits as many cards as possible
    per row, then they grow together to fill the width."""
    NAT_W = 272

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 200, self.NAT_W, -1, -1
        w = for_size if for_size > 0 else self.NAT_W
        h = round(w * 9 / 16)
        return h, h, -1, -1

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH


# ---------------------------------------------------------------- model

class ClipItem(GObject.Object):
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, path):
        super().__init__()
        self.path = path
        st = os.stat(path)
        self.mtime = st.st_mtime
        self.size = st.st_size
        self.meta = None
        self.texture = None
        self.loading = False
        self.load_info()

    def refresh(self):
        """The file changed (end of a manual recording): everything is re-read (duration, thumbnail…)."""
        st = os.stat(self.path)
        self.mtime, self.size = st.st_mtime, st.st_size
        self.meta = self.texture = None
        self.loading = False
        self.load_info()

    def load_info(self):
        info = load_clip_info(self.path)
        self.game = info.get("game")
        self.appid = info.get("appid") or (steam_appid(self.game) if self.game else None)
        self.favorite = bool(info.get("favorite"))
        self.n_screens = max(1, len(info.get("screens") or []))

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def stem(self):
        return os.path.splitext(self.name)[0]


class Library(GObject.Object):
    """Clip list, folder monitoring, lazy metadata and thumbnails."""
    __gsignals__ = {"stats-changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, folder):
        super().__init__()
        self.folder = folder
        self.recording_path = None   # manual recording in progress: incomplete file, neither probed nor played
        self.store = Gio.ListStore(item_type=ClipItem)
        os.makedirs(folder, exist_ok=True)
        os.makedirs(THUMB_DIR, exist_ok=True)
        self.reload()
        self.monitor = Gio.File.new_for_path(folder).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.monitor.connect("changed", self._on_changed)
        self.store.connect("items-changed", lambda *_: self.emit("stats-changed"))

    def total_size(self):
        return sum(self.store.get_item(i).size for i in range(self.store.get_n_items()))

    def reload(self):
        items = []
        with os.scandir(self.folder) as it:
            for e in it:
                if e.is_file() and e.name.lower().endswith(VIDEO_EXT):
                    try:
                        items.append(ClipItem(e.path))
                    except OSError:
                        pass
        items.sort(key=lambda i: i.mtime, reverse=True)
        self.store.splice(0, self.store.get_n_items(), items)

    def index(self, path):
        for i in range(self.store.get_n_items()):
            if self.store.get_item(i).path == path:
                return i
        return -1

    def _add(self, path):
        if not path.lower().endswith(VIDEO_EXT) or os.path.dirname(path) != self.folder:
            return
        if self.index(path) >= 0 or not os.path.isfile(path):
            return
        try:
            item = ClipItem(path)
        except OSError:
            return
        pos = 0
        while pos < self.store.get_n_items() and self.store.get_item(pos).mtime > item.mtime:
            pos += 1
        self.store.insert(pos, item)
        GLib.timeout_add(4000, lambda: (self.reread(item), False)[1])

    def reread(self, item):
        """Sidecar file completed afterwards (game, 2nd screen attached by on_save.py)."""
        if self.index(item.path) >= 0 and os.path.exists(item.path):
            before = (item.game, item.n_screens)
            item.load_info()
            if (item.game, item.n_screens) != before:
                item.emit("changed")

    def _remove(self, path):
        i = self.index(path)
        if i >= 0:
            self.store.remove(i)

    def _on_changed(self, _mon, f, other, event):
        E = Gio.FileMonitorEvent
        path = f.get_path()
        if event in (E.CHANGES_DONE_HINT, E.MOVED_IN):
            self._add(path)
        elif event in (E.DELETED, E.MOVED_OUT):
            self._remove(path)
        elif event == E.RENAMED:
            self._remove(path)
            if other:
                self._add(other.get_path())

    def ensure(self, item):
        """Loads metadata then thumbnail (with disk cache) if not done yet."""
        if item.loading or (item.meta and item.texture) or item.path == self.recording_path:
            return
        item.loading = True

        def got_meta(meta):
            item.meta = meta
            item.emit("changed")
            if meta:
                self._thumb(item)
            else:
                item.loading = False

        probe(item.path, got_meta)

    def _thumb(self, item):
        try:
            out = os.path.join(THUMB_DIR, cache_key(item.path) + ".jpg")
        except OSError:
            item.loading = False
            return

        def load(*_):
            item.loading = False
            if os.path.exists(out):
                try:
                    item.texture = Gdk.Texture.new_from_filename(out)
                    item.emit("changed")
                except GLib.Error:
                    pass

        if os.path.exists(out):
            load()
            return
        t = min(item.meta["duration"] * 0.4, 3.0)
        JOBS.add(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{t:.2f}", "-i", item.path,
                  "-frames:v", "1", "-vf", "scale=384:-2", "-q:v", "5", out], load)


# ---------------------------------------------------------------- window

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Clips")
        self.set_default_size(1320, 860)
        self.cfg = load_config()
        Thumb.NAT_W = int(self.cfg.get("thumb_size", 272))
        self.cards = []
        self.selected = set()
        self.preview = None
        self.scrub_grab = None
        self.library = Library(self.cfg["clips_dir"])
        if manual_recording_started() is not None:   # before the 1st card: the file in progress is not probed
            self.library.recording_path = manual_recording_path()
        SHARE.prune()

        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.nav.add(self._library_page())
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)
        self.refresh_recorder_state()

        self._action("prefs", lambda *_: Preferences(self).present(self))
        self._action("folder", lambda *_: open_uri(self.cfg["clips_dir"]))
        self._action("clip-edit", lambda _a, p: self.open_clip(p.get_string()), "s")
        self._action("clip-show", lambda _a, p: show_in_folder(p.get_string()), "s")
        self._action("clip-copy", lambda _a, p: self._copy_clip(p.get_string()), "s")
        self._action("clip-export", lambda _a, p: self._quick_export(*p.get_string().split("\n", 1)), "s")
        self._action("clip-trash", lambda _a, p: self.trash_clip(p.get_string()), "s")
        self._action("clip-fullscreen", lambda _a, p: self.open_clip(p.get_string(), fullscreen=True), "s")
        self._action("clip-rename", lambda _a, p: self._with_item(p.get_string(), self.rename_dialog), "s")
        self._action("clip-game", lambda _a, p: self._with_item(p.get_string(), lambda it: self.pick_game([it])), "s")
        self._action("clip-fav", lambda _a, p: self._with_item(
            p.get_string(), lambda it: self._set_favorite(it, not it.favorite)), "s")
        self._action("clip-select", lambda _a, p: self._with_item(
            p.get_string(), lambda it: self._select(it, True)), "s")
        app.set_accels_for_action("win.prefs", ["<Control>comma"])

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))

    def _action(self, name, cb, param=None):
        a = Gio.SimpleAction.new(name, GLib.VariantType.new(param) if param else None)
        a.connect("activate", cb)
        self.add_action(a)

    def _is_recording(self, path):
        if path and path == self.library.recording_path:
            self.toast(f"Recording in progress — {shortcut_label('rec_stop')} to stop it")
            return True
        return False

    def _share_args(self, item):
        if not item.meta:
            return None
        return item.path, share_gains(item.path, item.meta["audio_tracks"], self.cfg)

    def _prepare_share(self, item):
        # On hover, only short clips (≈ 1 s of work); a long clip is prepared when the editor opens,
        # in the background.
        args = self._share_args(item)
        if args and item.path != self.library.recording_path and item.meta["duration"] <= SHARE.SYNC_MAX_S:
            SHARE.prepare(*args)

    def _share_path(self, item):
        """File to send: copy with mixed audio if the clip has several tracks (otherwise only the 1st would be
        heard). None = long clip still being prepared."""
        args = self._share_args(item)
        return SHARE.get_sync(*args, item.meta["duration"]) if args else item.path

    def _share_content(self, item):
        if self._is_recording(item.path):
            return None
        path = self._share_path(item)
        if path is None:
            self.toast("Preparing the audio for sharing… try again in a moment")
            return None
        return file_content_provider(path)

    def _copy_clip(self, path):
        idx = self.library.index(path)
        content = self._share_content(self.library.store.get_item(idx)) if idx >= 0 else file_content_provider(path)
        if content:
            self.get_clipboard().set_content(content)
            self.toast("File copied — paste it with Ctrl+V")

    def _with_item(self, path, fn):
        i = self.library.index(path)
        if i >= 0:
            fn(self.library.store.get_item(i))

    def trash_clip(self, path, quiet=False):
        """System trash: the clip, its sidecar file and its other screens (all recovered together)."""
        try:
            if self.preview and self.preview[0].item.path == path:
                self._stop_preview()
            files = [path] + companion_files(path)
            for f in files:
                Gio.File.new_for_path(f).trash(None)
        except GLib.Error as e:
            self.toast(f"Couldn't do it: {e.message}")
            return
        finally:
            self._mark_kde_trash_full()
        if not quiet:
            toast = Adw.Toast(title=f"\"{os.path.splitext(os.path.basename(path))[0]}\" moved to the trash",
                              button_label="Undo", timeout=6)
            toast.connect("button-clicked", lambda *_: self._untrash(files))
            self.toasts.add_toast(toast)

    @staticmethod
    def _mark_kde_trash_full():
        """KDE (Dolphin, Plasma) reads "trash empty" from trashrc, which only KIO updates: without this, Gio leaves
        "Empty Trash" greyed out even though it's full."""
        try:
            Gio.Subprocess.new(["kwriteconfig6", "--file", "trashrc", "--group", "Status", "--key", "Empty", "false"],
                               Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error:
            pass  # not on KDE

    @staticmethod
    def _trash_entry(path):
        """Trash file (freedesktop) matching path: the most recently trashed one."""
        from urllib.parse import unquote
        trash = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "Trash")
        best = None
        try:
            infos = list(os.scandir(os.path.join(trash, "info")))
        except OSError:
            return None
        for e in infos:
            if not e.name.endswith(".trashinfo"):
                continue
            try:
                with open(e.path) as f:
                    fields = dict(l.strip().split("=", 1) for l in f if "=" in l)
            except (OSError, ValueError):
                continue
            if unquote(fields.get("Path", "")) == path:
                date = fields.get("DeletionDate", "")
                if best is None or date > best[0]:
                    best = (date, e.path, os.path.join(trash, "files", e.name[:-len(".trashinfo")]))
        return best

    def _untrash(self, files):
        """Undo: puts each file back in place from the trash."""
        missing = 0
        for path in files:
            entry = self._trash_entry(path)
            if not entry or os.path.exists(path):
                missing += 1
                continue
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                os.rename(entry[2], path)
                os.remove(entry[1])
            except OSError:
                missing += 1
        self.toast("Clip restored" if not missing else "Incomplete restore: have a look in the trash")

    # ---- library
    def _library_page(self):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="Clips")
        header.set_title_widget(self.window_title)

        pill = Gtk.Box(spacing=8, css_classes=["replay-pill"], valign=Gtk.Align.CENTER)
        self.rec_dot = Gtk.Box(css_classes=["rec-dot"], valign=Gtk.Align.CENTER)
        self.rec_label = Gtk.Label(label="Replay", css_classes=["caption-heading"], width_chars=13, xalign=0)
        self.rec_switch = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Continuous recording (replay)")
        self.rec_handler = self.rec_switch.connect("notify::active", self._toggle_recorder)
        for w in (self.rec_dot, self.rec_label, self.rec_switch):
            pill.append(w)
        # Manual recording in progress (Alt+F9 → Alt+F11): red pill with the duration and a stop button.
        self.manual_pill = Gtk.Box(spacing=6, css_classes=["manual-rec-pill"], valign=Gtk.Align.CENTER,
                                   visible=False)
        self.manual_pill.append(Gtk.Box(css_classes=["rec-dot", "on"], valign=Gtk.Align.CENTER))
        self.manual_label = Gtk.Label(css_classes=["caption-heading", "numeric"], width_chars=9, xalign=0)
        self.manual_pill.append(self.manual_label)
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", css_classes=["flat", "circular", "compact"],
                          valign=Gtk.Align.CENTER, tooltip_text=f"Stop the recording ({shortcut_label('rec_stop')})")
        stop.connect("clicked", lambda *_: run_async([os.path.join(APP_DIR, "record-clip"), "stop"],
                                                     lambda *_a: self._poll_manual_recording()))
        self.manual_pill.append(stop)
        self.side_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Show / hide the panel (F9)",
                                         active=self.cfg.get("sidebar_open", True))
        self.side_btn.connect("toggled", self._on_side_toggled)
        header.pack_start(self.side_btn)
        header.pack_start(pill)
        header.pack_start(self.manual_pill)
        GLib.timeout_add(500, self._poll_manual_recording)
        menu = Gio.Menu()
        menu.append("Preferences", "win.prefs")
        menu.append("Open the clips folder", "win.folder")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Menu"))
        sources = Gtk.Button(icon_name="audio-speakers-symbolic", tooltip_text="Audio sources and settings (Ctrl+,)")
        sources.connect("clicked", lambda *_: Preferences(self).present(self))
        header.pack_end(sources)
        toolbar.add_top_bar(header)

        # Sidebar: All clips · one game per row (Steam icon + count) · No game
        self.game_filter = "all"
        self.side_key = None
        self.side_updating = False
        self.sidebar = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.sidebar.connect("row-selected", self._on_side_selected)
        side_scroll = Gtk.ScrolledWindow(child=self.sidebar, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         width_request=236)
        side_box = Gtk.Box()
        side_box.append(side_scroll)
        side_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        # Panel always there (even with no clip); collapsible via the title bar button.
        self.side_panel = Gtk.Revealer(child=side_box, transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT,
                                       transition_duration=180, reveal_child=self.side_btn.get_active())

        # Filter bar: search · period · specific day · sort
        # Wraps when the window is narrow (nothing gets truncated or overflows).
        bar = Adw.WrapBox(child_spacing=10, line_spacing=8, css_classes=["filter-bar"])
        self.filter_bar = bar
        self.search = Gtk.SearchEntry(placeholder_text="Search for a clip", width_chars=22)
        self.search.connect("search-changed", lambda *_: self._schedule_rebuild())
        bar.append(self.search)
        self.period = Adw.ToggleGroup(css_classes=["round"])
        for name, label in (("all", "All"), ("today", "Today"), ("7", "7 days"), ("30", "30 days")):
            self.period.add(Adw.Toggle(name=name, label=label))
        self.period.set_active_name("all")
        self.period.connect("notify::active-name", self._on_period)
        bar.append(self.period)
        self.day = None
        cal = Gtk.Calendar()
        cal.connect("day-selected", self._on_day)
        pop = Gtk.Popover(child=cal)
        self.day_btn = Gtk.MenuButton(popover=pop, tooltip_text="Pick a specific day")
        self.day_btn.set_child(Adw.ButtonContent(icon_name="x-office-calendar-symbolic", label="Date"))
        self.day_clear = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                                    tooltip_text="Remove the date filter", visible=False)
        self.day_clear.connect("clicked", lambda *_: self._set_day(None))
        date = Gtk.Box(spacing=2)
        date.append(self.day_btn)
        date.append(self.day_clear)
        bar.append(date)
        self.sort = Gtk.DropDown(model=Gtk.StringList.new(["Newest", "Oldest", "Longest", "Largest"]),
                                 tooltip_text="Sort")
        self.sort.connect("notify::selected", lambda *_: self._schedule_rebuild())
        bar.append(self.sort)
        self.count_label = Gtk.Label(css_classes=["dim-label"], valign=Gtk.Align.CENTER)
        bar.append(self.count_label)
        size = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER, tooltip_text="Thumbnail size")
        size.append(Gtk.Image(icon_name="clips-thumb-size-symbolic", pixel_size=14, css_classes=["dim-label"]))
        self.thumb_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 180, 560, 10)
        self.thumb_scale.set_value(Thumb.NAT_W)
        self.thumb_scale.set_size_request(120, -1)
        self.thumb_scale.connect("value-changed", self._on_thumb_size)
        size.append(self.thumb_scale)
        bar.append(size)

        # Main column (scrolls as one block): game header · filters · clips by day
        self.sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                                margin_start=18, margin_end=18, margin_top=14, margin_bottom=18)
        scroller = Gtk.ScrolledWindow(child=self.sections, vexpand=True, hexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        empty = Adw.StatusPage(icon_name="camera-video-symbolic", title="No clips yet",
                               description=f"Turn on the replay, launch a game and press <b>{SHORTCUT_LABEL}</b> "
                                           "to keep the last seconds — or record whatever you want with "
                                           f"<b>{shortcut_label('rec_start')}</b> then "
                                           f"<b>{shortcut_label('rec_stop')}</b>.", hexpand=True)
        self.lib_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True)
        self.lib_stack.add_named(scroller, "grid")
        self.lib_stack.add_named(empty, "empty")
        row = Gtk.Box(vexpand=True)
        row.append(self.side_panel)
        row.append(self.lib_stack)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(row)
        body.append(self._selection_bar())
        keys = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("F9"),
                                       action=Gtk.CallbackAction.new(lambda *_: (self.side_btn.activate(), True)[1])))
        body.add_controller(keys)
        self.scroller = scroller
        self.card_handlers = []
        self.rebuild_source = 0
        self.library.connect("stats-changed", lambda *_: self._schedule_rebuild())
        self._rebuild()
        toolbar.set_content(body)
        return Adw.NavigationPage(title="Clips", child=toolbar, tag="library")

    def _on_period(self, group, _):
        if group.get_active_name() and self.day:
            self._set_day(None, rebuild=False)
        self._schedule_rebuild()

    def _on_day(self, cal):
        d = cal.get_date()
        self._set_day((d.get_year(), d.get_month(), d.get_day_of_month()))
        self.day_btn.popdown()

    def _set_day(self, day, rebuild=True):
        self.day = day
        self.day_clear.set_visible(day is not None)
        if day:
            self.period.set_active(Gtk.INVALID_LIST_POSITION)
            self.day_btn.get_child().set_label(f"{day[2]:02d}/{day[1]:02d}/{day[0]}")
        else:
            self.day_btn.get_child().set_label("Date")
            if self.period.get_active_name() is None:
                self.period.set_active_name("all")
        if rebuild:
            self._schedule_rebuild()

    def _schedule_rebuild(self):
        if not self.rebuild_source:
            self.rebuild_source = GLib.idle_add(self._rebuild)

    def refresh_library(self):
        self.cfg = load_config()
        for i in range(self.library.store.get_n_items()):
            self.library.store.get_item(i).load_info()
        self.side_key = None
        self._schedule_rebuild()

    # games sidebar
    def _side_row(self, key, name, count, appid=None, icon=None):
        row = Gtk.ListBoxRow()
        row.key = key
        box = Gtk.Box(spacing=10, margin_top=2, margin_bottom=2)
        tex = texture(game_art(appid).get("icon")) if appid else None
        if tex:
            box.append(Gtk.Picture(paintable=tex, width_request=22, height_request=22, can_shrink=True,
                                   css_classes=["game-icon"], valign=Gtk.Align.CENTER))
        else:
            box.append(Gtk.Image(icon_name=icon or "input-gaming-symbolic", pixel_size=18,
                                 width_request=22, valign=Gtk.Align.CENTER))
        box.append(Gtk.Label(label=name, xalign=0, ellipsize=3, hexpand=True))
        box.append(Gtk.Label(label=str(count), css_classes=["dim-label", "numeric", "sidebar-count"]))
        row.set_child(box)
        row.set_tooltip_text(name)
        return row

    def _side_title(self, text):
        row = Gtk.ListBoxRow(selectable=False, activatable=False)
        row.set_child(Gtk.Label(label=text, xalign=0, css_classes=["caption-heading", "dim-label"],
                                margin_top=10, margin_start=6))
        return row

    def _rebuild_sidebar(self, items):
        games = {}
        for it in items:
            g = games.setdefault(it.game, [0, it.appid])
            g[0] += 1
            g[1] = g[1] or it.appid
        favs = sum(1 for it in items if it.favorite)
        valid = {"all", "none", "fav"} | {f"g:{g}" for g in games if g}
        if self.game_filter not in valid:
            self.game_filter = "all"
        key = (tuple(sorted((g or "", c, a or "") for g, (c, a) in games.items())), favs)
        if key != self.side_key:
            self.side_key = key
            self.side_updating = True
            while (c := self.sidebar.get_first_child()):
                self.sidebar.remove(c)
            self.sidebar.append(self._side_row("all", "All clips", len(items), icon="view-grid-symbolic"))
            self.sidebar.append(self._side_row("fav", "Favorites", favs, icon="clips-star-symbolic"))
            if any(g for g in games) and self.cfg.get("detect_game", True):
                self.sidebar.append(self._side_title("GAMES"))
            for g in sorted((g for g in games if g and self.cfg.get("detect_game", True)),
                            key=lambda x: (-games[x][0], x.lower())):
                self.sidebar.append(self._side_row(f"g:{g}", g, games[g][0], appid=games[g][1]))
            if games.get(None) and self.cfg.get("detect_game", True):
                self.sidebar.append(self._side_row("none", "No game", games[None][0], icon="camera-video-symbolic"))
            self.side_updating = False
        self.side_updating = True
        row = self.sidebar.get_first_child()
        while row:
            if getattr(row, "key", None) == self.game_filter:
                self.sidebar.select_row(row)
            row = row.get_next_sibling()
        self.side_updating = False
        return games

    def _on_side_toggled(self, btn):
        self.side_panel.set_reveal_child(btn.get_active())
        cfg = load_config()
        cfg["sidebar_open"] = btn.get_active()
        save_config(cfg)

    def _on_side_selected(self, _box, row):
        if self.side_updating or row is None or not hasattr(row, "key"):
            return
        if row.key != self.game_filter:
            self.game_filter = row.key
            self._schedule_rebuild()

    # column header: game banner (Steam images) or plain title
    def _header(self, items):
        n = len(items)
        size = sum(it.size for it in items)
        total = sum(it.meta["duration"] for it in items if it.meta)
        stats = [f"{n} clip{'s' if n > 1 else ''}", fmt_size(size)]
        if total:
            stats.append(f"{int(total // 60)} min {int(total % 60):02d} s in total" if total >= 60 else f"{int(total)} s in total")
        if items:
            last = max(items, key=lambda it: it.mtime)
            stats.append(f"latest: {self._day_label(last.mtime).lower()} at {time.strftime('%H:%M', time.localtime(last.mtime))}")
        stats = " · ".join(stats)
        key = self.game_filter
        if key == "fav":
            hero = Gtk.Box(spacing=20, css_classes=["fav-hero"], margin_bottom=6)
            hero.append(Gtk.Image(icon_name="clips-fav-on", pixel_size=64, margin_start=28, valign=Gtk.Align.CENTER))
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
            text.append(Gtk.Label(label="Favorites", xalign=0, css_classes=["title-1", "hero-text"]))
            text.append(Gtk.Label(label=stats, xalign=0, css_classes=["hero-text", "hero-stats"]))
            hero.append(text)
            return hero
        if key.startswith("g:"):
            name = key[2:]
            appid = next((it.appid for it in items if it.appid), None)
            art = game_art(appid)
            hero_tex = texture(art.get("hero") or art.get("header"))
            if hero_tex:
                hero = Gtk.Overlay(css_classes=["hero"], overflow=Gtk.Overflow.HIDDEN, margin_bottom=6)
                hero.set_child(Gtk.Picture(paintable=hero_tex, content_fit=Gtk.ContentFit.COVER, can_shrink=True,
                                           height_request=180, hexpand=True))
                hero.add_overlay(Gtk.Box(css_classes=["hero-fade"]))
                text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.START,
                               valign=Gtk.Align.END, margin_start=28, margin_bottom=18)
                logo = texture(art.get("logo"))
                if logo:
                    ratio = logo.get_width() / max(logo.get_height(), 1)
                    h = 56 if ratio < 6 else 40
                    text.append(Gtk.Picture(paintable=logo, content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True,
                                            height_request=h, width_request=min(int(h * ratio), 420),
                                            halign=Gtk.Align.START))
                else:
                    text.append(Gtk.Label(label=name, xalign=0, css_classes=["title-1", "hero-text"]))
                text.append(Gtk.Label(label=stats, xalign=0, css_classes=["hero-text", "hero-stats"]))
                hero.add_overlay(text)
                return hero
            title = name
        else:
            title = {"none": "No game", "fav": "Favorites"}.get(key, "All clips")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=6, margin_bottom=4)
        line = Gtk.Box(spacing=12)
        if key.startswith("g:"):
            appid = next((it.appid for it in items if it.appid), None)
            tex = texture(game_art(appid).get("icon"))
            if tex:
                line.append(Gtk.Picture(paintable=tex, width_request=36, height_request=36, can_shrink=True,
                                        css_classes=["game-icon"], valign=Gtk.Align.CENTER))
        line.append(Gtk.Label(label=title, xalign=0, css_classes=["title-1"]))
        box.append(line)
        box.append(Gtk.Label(label=stats, xalign=0, css_classes=["dim-label"]))
        return box

    def _filtered_game(self, items):
        if self.game_filter == "fav":
            return [it for it in items if it.favorite]
        if self.game_filter.startswith("g:"):
            return [it for it in items if it.game == self.game_filter[2:]]
        if self.game_filter == "none":
            return [it for it in items if not it.game]
        return items

    def _filtered(self, items):
        items = self._filtered_game(items)
        q = self.search.get_text().strip().lower()
        if q:
            items = [it for it in items if q in it.stem.lower() or q in (it.game or "").lower()]
        if self.day:
            y, mo, d = self.day
            items = [it for it in items if time.localtime(it.mtime)[:3] == (y, mo, d)]
        else:
            period = self.period.get_active_name() or "all"
            if period != "all":
                midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
                since = midnight if period == "today" else midnight - (int(period) - 1) * 86400
                items = [it for it in items if it.mtime >= since]
        sort = self.sort.get_selected()
        if sort == 1:
            items = sorted(items, key=lambda it: it.mtime)
        elif sort == 2:
            items = sorted(items, key=lambda it: -(it.meta["duration"] if it.meta else 0))
        elif sort == 3:
            items = sorted(items, key=lambda it: -it.size)
        return items

    def _rebuild(self):
        self.rebuild_source = 0
        self.cfg = load_config()
        store = self.library.store
        items = [store.get_item(i) for i in range(store.get_n_items())]
        n = len(items)
        self.lib_stack.set_visible_child_name("grid" if n else "empty")
        self.window_title.set_subtitle(f"{n} clip{'s' if n > 1 else ''} · {fmt_size(self.library.total_size())}" if n else "")
        self._rebuild_sidebar(items)
        in_game = self._filtered_game(items)
        shown = self._filtered(items)
        self.count_label.set_label(f"{len(shown)} clip{'s' if len(shown) > 1 else ''}")

        self._stop_preview()
        for it, h in self.card_handlers:
            it.disconnect(h)
        self.card_handlers = []
        self.cards = []
        self.selected &= {it.path for it in items}
        while (child := self.sections.get_first_child()):
            self.sections.remove(child)
        self.sections.append(self._header(in_game))
        self.sections.append(self.filter_bar)
        if not shown:
            self.sections.append(Adw.StatusPage(icon_name="edit-find-symbolic", title="No matching clips",
                                                description="Try another period or search.", vexpand=True))
        by_game = self.game_filter == "fav"     # Favorites: one section per game
        by_date = self.sort.get_selected() in (0, 1) and not by_game
        groups = []
        for it in shown:
            if by_game:
                key = it.game or ""
                grp = next((g for g in groups if g[0] == key), None)
                if grp is None:
                    grp = (key, [])
                    groups.append(grp)
                grp[1].append(it)
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(it.mtime)) if by_date else "all"
            if not groups or groups[-1][0] != day:
                groups.append((day, []))
            groups[-1][1].append(it)
        if by_game:
            groups.sort(key=lambda g: g[0] == "")   # "No game" last
        for day, group in groups:
            if by_game:
                head = Gtk.Box(spacing=10, margin_top=14, margin_bottom=4, margin_start=6, css_classes=["section-game"])
                appid = next((it.appid for it in group if it.appid), None)
                tex = texture(game_art(appid).get("icon")) if appid else None
                if tex:
                    head.append(Gtk.Picture(paintable=tex, width_request=24, height_request=24, can_shrink=True,
                                            valign=Gtk.Align.CENTER))
                head.append(Gtk.Label(label=day or "No game", css_classes=["title-4"], xalign=0))
                head.append(Gtk.Label(label=f"{len(group)} clip{'s' if len(group) > 1 else ''}",
                                      css_classes=["dim-label", "section-count"], valign=Gtk.Align.BASELINE))
                self.sections.append(head)
            elif by_date:
                head = Gtk.Box(margin_top=14, margin_bottom=4, margin_start=6)
                head.append(Gtk.Label(label=self._day_label(group[0].mtime), css_classes=["title-4"], xalign=0))
                head.append(Gtk.Label(label=f"{len(group)} clip{'s' if len(group) > 1 else ''}",
                                      css_classes=["dim-label", "section-count"], valign=Gtk.Align.BASELINE))
                self.sections.append(head)
            flow = Gtk.FlowBox(homogeneous=True, selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8,
                               min_children_per_line=1, activate_on_single_click=True,
                               row_spacing=6, column_spacing=6, valign=Gtk.Align.START,
                               margin_top=0 if (by_date or by_game) else 12)
            flow.connect("child-activated", lambda _f, child: self._on_card_activated(child.get_child()))
            for it in group:
                flow.append(self._card(it))
            self.sections.append(flow)
        self._update_selection()
        return False

    @staticmethod
    def _day_label(ts):
        lt = time.localtime(ts)
        days = (time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)) - time.mktime(lt[:3] + (0, 0, 0, 0, 0, -1))) // 86400
        months = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                  "October", "November", "December"]
        weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        if days == 0:
            return "Today"
        if days == 1:
            return "Yesterday"
        date = f"{lt.tm_mday} {months[lt.tm_mon - 1]}"
        if days < 7:
            return f"{weekdays[lt.tm_wday]} {date}"
        return date if lt.tm_year == time.localtime().tm_year else f"{date} {lt.tm_year}"

    def _card(self, item):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["clip-card"])
        box.item = item
        overlay = Gtk.Overlay()
        pic = Thumb(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        badge = Gtk.Label(css_classes=["duration-badge", "numeric"], halign=Gtk.Align.END, valign=Gtk.Align.END)
        chip = Gtk.Box(spacing=5, css_classes=["game-chip"], halign=Gtk.Align.START, valign=Gtk.Align.END)
        chip_icon = Gtk.Picture(width_request=16, height_request=16, can_shrink=True,
                                content_fit=Gtk.ContentFit.COVER, valign=Gtk.Align.CENTER)
        chip_label = Gtk.Label(ellipsize=3, max_width_chars=22)
        chip.append(chip_icon)
        chip.append(chip_label)
        # play button: preview right in the card
        play = Gtk.Button(css_classes=["card-play"], halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                          focus_on_click=False, tooltip_text="Preview here")
        white = Gdk.RGBA()
        white.parse("white")
        glyph = PlayGlyph(17, color=white)
        play.set_child(glyph)
        play.connect("clicked", lambda *_: self._toggle_preview(box))
        # selection (top left corner) · fullscreen + favorite (top right corner)
        sel = Gtk.CheckButton(css_classes=["sel-btn"],
                               halign=Gtk.Align.START, valign=Gtk.Align.START, margin_top=6, margin_start=6,
                               focus_on_click=False, tooltip_text="Select")
        sel.connect("toggled", lambda b: self._select(box.item, b.get_active()))
        full = Gtk.Button(icon_name="view-fullscreen-symbolic", css_classes=["card-btn"], halign=Gtk.Align.END,
                          valign=Gtk.Align.START, margin_top=6, margin_end=42, focus_on_click=False,
                          tooltip_text="Play fullscreen")
        full.connect("clicked", lambda *_: self.open_clip(box.item.path, fullscreen=True))
        trash = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["card-btn"], halign=Gtk.Align.END,
                           valign=Gtk.Align.START, margin_top=6, margin_end=78, focus_on_click=False,
                           tooltip_text="Move to the trash (can be undone for a few seconds)")
        trash.connect("clicked", lambda *_: self.trash_clip(box.item.path))
        star = Gtk.ToggleButton(css_classes=["fav-btn", "circular"], halign=Gtk.Align.END, valign=Gtk.Align.START,
                                margin_top=6, margin_end=6, focus_on_click=False)
        star.set_child(Gtk.Image(icon_name="clips-fav-off", pixel_size=22))
        star.connect("toggled", lambda b: self._set_favorite(box.item, b.get_active()))
        # preview playback bar, at the bottom of the thumbnail
        scrub = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.01)
        scrub.set_draw_value(False)
        scrub.set_valign(Gtk.Align.END)
        scrub.add_css_class("card-scrub")
        scrub.set_visible(False)
        scrub.set_focus_on_click(False)
        scrub.connect("change-value", lambda _s, _t, v: self._preview_seek(box, v))
        # Hook into the slider's internal drag gesture: it starts on press (knob or trough)
        # and ends on release, even if the slider "captured" the click.
        ctrls = scrub.observe_controllers()
        for i in range(ctrls.get_n_items()):
            c = ctrls.get_item(i)
            if isinstance(c, Gtk.GestureDrag):
                c.connect("drag-begin", lambda *_: self._preview_grab(box, True))
                c.connect("drag-end", lambda *_: self._preview_grab(box, False))
        overlay.set_child(pic)
        # manual recording in progress: veil + "Recording" (the file isn't playable yet)
        rec = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["rec-veil"], visible=False,
                      valign=Gtk.Align.FILL, halign=Gtk.Align.FILL)
        rec_title = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, vexpand=True)
        rec_title.append(Gtk.Box(css_classes=["rec-dot", "on"], valign=Gtk.Align.CENTER))
        rec_title.append(Gtk.Label(label="Recording", css_classes=["title-3"]))
        rec.append(rec_title)
        rec.append(Gtk.Label(label=f"{shortcut_label('rec_stop')} to stop", css_classes=["caption"],
                             margin_bottom=14))
        for w in (badge, chip, play, sel, trash, full, star, scrub, rec):
            overlay.add_overlay(w)
        title = Gtk.Label(xalign=0, ellipsize=3, css_classes=["heading"], margin_start=4, max_width_chars=30)
        sub = Gtk.Label(xalign=0, css_classes=["caption", "dim-label", "numeric"], margin_start=4)
        for w in (overlay, title, sub):
            box.append(w)
        box.widgets = (pic, badge, chip, chip_icon, chip_label, title, sub)
        box.star, box.sel, box.play, box.glyph, box.scrub = star, sel, play, glyph, scrub
        box.full, box.rec, box.trash = full, rec, trash

        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        drag.connect("prepare", lambda *_: self._share_content(box.item))
        hover = Gtk.EventControllerMotion()   # prepare the shareable version as soon as it's hovered
        hover.connect("enter", lambda *_: self._prepare_share(box.item))
        box.add_controller(hover)
        drag.connect("drag-begin", lambda src, _d: box.item.texture and src.set_icon(box.item.texture, 64, 36))
        box.add_controller(drag)
        menu_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        menu_click.connect("pressed", lambda _g, _n, x, y: self._card_menu(box, x, y))
        box.add_controller(menu_click)

        def changed(it):
            self._card_fill(box, it)
            if self.sort.get_selected() == 2:   # sort by duration: the duration just arrived
                self._schedule_rebuild()

        self.card_handlers.append((item, item.connect("changed", changed)))
        self._card_fill(box, item)
        self.library.ensure(item)
        self.cards.append(box)
        return box

    def _on_card_activated(self, box):
        if self._is_recording(box.item.path):
            return
        if self.selected:   # in selection mode, a click checks / unchecks the card
            box.sel.set_active(not box.sel.get_active())
        else:
            self.open_clip(box.item.path)

    # ---- preview inside the card
    def _toggle_preview(self, box):
        if self.preview and self.preview[0] is box:
            player = self.preview[1]
            if player.playing:
                player.pause()
            else:
                player.play()
            box.glyph.set_playing(player.playing)
            return
        self._stop_preview()
        meta = box.item.meta
        if not meta:
            return
        holder = {}

        def restart():
            p = holder.get("p")
            if p:
                p.seek(0)
                p.play()

        player = Player(box.item.path, meta["audio_tracks"], meta.get("vcodec", ""), on_eos=restart)
        player.set_monitor(float(load_config().get("preview_volume", 1.0)))   # the editor's monitor volume
        holder["p"] = player
        box.widgets[0].set_paintable(player.paintable)
        box.scrub.set_range(0, max(meta["duration"], 0.01))
        box.scrub.set_value(0)
        box.scrub.set_visible(True)
        box.add_css_class("previewing")
        player.play_segments([(0.0, meta["duration"])], 0.0, True)
        player.play()
        box.glyph.set_playing(True)
        self.preview = (box, player, GLib.timeout_add(100, self._preview_tick))

    def _preview_tick(self):
        if not self.preview:
            return False
        if self.scrub_grab:   # playhead held: the mouse places it
            return True
        box, player, _t = self.preview
        pos = player.position()
        box.scrub.set_value(pos)
        box.widgets[1].set_label(f"{int(pos // 60)}:{int(pos % 60):02d}")
        return True

    def _preview_seek(self, box, value):
        if self.preview and self.preview[0] is box:
            player = self.preview[1]
            if player.playing:
                player.play_segments([(0.0, box.item.meta["duration"])], value, True)
            else:
                player.seek(value)
            box.widgets[1].set_label(f"{int(value // 60)}:{int(value % 60):02d}")
        return False

    def _preview_grab(self, box, pressed):
        """Grabbing the playhead: pause while dragging, resume (if it was playing) on release."""
        if not (self.preview and self.preview[0] is box):
            return
        player = self.preview[1]
        if pressed and not self.scrub_grab:
            self.scrub_grab = {"resume": player.playing}
            if player.playing:
                player.pause()
        elif not pressed and self.scrub_grab:
            resume = self.scrub_grab["resume"]
            self.scrub_grab = None
            if resume:
                player.play_segments([(0.0, box.item.meta["duration"])], box.scrub.get_value(), True)
                player.play()

    def _stop_preview(self):
        if not getattr(self, "preview", None):
            return
        box, player, tick = self.preview
        self.preview = None
        self.scrub_grab = None
        GLib.source_remove(tick)
        player.destroy()
        box.scrub.set_visible(False)
        box.remove_css_class("previewing")
        box.glyph.set_playing(False)
        self._card_fill(box, box.item)

    # ---- thumbnail size
    def _on_thumb_size(self, scale):
        Thumb.NAT_W = int(scale.get_value())
        for box in self.cards:
            box.widgets[0].queue_resize()
        if getattr(self, "_thumb_save", 0):
            GLib.source_remove(self._thumb_save)

        def store():
            self._thumb_save = 0
            cfg = load_config()
            cfg["thumb_size"] = Thumb.NAT_W
            save_config(cfg)
            return False

        self._thumb_save = GLib.timeout_add(400, store)

    # ---- multiple selection
    def _selection_bar(self):
        bar = Gtk.ActionBar(css_classes=["selection-bar"])
        self.sel_label = Gtk.Label(css_classes=["heading"])
        cancel = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                            tooltip_text="Cancel the selection (Esc)")
        cancel.connect("clicked", lambda *_: self._clear_selection())
        bar.pack_start(cancel)
        bar.pack_start(self.sel_label)
        all_btn = Gtk.Button(label="Select all", css_classes=["flat"])
        all_btn.connect("clicked", lambda *_: self._select_all())
        bar.pack_start(all_btn)
        trash = Gtk.Button(css_classes=["destructive-action"])
        trash.set_child(Adw.ButtonContent(icon_name="user-trash-symbolic", label="Delete"))
        trash.connect("clicked", lambda *_: self._batch_trash())
        bar.pack_end(trash)
        for icon, label, tip, cb in (
                ("document-save-symbolic", "Export", "Export for Discord (Exports folder)", self._batch_export),
                ("edit-copy-symbolic", "Copy", "Copy the files (paste them with Ctrl+V)", self._batch_copy),
                ("input-gaming-symbolic", "Game", "Set the game", self._batch_game),
                ("clips-star-symbolic", "Favorites", "Add to / remove from favorites", self._batch_favorite)):
            btn = Gtk.Button(css_classes=["flat"], tooltip_text=tip)
            btn.set_child(Adw.ButtonContent(icon_name=icon, label=label))
            btn.connect("clicked", lambda *_, c=cb: c())
            bar.pack_end(btn)
        self.sel_revealer = Gtk.Revealer(child=bar, transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_library_key)
        self.add_controller(keys)
        return self.sel_revealer

    def _on_library_key(self, _c, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape and self.selected and isinstance(self.nav.get_visible_page().get_child(),
                                                                       Adw.ToolbarView):
            self._clear_selection()
            return True
        return False

    def _select(self, item, on):
        (self.selected.add if on else self.selected.discard)(item.path)
        self._update_selection()

    def _update_selection(self):
        n = len(self.selected)
        self.sel_revealer.set_reveal_child(n > 0)
        self.sel_label.set_label(f"{n} clip{'s' if n > 1 else ''} selected")
        for box in self.cards:
            on = box.item.path in self.selected
            if box.sel.get_active() != on:
                box.sel.set_active(on)
            (box.add_css_class if on else box.remove_css_class)("selected")
            (box.add_css_class if n else box.remove_css_class)("selecting")

    def _clear_selection(self):
        self.selected.clear()
        self._update_selection()

    def _select_all(self):
        self.selected |= {box.item.path for box in self.cards}
        self._update_selection()

    def _selected_items(self):
        store = self.library.store
        return [store.get_item(i) for i in range(store.get_n_items()) if store.get_item(i).path in self.selected]

    def _batch_trash(self):
        items = self._selected_items()
        if not items:
            return
        n = len(items)
        dialog = Adw.AlertDialog(heading=f"Move {n} clip{'s' if n > 1 else ''} to the trash?",
                                 body="You'll be able to recover them from the trash.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("trash", "Trash")
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r == "trash":
                self._stop_preview()
                for it in items:
                    self.trash_clip(it.path, quiet=True)
                self.toast(f"{n} clip{'s' if n > 1 else ''} moved to the trash")
                self._clear_selection()

        dialog.connect("response", resp)
        dialog.present(self)

    def _batch_favorite(self):
        items = self._selected_items()
        if not items:
            return
        on = not all(it.favorite for it in items)
        for it in items:
            self._set_favorite(it, on)
        self.toast("Added to favorites" if on else "Removed from favorites")

    def _batch_game(self):
        items = self._selected_items()
        if items:
            self.pick_game(items)

    def _batch_copy(self):
        items = self._selected_items()
        if not items:
            return
        paths = [self._share_path(it) for it in items if it.path != self.library.recording_path]
        if None in paths:
            self.toast("Preparing the audio for sharing… try again in a moment")
            return
        files = Gdk.FileList.new_from_array([Gio.File.new_for_path(p) for p in paths])
        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(files))
        n = len(items)
        self.toast(f"{n} file{'s' if n > 1 else ''} copied — paste with Ctrl+V")

    def _discord_export(self, it, screen, finished):
        """Export with the Discord preset. screen: "0", "1"… (one screen) or "m" (edit following the saved
        switches; screen 1 if there are none)."""
        cfg = load_config()
        m = it.meta
        dur = max(m["duration"], 0.1)
        limit = cfg["discord_limit_mb"]
        bps = limit * 1e6 * 8 * 0.94 / dur - 160_000
        h = 1440 if bps >= 9e6 else 1080 if bps >= 5e6 else 720 if bps >= 2e6 else 480
        h = 0 if h >= m["height"] else h
        fps = 60 if bps >= 1.5e6 else 30
        fps = 0 if fps >= m["fps"] else fps
        tracks = [{"gain": cfg["levels"].get(t["kind"], 1.0), "muted": False, "points": []}
                  for t in clip_tracks(it.path, m["audio_tracks"])]
        segments = [(0.0, m["duration"])]
        screens = clip_screens(it.path)
        plan, tag = None, ""
        if len(screens) > 1:
            only = None if screen == "m" else int(screen)
            plan = video_plan(segments, clip_angles(it.path), screens, only=only)
            tag = "-edit" if only is None else f"-screen{only + 1}"
        os.makedirs(exports_dir(cfg), exist_ok=True)
        out = os.path.join(exports_dir(cfg), f"{it.stem}-{h or m['height']}p{fps or m['fps']}-{limit}mb{tag}.mp4")
        Exporter(it.path, out, segments, h, fps, "h264", limit, 22, tracks, False,
                 lambda f: None, lambda ok, err: finished(ok, err, out), normalize=cfg.get("normalize_audio", True),
                 screens=screens, plan=plan).start()

    def _quick_export(self, screen, path):
        idx = self.library.index(path)
        if idx < 0 or self._is_recording(path):
            return
        it = self.library.store.get_item(idx)
        what = "" if it.n_screens < 2 else " (edit)" if screen == "m" else f" (screen {int(screen) + 1})"
        self.toast(f"Exporting for Discord{what}…")

        def finished(ok, err, out):
            if ok:
                self.toast(f"\"{os.path.basename(out)}\" ready · {fmt_size(os.path.getsize(out))} (Exports folder)")
            elif err:
                self.toast(f"Export failed: {err}")

        def start(*_):
            if it.meta:
                self._discord_export(it, screen, finished)
            else:
                probe(it.path, lambda m: (setattr(it, "meta", m), self._discord_export(it, screen, finished)) if m
                      else self.toast("Couldn't read this clip"))

        ENCODER_DETECTOR.detect(start)

    def _batch_export(self):
        """Exports each selected clip with the Discord preset, one after the other (the edit if there is one)."""
        queue = list(self._selected_items())
        if not queue:
            return
        total, ok_count = len(queue), [0]

        def next_clip():
            if not queue:
                self.toast(f"{ok_count[0]}/{total} exported for Discord (Exports folder)")
                self._update_selection()
                return
            it = queue.pop(0)
            self.sel_label.set_label(f"Exporting for Discord… {total - len(queue)}/{total}")
            if it.meta:
                start(it)
            else:
                probe(it.path, lambda m: (setattr(it, "meta", m), start(it)) if m else next_clip())

        def start(it):
            def finished(ok, err, _out):
                ok_count[0] += 1 if ok else 0
                if not ok and err:
                    self.toast(f"{it.stem}: {err}")
                next_clip()

            self._discord_export(it, "m", finished)

        ENCODER_DETECTOR.detect(next_clip)

    # ---- game picker
    def pick_game(self, items, callback=None):
        """Game picker: games from the library, installed Steam games, free name, or none."""
        dialog = Adw.Dialog(title="Clip's game" if len(items) == 1 else f"Game of {len(items)} clips",
                            content_width=420, content_height=540)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=12, margin_end=12,
                      margin_bottom=12)
        search = Gtk.SearchEntry(placeholder_text="Search or type a game name")
        box.append(search)
        listbox = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
                              valign=Gtk.Align.START)
        box.append(Gtk.ScrolledWindow(child=listbox, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        view.set_content(box)
        dialog.set_child(view)

        store = self.library.store
        known = {}
        for i in range(store.get_n_items()):
            it = store.get_item(i)
            if it.game:
                known.setdefault(it.game, it.appid)
        for name, appid in steam_installed_games():
            known.setdefault(name, appid)
        games = {it.game for it in items}
        current = next(iter(games)) if len(games) == 1 else None

        def apply(name, appid):
            for it in items:
                try:
                    set_clip_game(it.path, name, appid)
                except OSError as e:
                    self.toast(f"Couldn't do it: {e.strerror}")
                    return
                it.load_info()
                it.emit("changed")
            self.side_key = None
            self._schedule_rebuild()
            self.toast(f"Game set: {name}" if name else "Game removed")
            dialog.close()
            if callback:
                callback()

        def row(name, appid, subtitle=None, icon=None):
            r = Adw.ActionRow(title=GLib.markup_escape_text(name or "No game"), activatable=True)
            if subtitle:
                r.set_subtitle(subtitle)
            tex = texture(game_art(appid).get("icon")) if appid else None
            if tex:
                r.add_prefix(Gtk.Picture(paintable=tex, width_request=28, height_request=28, can_shrink=True,
                                         valign=Gtk.Align.CENTER, css_classes=["game-icon"]))
            else:
                r.add_prefix(Gtk.Image(icon_name=icon or "input-gaming-symbolic", pixel_size=20, width_request=28))
            if name and name == current:
                r.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
            r.connect("activated", lambda *_: apply(name, appid))
            return r

        def fill(*_):
            while (c := listbox.get_first_child()):
                listbox.remove(c)
            q = search.get_text().strip()
            if q and q.lower() not in (k.lower() for k in known):
                listbox.append(row(q, None, subtitle="Use this name", icon="list-add-symbolic"))
            if not q:
                listbox.append(row(None, None, subtitle="Remove the game", icon="window-close-symbolic"))
            for name in sorted(known, key=str.lower):
                if not q or q.lower() in name.lower():
                    listbox.append(row(name, known[name]))

        def on_activate(*_):
            q = search.get_text().strip()
            if q:
                match = next((k for k in known if k.lower() == q.lower()), None)
                apply(match or q, known.get(match) if match else None)

        search.connect("search-changed", fill)
        search.connect("activate", on_activate)
        fill()
        dialog.present(self)

    # ---- renaming from the library
    def rename_dialog(self, item):
        dialog = Adw.AlertDialog(heading="Rename the clip")
        entry = Gtk.Entry(text=item.stem, activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")

        def resp(_d, r):
            if r != "rename":
                return
            try:
                rename_clip(item.path, entry.get_text())
            except FileExistsError:
                self.toast("A clip already has this name")
            except OSError as e:
                self.toast(f"Couldn't rename: {e.strerror}")

        dialog.connect("response", resp)
        dialog.present(self)
        entry.grab_focus()
        entry.select_region(0, -1)

    def _set_favorite(self, item, on):
        if getattr(item, "favorite", False) == on:
            return
        item.favorite = on
        try:
            set_clip_favorite(item.path, on)
        except OSError as e:
            self.toast(f"Couldn't do it: {e.strerror}")
            return
        item.emit("changed")   # only updates the card (no rebuild: no flicker)
        self.side_key = None
        if self.game_filter == "fav":
            self._schedule_rebuild()   # the Favorites view must remove / add the clip
        else:
            store = self.library.store
            self._rebuild_sidebar([store.get_item(i) for i in range(store.get_n_items())])

    def _card_menu(self, box, x, y):
        item = box.item
        if self._is_recording(item.path):
            return
        menu = Gio.Menu()
        target = GLib.Variant("s", item.path)
        for entries in ((("Edit", "win.clip-edit"), ("Play fullscreen", "win.clip-fullscreen"),
                         ("Rename…", "win.clip-rename"), ("Set the game…", "win.clip-game"),
                         ("Remove from favorites" if item.favorite else "Add to favorites", "win.clip-fav"),
                         ("Select", "win.clip-select")),
                        (("Copy the file", "win.clip-copy"), ("Show in folder", "win.clip-show")),
                        (("Move to the trash", "win.clip-trash"),)):
            section = Gio.Menu()
            for label, action in entries:
                mi = Gio.MenuItem.new(label, None)
                mi.set_action_and_target_value(action, target)
                section.append_item(mi)
            menu.append_section(None, section)
        # Quick export for Discord (Discord preset): one screen of your choice, or the edit if there are switches
        quick = Gio.Menu()
        if item.n_screens > 1:
            screens = clip_screens(item.path)
            for k, sc in enumerate(screens):
                quick.append_item(self._menu_item(f"Screen {k + 1} only — {sc['monitor']}", "win.clip-export",
                                                  f"{k}\n{item.path}"))
            if len(clip_angles(item.path)) > 1:
                quick.append_item(self._menu_item("Edit (the editor's switches)", "win.clip-export",
                                                  f"m\n{item.path}"))
            section = Gio.Menu()
            section.append_submenu("Export for Discord", quick)
            menu.insert_section(1, None, section)
        else:
            quick.append_item(self._menu_item("Export for Discord", "win.clip-export", f"0\n{item.path}"))
            menu.insert_section(1, None, quick)
        pop = Gtk.PopoverMenu(menu_model=menu, has_arrow=False, halign=Gtk.Align.START)
        pop.set_parent(box)
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = int(x), int(y), 1, 1
        pop.set_pointing_to(r)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    @staticmethod
    def _menu_item(label, action, value):
        mi = Gio.MenuItem.new(label, None)
        mi.set_action_and_target_value(action, GLib.Variant("s", value))
        return mi

    def _card_fill(self, box, item):
        pic, badge, chip, chip_icon, chip_label, title, sub = box.widgets
        recording = item.path == self.library.recording_path
        box.rec.set_visible(recording)
        (box.add_css_class if recording else box.remove_css_class)("recording")
        for w in (box.play, box.full, box.star, box.sel, box.trash):
            w.set_visible(not recording)
        previewing = bool(getattr(self, "preview", None)) and self.preview[0] is box
        if not previewing:   # during a preview, the video stays displayed
            pic.set_paintable(item.texture)
        box.filling = True
        box.star.set_active(item.favorite)
        box.star.get_child().set_from_icon_name("clips-fav-on" if item.favorite else "clips-fav-off")
        box.star.set_tooltip_text("Remove from favorites" if item.favorite else "Add to favorites")
        (box.star.add_css_class if item.favorite else box.star.remove_css_class)("on")
        box.filling = False
        if item.meta:
            d = item.meta["duration"]
            badge.set_label(f"{int(d // 60)}:{int(d % 60):02d}")
        badge.set_visible(bool(item.meta) and not recording)
        icon = texture(game_art(item.appid).get("icon")) if item.game else None
        chip_icon.set_paintable(icon)
        chip_icon.set_visible(icon is not None)
        chip_label.set_label(item.game or "")
        chip.set_visible(bool(item.game) and self.cfg.get("detect_game", True)
                         and not self.game_filter.startswith("g:"))   # pointless on a game's page
        title.set_label(item.stem)
        box.set_tooltip_text(item.name)
        parts = [time.strftime("%H:%M", time.localtime(item.mtime))]
        if item.meta:
            parts.append(f"{item.meta['height']}p{item.meta['fps']}")
        parts.append(fmt_size(item.size))
        if item.n_screens > 1:
            parts.append(f"{item.n_screens} screens")
        sub.set_label("Recording…" if recording else " · ".join(parts))

    def open_clip(self, path, fullscreen=False):
        if self._is_recording(path):
            return
        self._stop_preview()
        top = self.nav.get_visible_page()
        if isinstance(top, EditorPage):
            if top.item.path == path:
                return
            self.nav.pop()
        idx = self.library.index(path)
        try:
            item = self.library.store.get_item(idx) if idx >= 0 else ClipItem(path)
        except OSError:
            self.toast("Clip not found")
            return
        self.nav.push(EditorPage(self, item, fullscreen_only=fullscreen))

    # ---- replay service
    def refresh_recorder_state(self):
        def done(ok, out, _e):
            active = out.decode().strip() == "active"
            cfg = load_config()
            self.rec_switch.handler_block(self.rec_handler)
            self.rec_switch.set_active(active)
            self.rec_switch.handler_unblock(self.rec_handler)
            if active:
                self.rec_dot.add_css_class("on")
                self.rec_label.set_label(f"Replay {cfg['replay_seconds']} s")
                self.rec_switch.set_tooltip_text(f"Replay running — {SHORTCUT_LABEL} to save · "
                                                 f"{shortcut_label('rec_start')} / {shortcut_label('rec_stop')} to "
                                                 "start / stop a recording")
            else:
                self.rec_dot.remove_css_class("on")
                self.rec_label.set_label("Replay off")
                self.rec_switch.set_tooltip_text("Turn on continuous recording")

        run_async(["systemctl", "--user", "is-active", SERVICE], done)

    def _poll_manual_recording(self):
        started = manual_recording_started()
        self.manual_pill.set_visible(started is not None)
        path = manual_recording_path() if started is not None else None
        if path != self.library.recording_path:
            old, self.library.recording_path = self.library.recording_path, path
            if old and os.path.exists(old):   # recording finished: the complete clip "appears"
                idx = self.library.index(old)
                if idx >= 0:
                    try:
                        it = self.library.store.get_item(idx)
                        it.refresh()
                        GLib.timeout_add(4000, lambda: (self.library.reread(it), False)[1])
                    except OSError:
                        pass
            if path and self.library.index(path) < 0:   # without waiting for the folder to report the new file
                self.library._add(path)
            self._schedule_rebuild()
        if started is not None:
            t = max(0, int(time.time() - started))
            self.manual_label.set_label(f"REC {t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600
                                        else f"REC {t // 60}:{t % 60:02d}")
        return True

    def _toggle_recorder(self, sw, _):
        verb = "enable" if sw.get_active() else "disable"

        def done(ok, _o, err):
            if not ok:
                self.toast(f"Error: {err.strip() or verb}")
            GLib.timeout_add(800, lambda: (self.refresh_recorder_state(), False)[1])

        run_async(["systemctl", "--user", verb, "--now", SERVICE], done)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)

    def do_startup(self):
        Adw.Application.do_startup(self)
        # The app's own icons (selection start / end…), recolored to match the theme.
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons"))
        Gtk.Window.set_default_icon_name(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        ENCODER_DETECTOR.detect()   # in the background, result cached

    def do_shutdown(self):
        # Stop GStreamer before the process ends (otherwise its threads crash on exit).
        for win in self.get_windows():
            page = win.nav.get_visible_page() if isinstance(win, MainWindow) else None
            if isinstance(page, EditorPage):
                page.destroy_player()
        Adw.Application.do_shutdown(self)

    def do_activate(self):
        pass

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        win = self.props.active_window or MainWindow(self)
        win.present()
        if len(args) >= 2 and args[0] == "--edit":
            path = args[1] if os.path.isabs(args[1]) else os.path.join(cmdline.get_cwd() or "", args[1])
            win.open_clip(path)
        return 0


if __name__ == "__main__":
    Gst.init(None)
    sys.exit(App().run(sys.argv))
