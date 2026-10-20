"""A clip's editing page: playback, trimming (start/end + removed sections), audio (simple or advanced mode),
export and sharing."""
import json
import os
import shutil
import tempfile
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

from clips_common import (SOURCE_ICONS, SOURCE_LABELS, clip_angles, clip_screens, exports_dir,  # noqa: E402
                          game_art, load_clip_info, load_clip_meta, load_config, rename_clip, save_config,
                          set_clip_angles, set_clip_field)
from media import (SHARE, BACKEND_LABELS, ENCODER_DETECTOR, JOBS, Exporter, Player, angle_at, cache_key,  # noqa: E402
                   probe, screen_range, video_info, video_plan)
from widgets import (Lane, PlayGlyph, ScreenRow, ShareTile, Timeline, TimelineCanvas, fmt_size, merge_ranges,  # noqa: E402
                     show_in_folder, texture)

THUMB_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "clips", "thumbs")
KIND_COLORS = {"game": (0.21, 0.52, 0.89), "chat": (0.57, 0.38, 0.92),
               "mic": (0.18, 0.76, 0.49), "other": (0.90, 0.65, 0.04)}
APP_COLORS = [(0.96, 0.47, 0.0), (0.88, 0.24, 0.55), (0.11, 0.70, 0.75), (0.93, 0.76, 0.13), (0.62, 0.45, 0.30)]
SIDE_W = 390          # right panel: default width (never changes with its content)
SIDE_MIN, SIDE_MAX = 330, 680   # bounds of the resize handle
VIDEO_MIN = 220       # minimum height of the preview (its handle doesn't go above it)
TRACKS_MIN = 64       # minimum height of the tracks area (its handle doesn't go below it)

HELP_SIMPLE = ("<b>Track volume</b>\n"
               "Each audio track of the clip (game, voice chat, mic…) has its own slider, from 0 to 200 %.\n"
               "The setting is heard right away and will be applied to the export.\n\n"
               "<b>Speaker</b>: mute the track\n"
               "<b>S</b> (solo): listen to this track only\n\n"
               "<b>Advanced mode</b> lets you vary the volume over the course of the clip.")
HELP_ADVANCED = ("<b>Volume curves</b>\n"
                 "The line drawn on each track is its volume over time:\n"
                 "the higher it is, the louder the track.\n\n"
                 "<b>Click</b> on the track: add a point\n"
                 "<b>Drag</b> a point: move it (up = louder)\n"
                 "<b>Right click</b> or <b>double click</b> on a point: delete it\n"
                 "<b>Eraser</b>: clear all the points of the track\n"
                 "<b>Speaker</b>: mute · <b>S</b>: solo\n"
                 "<b>Ctrl + wheel</b> on a track: change their height")
HEADER_W = 168        # track headers column (advanced mode): name + 3 small buttons

# Names designed around use; the 3rd value is the sentence shown at the top of the summary.
PRESETS = [
    ("discord", "Discord", "To send it on Discord without Nitro"),
    ("hq", "HQ", "High quality: original resolution and smoothness, very lightly compressed"),
    ("light", "Compact", "Small file, quick to send anywhere"),
    ("copy", "Original", "Original quality, instant: nothing is re-encoded"),
]
HEIGHTS = [0, 2160, 1440, 1080, 720, 480]   # 0 = original
FPS_CHOICES = [0, 120, 60, 30]               # 0 = original
CODECS = [("h264", "H.264 (works everywhere)"), ("hevc", "HEVC"), ("av1", "AV1")]


def fmt_clock(t):
    """0:05.2 — minutes without a useless zero, tenths of a second."""
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}"


def clip_tracks(path, n):
    """A clip's audio tracks: sidecar file written on save, otherwise guess."""
    tracks = load_clip_meta(path)
    if len(tracks) == n:
        return tracks
    guess = {1: ["game"], 2: ["game", "mic"], 3: ["game", "chat", "mic"]}.get(n)
    if guess:
        out = [{"kind": k, "label": SOURCE_LABELS[k], "source": ""} for k in guess]
        if n == 2:
            out[0]["label"] = "System audio"
        return out
    return [{"kind": "other", "label": f"Track {i + 1}", "source": ""} for i in range(n)]


def share_gains(path, n, cfg):
    """Gain of each track for the shareable version (default levels from the preferences)."""
    return [cfg["levels"].get(t["kind"], 1.0) for t in clip_tracks(path, n)]


def source_hint(src):
    if src.startswith("app-inverse:"):
        return "Everything else"
    if src.startswith("app:"):
        return "Application"
    if src.startswith("device:"):
        return "Dedicated device"
    return {"default_output": "All PC audio", "default_input": "Default input"}.get(src, "")


def no_focus(*widgets):
    """Buttons don't take focus on click: Space stays "play/pause"."""
    for w in widgets:
        w.set_focus_on_click(False)
    return widgets[0] if len(widgets) == 1 else widgets


# ---------------------------------------------------------------- audio tracks

class TrackState(GObject.Object):
    """A track's state shared between simple mode (slider) and advanced mode (header + curve)."""
    level = GObject.Property(type=float, default=100.0)   # % (0–200)
    mute = GObject.Property(type=bool, default=False)
    solo = GObject.Property(type=bool, default=False)

    def __init__(self, info, index, app_index, level):
        super().__init__()
        self.info = info
        self.index = index
        kind = info["kind"] if info["kind"] in KIND_COLORS or info["kind"] == "app" else "other"
        self.kind = kind
        if kind == "app":
            self.color = APP_COLORS[app_index % len(APP_COLORS)]
            self.css = f"kind-app{app_index % len(APP_COLORS)}"
        else:
            self.color, self.css = KIND_COLORS[kind], f"kind-{kind}"
        self.icon = SOURCE_ICONS.get(kind, SOURCE_ICONS["other"])
        self.level = round(level * 100)


def _track_title(state, expand):
    """Color dot + icon + name on a single line; the source is in the tooltip."""
    box = Gtk.Box(spacing=6, hexpand=expand)
    box.append(Gtk.Box(css_classes=["track-dot", state.css], width_request=4, height_request=18,
                       valign=Gtk.Align.CENTER))
    box.append(Gtk.Image(icon_name=state.icon, pixel_size=14))
    box.append(Gtk.Label(label=state.info["label"], xalign=0, css_classes=["heading"], ellipsize=3,
                         hexpand=expand))
    hint = source_hint(state.info.get("source", ""))
    box.set_tooltip_text(f"{state.info['label']} — {hint}" if hint else state.info["label"])
    return box


def _toggle(state, prop, **kw):
    btn = Gtk.ToggleButton(css_classes=["flat", "compact"], valign=Gtk.Align.CENTER, **kw)
    state.bind_property(prop, btn, "active", GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
    no_focus(btn)
    return btn


def _mute_button(state):
    btn = _toggle(state, "mute", tooltip_text="Mute")

    def icon(*_):
        btn.set_icon_name("audio-volume-muted-symbolic" if state.mute else "audio-volume-high-symbolic")

    state.connect("notify::mute", icon)
    icon()
    return btn


class SimpleRow(Gtk.Box):
    """Simple mode, one row: name · volume 0–200 % · mute · solo.
    Names share the width of the longest one (size group): aligned sliders, no extra space."""

    def __init__(self, state, names_group=None):
        super().__init__(spacing=8, css_classes=["track-row"])
        title = _track_title(state, False)
        if names_group:
            names_group.add_widget(title)
        self.append(title)
        solo = _toggle(state, "solo", label="S", tooltip_text="Solo: listen to this track only")
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 200, 5)
        scale.set_hexpand(True)
        scale.set_valign(Gtk.Align.CENTER)
        scale.add_mark(100, Gtk.PositionType.BOTTOM, None)
        state.bind_property("level", scale.get_adjustment(), "value",
                            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        no_focus(scale)
        pct = Gtk.Label(width_chars=5, xalign=1, css_classes=["numeric", "caption"])
        state.bind_property("level", pct, "label", GObject.BindingFlags.SYNC_CREATE,
                            lambda _b, v: f"{int(v)} %")
        buttons = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        buttons.append(_mute_button(state))
        buttons.append(solo)
        for w in (scale, pct, buttons):
            self.append(w)


class LaneHeader(Gtk.Box):
    """Advanced mode: header on the left of a track (like in an editing program)."""

    def __init__(self, state, lane):
        super().__init__(spacing=4, css_classes=["lane-header"], height_request=Lane.HEIGHT,
                         width_request=HEADER_W)
        self.append(_track_title(state, True))
        solo = _toggle(state, "solo", label="S", tooltip_text="Solo")
        clear = no_focus(Gtk.Button(icon_name="edit-clear-symbolic", css_classes=["flat", "compact"],
                                    valign=Gtk.Align.CENTER, tooltip_text="Clear the volume points"))
        clear.connect("clicked", lambda *_: (lane.set_points([]), lane._changed()))
        buttons = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        for w in (_mute_button(state), solo, clear):
            buttons.append(w)
        self.append(buttons)


# ---------------------------------------------------------------- editor

class EditorPage(Adw.NavigationPage):
    def __init__(self, window, item, fullscreen_only=False):
        # No fixed tag: two editors can coexist for the duration of a transition.
        super().__init__(title=item.stem)
        self.fullscreen_only = fullscreen_only   # opened just to watch fullscreen
        self.win = window
        self.item = item
        self.meta = item.meta
        self.cfg = load_config()
        self.player = None
        self.tick = 0
        self.loop = True
        self.exporter = None
        self.updating = False
        self.states = []
        self.lanes = []
        self.cuts = []
        # Undo / redo: snapshots of the whole edit (excerpt, cuts, switches, tracks, curves)
        self.undo_stack, self.redo_stack = [], []
        self.committed = None        # state after the last recorded step
        self.restoring = False
        self.commit_timer = 0
        self.last_time = None
        self.scrub_resume = False   # playback resumes after dragging the playhead
        self.cut_start = None       # start of a section to remove set with the keyboard (C)
        self.export_sig = load_clip_info(item.path).get("export_sig")   # edit of the last export
        self.fullscreen = False
        # Several screens: the clip (screen 1, with audio) + the others, played in parallel, without audio
        self.screens = clip_screens(item.path)
        self.multi = len(self.screens) > 1
        self.angles = [a for a in clip_angles(item.path) if a[1] < len(self.screens)] or [[0.0, 0]]
        self.vplayers = {}
        self.shown = 0
        self.follow_tick = 0
        self.preview_screen = None   # preview: None = the edit, k = always screen k (doesn't affect the edit)
        self.rows = []

        self.connect("hidden", lambda *_: self.destroy_player())
        if self.meta:
            self._build()
        else:
            self.set_child(Adw.Spinner())
            probe(item.path, self._got_meta)

    def _got_meta(self, meta):
        if not meta:
            self.win.toast("Couldn't read this clip")
            return
        self.meta = self.item.meta = meta
        self._build()

    # ------------------------------------------------------------ construction
    def _build(self):
        m = self.meta
        dur = m["duration"]
        self.t_in, self.t_out = 0.0, dur
        n = m["audio_tracks"]
        tracks = clip_tracks(self.item.path, n)
        advanced = self.cfg["advanced_audio"]

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_label = Gtk.EditableLabel(text=self.item.stem, tooltip_text="Click to rename",
                                             xalign=0.5, width_chars=28, max_width_chars=40)
        self.title_label.connect("notify::editing", self._on_rename)
        title = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        art = game_art(getattr(self.item, "appid", None))
        icon = texture(art.get("icon"))
        if icon:
            # Gtk.Image + pixel_size: a truly fixed size (a Picture takes the image's size, 32 px)
            title.append(Gtk.Image(paintable=icon, pixel_size=18, valign=Gtk.Align.CENTER,
                                   tooltip_text=self.item.game, css_classes=["game-icon"]))
        title.append(self.title_label)
        self.header_title = title
        header.set_title_widget(title)
        delete = no_focus(Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Move the clip to the trash"))
        delete.connect("clicked", self._delete)
        folder = no_focus(Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Show in folder"))
        folder.connect("clicked", lambda *_: show_in_folder(self.item.path))
        header.pack_end(delete)
        header.pack_end(folder)
        self.fav_btn = no_focus(Gtk.ToggleButton(css_classes=["flat", "fav-toggle"]))
        self.fav_btn.connect("toggled", self._on_favorite)
        header.pack_end(self.fav_btn)
        self._sync_favorite()
        toolbar.add_top_bar(header)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True,
                       margin_start=18, margin_end=18, margin_top=6, margin_bottom=18)

        # video
        self.player = Player(self.item.path, n, m.get("vcodec", ""), on_eos=self._on_eos,
                             on_error=lambda e: self.win.toast(e))
        self.player.set_monitor(float(self.cfg.get("preview_volume", 1.0)))
        for k, sc in enumerate(self.screens[1:], 1):
            self.vplayers[k] = Player(sc["path"], 0, "h264")
            sc["range"] = screen_range(sc)
        picture = Gtk.Picture(paintable=self.player.paintable, content_fit=Gtk.ContentFit.CONTAIN,
                              vexpand=True, css_classes=["clip-video"], overflow=Gtk.Overflow.HIDDEN,
                              height_request=220)
        overlay = Gtk.Overlay(child=picture, vexpand=True)
        self.big_play = no_focus(Gtk.Button(css_classes=["big-play"], halign=Gtk.Align.CENTER,
                                            valign=Gtk.Align.CENTER, tooltip_text="Play (Space)"))
        self.big_play.set_child(PlayGlyph(26))
        self.big_play.connect("clicked", lambda *_: self.toggle_play())
        overlay.add_overlay(self.big_play)
        self.fs_btn = no_focus(Gtk.Button(icon_name="view-fullscreen-symbolic", css_classes=["osd", "circular"],
                                          halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=12,
                                          margin_bottom=12, tooltip_text="Fullscreen (F)"))
        self.fs_btn.connect("clicked", lambda *_: self.toggle_fullscreen())
        overlay.add_overlay(self.fs_btn)
        overlay.add_overlay(self._build_listen_volume())
        self._build_fullscreen_osd(overlay, dur)
        self.preview_group = None
        self.preview_note = None
        if self.multi:
            # Which screen to watch in the preview (Ctrl+1, Ctrl+2; Ctrl+0 = the edit): handy to place the
            # switches frame-accurately. Doesn't change the edit.
            self.preview_group = no_focus(Adw.ToggleGroup(css_classes=["osd", "preview-pick"], halign=Gtk.Align.START,
                                                          valign=Gtk.Align.START, margin_start=12, margin_top=12))
            self.preview_group.add(Adw.Toggle(name="m", label="Edit",
                                              tooltip="Preview of the edit (Ctrl+0)"))
            for k, sc in enumerate(self.screens):
                self.preview_group.add(Adw.Toggle(name=str(k), label=f"Screen {k + 1}",
                                                  tooltip=f"Preview of screen {k + 1} only — {sc['monitor']} (Ctrl+{k + 1})"))
            self.preview_group.set_active_name("m")
            self.preview_group.connect("notify::active-name", self._on_preview_pick)
            overlay.add_overlay(self.preview_group)
            self.preview_note = Gtk.Label(css_classes=["osd", "preview-note"], halign=Gtk.Align.CENTER,
                                          valign=Gtk.Align.START, margin_top=56, visible=False, can_target=False)
            overlay.add_overlay(self.preview_note)
        self.picture = picture
        click = Gtk.GestureClick()
        click.connect("released", self._on_video_click)
        picture.add_controller(click)
        main.append(overlay)

        # transport
        controls = Gtk.Box(spacing=6)
        to_start = no_focus(Gtk.Button(icon_name="media-skip-backward-symbolic", css_classes=["flat", "circular"],
                                       tooltip_text="Back to the start of the excerpt (Home)"))
        to_start.connect("clicked", lambda *_: self._seek_to(self.t_in))
        self.play_btn = no_focus(Gtk.Button(css_classes=["circular", "suggested-action", "play-btn"],
                                            tooltip_text="Play / pause (Space)"))
        self.play_glyph = PlayGlyph(15)
        self.play_btn.set_child(self.play_glyph)
        self.play_btn.connect("clicked", lambda *_: self.toggle_play())
        self.time_label = Gtk.Label(css_classes=["numeric"], margin_start=6, width_chars=17, xalign=0,
                                    tooltip_text="← / →: frame by frame · Shift+← / Shift+→: 1 second")
        # Contextual help (keyboard cut in progress) then final duration: only visible when useful.
        self.cut_hint = Gtk.Label(xalign=0, hexpand=True, css_classes=["caption", "dim-label"], ellipsize=3,
                                  width_chars=1, margin_start=8)
        self.final_label = Gtk.Label(css_classes=["numeric"], use_markup=True, margin_end=8, visible=False,
                                     tooltip_text="Duration of the clip once exported (excerpt minus the removed sections)")
        for w in (to_start, self.play_btn, self.time_label, self.cut_hint, self.final_label):
            controls.append(w)
        marks = Gtk.Box(css_classes=["linked"])
        for icon, tip, which in (("clips-mark-in-symbolic", "Start the excerpt here (I)", "in"),
                                 ("clips-reset-range-symbolic", "Keep everything: original start and end", "reset"),
                                 ("clips-mark-out-symbolic", "End the excerpt here (O)", "out")):
            b = no_focus(Gtk.Button(icon_name=icon, tooltip_text=tip))
            b.connect("clicked", lambda _b, w=which: self.mark(w))
            marks.append(b)
        self.cut_btn = no_focus(Gtk.Button(
            css_classes=["flat"],
            tooltip_text="Remove a 2 s section at the playhead (X)\n"
                         "On the timeline: drag a section to move it, pull its edges to adjust it,\n"
                         "right click to delete it · Shift + drag to draw one\n"
                         "With the keyboard: C = start of the section, V = end"))
        self.cut_btn.set_child(Adw.ButtonContent(icon_name="edit-cut-symbolic", label="Cut"))
        self.cut_btn.connect("clicked", lambda *_: self._add_cut_here())
        loop = no_focus(Gtk.ToggleButton(icon_name="clips-loop-symbolic", active=True,
                                         tooltip_text="Loop the excerpt", css_classes=["flat", "circular"]))
        loop.connect("toggled", self._on_loop)
        self.screen_group = None
        if self.multi:
            # Screen shown at the playhead; a click (or 1 / 2) switches from here on.
            self.screen_group = Adw.ToggleGroup(css_classes=["round"], valign=Gtk.Align.CENTER, margin_end=6)
            for k, sc in enumerate(self.screens):
                self.screen_group.add(Adw.Toggle(
                    name=str(k), label=f"Screen {k + 1}",
                    tooltip=f"{sc['monitor'] or 'Screen ' + str(k + 1)} — show this screen from the "
                            f"playhead on (key {k + 1})\nB: switch here to the screen watched in the preview "
                            f"(otherwise the next one)"))
            self.screen_group.set_active_name("0")
            self.screen_group.connect("notify::active-name", self._on_screen_toggle)
            controls.append(Gtk.Label(label="Edit", css_classes=["caption", "dim-label"], margin_end=2))
            controls.append(no_focus(self.screen_group))
            # Timeline view: one band (switches) or one track per screen, stacked
            self.view_group = no_focus(Adw.ToggleGroup(css_classes=["round"], valign=Gtk.Align.CENTER, margin_end=6))
            self.view_group.add(Adw.Toggle(name="band", icon_name="view-continuous-symbolic",
                                           tooltip="One timeline: the edit's screen in color under the frames"))
            self.view_group.add(Adw.Toggle(name="tracks", icon_name="view-list-symbolic",
                                           tooltip="One track per screen, stacked (click on a track: this screen "
                                                   "for this section)"))
            self.view_group.set_active_name(self.cfg.get("multiscreen_view", "band"))
            self.view_group.connect("notify::active-name", self._on_view)
            controls.append(self.view_group)
        for w in (marks, self.cut_btn, loop):
            controls.append(w)
        main.append(controls)
        self.controls = controls
        self.main = main

        # timeline (+ advanced-mode tracks, aligned below, with their headers on the left)
        self.timeline = Timeline(dur, on_seek=self._on_seek, on_range=self._on_range,
                                 on_cuts=self._set_cuts_user, on_cut_clicked=self._restore_cut,
                                 on_angles=self._set_angles)
        if self.multi:
            self.timeline.canvas.set_angles(self.angles)
            for k, sc in enumerate(self.screens):
                row = ScreenRow(k, sc["monitor"] or f"Screen {k + 1}", self.timeline.canvas,
                                self._row_view, self._row_switch)
                row.range = sc.get("range", (0.0, dur)) if k else (0.0, dur)
                self.rows.append(row)
                self.timeline.add_screen_row(row)
        canvas_h = TimelineCanvas.KNOB_H + TimelineCanvas.STRIP_H + TimelineCanvas.RULER_H
        # Advanced mode: track headers on the left (under an empty corner as tall as the top band),
        # tracks in the timeline, and a single scrollbar on the right. Headers and tracks share
        # the same vertical adjustment: they scroll together, the top band doesn't move.
        vadj = self.timeline.lanes_scroll.get_vadjustment()
        self.headers = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Timeline.LANE_GAP)
        self.headers_scroll = Gtk.ScrolledWindow(child=self.headers, vadjustment=vadj,
                                                 hscrollbar_policy=Gtk.PolicyType.NEVER,
                                                 vscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                                 propagate_natural_width=True)
        self.headers_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Timeline.LANE_GAP,
                                   width_request=HEADER_W, margin_end=8, hexpand=False, visible=False)
        self.headers_spacer = Gtk.Box(height_request=canvas_h)   # facing the top band (+ screen tracks)
        self.headers_col.append(self.headers_spacer)
        self.headers_col.append(self.headers_scroll)
        # Scrollbar column: always reserved in advanced mode (the timeline doesn't change width when
        # the scrollbar becomes useful) and aligned with the tracks only, not with the top band.
        # Compact slider (lanes-bar class): with Adwaita's (≈ 56 px including margins), a short tracks area
        # leaves no travel and the scrollbar jumps from one end to the other.
        self.vbar = Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL, adjustment=vadj, vexpand=True,
                                  css_classes=["lanes-bar"])
        self.vbar_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Timeline.LANE_GAP, margin_start=6,
                                visible=False)
        self.vbar_spacer = Gtk.Box(height_request=canvas_h)
        self.vbar_col.append(self.vbar_spacer)
        self.vbar_col.append(self.vbar)
        vadj.connect("changed", lambda *_: self._update_vbar())
        edit = Gtk.Box()
        edit.append(self.headers_col)
        edit.append(self.timeline)
        edit.append(self.vbar_col)
        main.append(edit)
        self.edit_box = edit
        self.canvas_h = canvas_h

        # audio
        self._load_strip()
        GLib.idle_add(lambda: (self._load_peaks(), False)[1])   # tracks created below        self.adv_switch = None
        if n:
            # Always last (simple or advanced mode): the switch never moves.
            # "Audio" takes the width of the track headers column: the help text starts
            # at the same left edge as the timeline and the advanced-mode tracks.
            head = Gtk.Box(spacing=8, margin_top=4)
            head.append(Gtk.Label(label="Audio", xalign=0, css_classes=["heading"], width_request=HEADER_W))
            self.help_label = Gtk.Label(xalign=0, use_markup=True, wrap=True, max_width_chars=48,
                                        css_classes=["help-pop"])
            help_btn = no_focus(Gtk.MenuButton(icon_name="help-about-symbolic", css_classes=["flat", "circular"],
                                               valign=Gtk.Align.CENTER, tooltip_text="How does it work?",
                                               popover=Gtk.Popover(child=self.help_label)))
            head.append(help_btn)
            head.append(Gtk.Box(hexpand=True))
            self.height_box = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER,
                                      tooltip_text="Track height (Ctrl + wheel on a track)")
            self.height_box.append(Gtk.Image(icon_name="clips-lane-height-symbolic", pixel_size=14,
                                             css_classes=["dim-label"]))
            self.height_scale = no_focus(Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 36, 180, 2))
            self.height_scale.set_value(self.cfg.get("lane_height", 46))
            self.height_scale.set_size_request(90, -1)
            self.height_scale.connect("value-changed", self._on_height_changed)
            self.height_box.append(self.height_scale)
            head.append(self.height_box)
            head.append(Gtk.Label(label="Advanced mode", css_classes=["caption"]))
            self.adv_switch = no_focus(Gtk.Switch(active=advanced, valign=Gtk.Align.CENTER,
                                                  tooltip_text="Tracks under the timeline with volume curves"))
            self.adv_switch.connect("notify::active", self._on_advanced)
            head.append(self.adv_switch)

            self.simple_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
            names_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
            app_i = 0
            for i, info_t in enumerate(tracks):
                state = TrackState(info_t, i, app_i, self.cfg["levels"].get(info_t["kind"], 1.0))
                app_i += info_t["kind"] == "app"
                for prop in ("level", "mute", "solo"):
                    state.connect(f"notify::{prop}", lambda *_: (self._on_mix(), self._changed()))
                self.states.append(state)
                self.simple_list.append(SimpleRow(state, names_group))
                lane = Lane(dur, state.color, lambda _p, live: (self._on_mix(live), self._changed()))
                lane.set_advanced(True)
                scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
                scroll.connect("scroll", self._on_lane_scroll)
                lane.add_controller(scroll)
                self.lanes.append(lane)
                self.timeline.add_lane(lane)
                self.headers.append(LaneHeader(state, lane))
            # Simple mode: the list takes exactly the space of the advanced-mode tracks (see _set_edit_h),
            # so switching mode changes neither the window size nor the panels' size.
            self.simple_box = Gtk.ScrolledWindow(child=self.simple_list, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                                 overlay_scrolling=False)
            main.append(self.simple_box)
            simple_h = self.simple_list.measure(Gtk.Orientation.VERTICAL, -1)[1]
            n_l = len(self.lanes)
            win_h = self.win.get_height() or 860
            area = max(160, min(460, int(win_h * 0.45)))
            want = canvas_h + n_l * (self.cfg.get("lane_height", 46) + Timeline.LANE_GAP)
            self.edit_h_default = max(canvas_h + simple_h + main.get_spacing(), min(area, want))
            self.edit_h = self.cfg.get("tracks_height") or self.edit_h_default
            main.append(head)

        # Under the video: controls, timeline, summary and Audio row always stay visible; only the tracks
        # area changes height. The handle enlarges the preview by shrinking that area (which then scrolls).
        # The whole bottom has a fixed height: the video alone takes the free space, so the handle follows
        # the mouse exactly.
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=main.get_spacing(), vexpand=False)
        child = overlay.get_next_sibling()
        while child:
            nxt = child.get_next_sibling()
            main.remove(child)
            bottom.append(child)
            child = nxt
        main.set_spacing(0)
        self.bottom = bottom
        if n:   # simple-mode tracks right under the timeline, like the advanced-mode ones: the summary and
            #     Audio row are in the same place in both modes
            bottom.reorder_child_after(self.simple_box, self.edit_box)
        self.video_box = overlay
        self.vhandle = None
        if n:
            self.vhandle = self._make_handle(
                Gtk.Orientation.HORIZONTAL, "row-resize",
                "Drag down to enlarge the preview\nDouble click: original size",
                self._on_vhandle_begin, self._on_vhandle_update, self._store_edit_h,
                lambda: (self._set_edit_h(self.edit_h_default), self._store_edit_h(reset=True)))
            main.append(self.vhandle)
        else:
            bottom.set_margin_top(10)
        main.append(bottom)
        self.fs_hidden = [w for w in (self.vhandle, bottom) if w]

        content = Gtk.Box()
        content.append(main)
        # Handle between the page and the Export panel: drag to widen / narrow it.
        handle = self._make_handle(Gtk.Orientation.VERTICAL, "col-resize",
                                   "Drag to widen or narrow the panel\nDouble click: original size",
                                   self._on_handle_begin, self._on_handle_update, self._store_side_width,
                                   lambda: (self._set_side_width(SIDE_W), self._store_side_width()))
        content.append(handle)
        # Explicit hexpand: otherwise it inherits it from its children, and the free space would be shared with
        # the page (the handle would no longer follow the mouse).
        side = Adw.Clamp(child=self._build_side(), hexpand=False)
        content.append(side)
        toolbar.set_content(content)
        self.set_child(toolbar)
        self.toolbar, self.side, self.separator = toolbar, side, handle
        self._set_side_width(self.cfg.get("export_width", SIDE_W))

        # Click outside the name while editing it: validate and leave editing.
        blur = Gtk.GestureClick(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        blur.connect("pressed", self._on_any_click)
        self.add_controller(blur)

        # Space & co. intercepted before the buttons (capture phase).
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        self._apply_mode(advanced)
        if self.multi and self.view_group.get_active_name() == "tracks":
            self._on_view(self.view_group, None)
        if self.states:
            self._set_lane_height(self.height_scale.get_value(), save=False)
        self._on_mix()
        self._update_labels()
        self._update_cut_hint()
        self._select_preset("discord")
        self._refresh_share()
        self.committed = self._snapshot()   # starting point of the history
        self.tick = GLib.timeout_add(33, self._on_tick)
        if self.fullscreen_only:
            GLib.idle_add(lambda: (self.toggle_fullscreen(), self.toggle_play(), False)[2])
        ENCODER_DETECTOR.detect(lambda: self.player and self._update_summary())

    # ------------------------------------------------------------ handles (Export panel, preview)
    def _make_handle(self, sep_orientation, cursor, tip, on_begin, on_update, on_end, on_reset):
        """Thin handle (a separator) that lights up on hover; drag = resize, double click = default."""
        vertical = sep_orientation == Gtk.Orientation.VERTICAL
        # Explicit hexpand/vexpand: the handle must never take free space (otherwise it "slips"
        # relative to the mouse).
        handle = Gtk.Box(css_classes=["side-handle" if vertical else "v-handle"], hexpand=False, vexpand=False,
                         cursor=Gdk.Cursor.new_from_name(cursor), tooltip_text=tip)
        handle.append(Gtk.Separator(orientation=sep_orientation, hexpand=True, vexpand=True,
                                    halign=Gtk.Align.CENTER if vertical else Gtk.Align.FILL,
                                    valign=Gtk.Align.FILL if vertical else Gtk.Align.CENTER))
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda g, *_: (handle.add_css_class("dragging"), on_begin(g)))
        drag.connect("drag-update", lambda g, *_: on_update(g))
        drag.connect("drag-end", lambda *_: (handle.remove_css_class("dragging"), on_end()))
        handle.add_controller(drag)
        reset = Gtk.GestureClick()
        reset.connect("pressed", lambda _g, n, *_: n == 2 and on_reset())
        handle.add_controller(reset)
        return handle

    @staticmethod
    def _event_pos(gesture):
        # Window coordinates (not the handle's, which moves during the drag).
        _ok, x, y = gesture.get_current_event().get_position()
        return x, y

    def _top_h(self):
        """Top of the timeline: frame strip (+ one track per screen in the stacked view)."""
        return self.canvas_h + self.timeline.rows_height()

    def _set_edit_h(self, h):
        """Height of the audio area (top band + tracks), the same in simple and advanced mode."""
        top = self._top_h()
        self.edit_h = int(max(h, top + Timeline.LANE_GAP + TRACKS_MIN))
        lanes_h = max(self.edit_h - top - Timeline.LANE_GAP, 0)
        for w in (self.timeline.lanes_scroll, self.headers_scroll):
            w.set_max_content_height(-1)
            w.set_min_content_height(lanes_h)
            w.set_max_content_height(lanes_h)
        simple_h = max(self.edit_h - top - self.bottom.get_spacing(), 0)
        self.simple_box.set_max_content_height(-1)
        self.simple_box.set_min_content_height(simple_h)
        self.simple_box.set_max_content_height(simple_h)

    def _update_vbar(self):
        adj = self.timeline.lanes_scroll.get_vadjustment()
        need = adj.get_upper() - adj.get_page_size() > 1
        self.vbar_col.set_visible(self.timeline.lanes_scroll.get_visible())
        self.vbar.set_opacity(1 if need else 0)
        self.vbar.set_can_target(need)

    def _on_vhandle_begin(self, gesture):
        # Bounds: the video doesn't go below VIDEO_MIN, the tracks keep at least TRACKS_MIN.
        room = max(self.video_box.get_height() - VIDEO_MIN, 0)
        self._vdrag_from = (self._event_pos(gesture)[1], self.edit_h, self.edit_h + room)

    def _on_vhandle_update(self, gesture):
        y0, h0, hi = self._vdrag_from
        lo = self._top_h() + Timeline.LANE_GAP + TRACKS_MIN
        self._set_edit_h(max(lo, min(hi, h0 - (self._event_pos(gesture)[1] - y0))))

    def _store_edit_h(self, reset=False):
        cfg = load_config()
        cfg["tracks_height"] = None if reset else self.edit_h
        save_config(cfg)

    # width of the Export panel
    def _set_side_width(self, w):
        # Never below the content's minimum width: otherwise the panel no longer shrinks and the handle
        # "comes loose" from the mouse.
        content_min = self.side.get_child().measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        w = int(max(SIDE_MIN, content_min, min(SIDE_MAX, w)))
        self.side_w = w
        self.side.set_maximum_size(w)
        self.side.set_tightening_threshold(w)
        self.side.set_size_request(w, -1)

    def _on_handle_begin(self, gesture):
        # The panel only widens by the space the page can give up: the window never grows.
        spare = self.main.get_width() - self.main.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        self._drag_from = (self._event_pos(gesture)[0], self.side_w, self.side_w + max(spare, 0))

    def _on_handle_update(self, gesture):
        x0, w0, hi = self._drag_from
        self._set_side_width(min(hi, w0 - (self._event_pos(gesture)[0] - x0)))

    def _store_side_width(self):
        cfg = load_config()
        cfg["export_width"] = self.side_w
        save_config(cfg)

    def _build_side(self):
        # No Adw.PreferencesPage: its inner column has a max width and stays centered, so the content
        # didn't follow the handle. Here the content takes the panel's full width.
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_start=20, margin_end=20,
                       margin_top=20, margin_bottom=24)
        m = self.meta

        g = Adw.PreferencesGroup(title="Export")
        self.presets = Adw.ToggleGroup(css_classes=["round"], homogeneous=True)
        self.preset_toggles = {}
        for key, label, tip in PRESETS:
            t = Adw.Toggle(name=key, label=label, tooltip=tip)
            self.preset_toggles[key] = t
            self.presets.add(t)
        self.presets.connect("notify::active-name", self._on_preset)
        g.add(self.presets)
        # 2 lines reserved: the summary never changes the panel's height
        self.summary = Gtk.Label(xalign=0, yalign=0, wrap=True, lines=3, margin_top=10, margin_bottom=4,
                                 max_width_chars=40, width_chars=40, css_classes=["summary"], height_request=58)
        g.add(self.summary)

        adv = Adw.ExpanderRow(title="Advanced settings", subtitle="Resolution, fps, codec, size")
        self.heights = [h for h in HEIGHTS if h == 0 or h < m["height"]]
        self.res_row = Adw.ComboRow(title="Resolution", model=Gtk.StringList.new(
            [f"Original ({m['height']}p)" if h == 0 else f"{h}p" for h in self.heights]))
        self.fps_choices = [f for f in FPS_CHOICES if f == 0 or f < m["fps"]]
        self.fps_row = Adw.ComboRow(title="Frames per second", model=Gtk.StringList.new(
            [f"Original ({m['fps']})" if f == 0 else str(f) for f in self.fps_choices]))
        self.codec_row = Adw.ComboRow(title="Codec", model=Gtk.StringList.new([lbl for _, lbl in CODECS]))
        self.size_row = Adw.SpinRow.new_with_range(0, 2000, 1)
        self.size_row.set_title("Maximum size (MB)")
        self.size_row.set_subtitle("0 = constant quality")
        self.quality_row = Adw.SpinRow.new_with_range(14, 40, 1)
        self.quality_row.set_title("Quality")
        self.quality_row.set_subtitle("Lower = better looking and heavier")
        for r in (self.res_row, self.fps_row, self.codec_row, self.size_row, self.quality_row):
            adv.add_row(r)
        for r in (self.res_row, self.fps_row, self.codec_row):
            r.connect("notify::selected", self._on_custom)
        for r in (self.size_row, self.quality_row):
            r.connect("notify::value", self._on_custom)
        adv_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"], margin_top=6)
        self.screen_row = None
        if self.multi:
            self.screen_choices = [None] + list(range(len(self.screens)))   # None = edit (switches)
            self.screen_row = Adw.ComboRow(
                title="Exported screen", model=Gtk.StringList.new(
                    ["Edit (switches)"] + [f"Screen {k + 1} only — {sc['monitor']}" for k, sc in enumerate(self.screens)]))
            self.screen_row.connect("notify::selected", lambda *_: (self._update_sensitivity(), self._update_summary()))
            adv_list.append(self.screen_row)
        self.norm_row = Adw.SwitchRow(title="Normalize the volume", active=self.cfg["normalize_audio"],
                                      subtitle="Standard platform loudness, no clipping")
        self.norm_row.connect("notify::active", self._on_normalize)
        adv_list.append(self.norm_row)
        adv_list.append(adv)
        g.add(adv_list)

        # Export button: the progress is shown INSIDE the button (no shift of the panel)
        self.export_btn = no_focus(Gtk.Button(css_classes=["suggested-action", "pill"], height_request=44))
        self.export_btn.set_child(Adw.ButtonContent(icon_name="document-save-symbolic", label="Export"))
        self.export_btn.connect("clicked", self._export)
        self.progress = Gtk.ProgressBar(css_classes=["osd", "export-progress"], valign=Gtk.Align.END,
                                        margin_start=22, margin_end=22, margin_bottom=5, visible=False,
                                        can_target=False)
        btn_overlay = Gtk.Overlay(child=self.export_btn, margin_top=14)
        btn_overlay.add_overlay(self.progress)
        g.add(btn_overlay)
        # Always one line (never empty): the panel doesn't move when the state changes.
        self.export_state = Gtk.Label(xalign=0.5, css_classes=["caption", "dim-label"], margin_top=8,
                                      label="Not exported yet")
        g.add(self.export_state)
        page.append(g)

        g3 = Adw.PreferencesGroup(title="Share",
                                  description="Drag a card into Discord, a browser or your file manager — or copy it.")
        tiles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        # Video (with audio) then audio only; each time: the edited version (last export) and the original.
        self.tile_comp = ShareTile("Edited", self.win.toast)
        self.tile_orig = ShareTile("Original", self.win.toast)
        self.tile_acomp = ShareTile("Edited", self.win.toast, audio=True)
        self.tile_aorig = ShareTile("Original", self.win.toast, audio=True)
        for title, group in (("Video with audio", (self.tile_comp, self.tile_orig)),
                             ("Audio only", (self.tile_acomp, self.tile_aorig))):
            tiles.append(Gtk.Label(label=title, xalign=0, css_classes=["caption-heading", "dim-label"],
                                   margin_top=4 if title == "Video with audio" else 12))
            for tile in group:
                tiles.append(tile)
        g3.add(tiles)
        page.append(g3)

        g4 = Adw.PreferencesGroup(title="Info")
        game = getattr(self.item, "game", None)
        rows = [("Video", f"{m['width']}×{m['height']} · {m['fps']} fps · {m['vcodec'].upper()}"),
                ("Duration", fmt_clock(m["duration"])),
                ("Size", fmt_size(self.item.size)),
                ("Recorded on", time.strftime("%Y-%m-%d at %H:%M:%S", time.localtime(self.item.mtime)))]
        for title, value in rows:
            g4.add(Adw.ActionRow(title=title, subtitle=value, css_classes=["property"]))
        self.game_row = Adw.ActionRow(title="Game", css_classes=["property"])
        self.game_icon = Gtk.Picture(width_request=32, height_request=32, can_shrink=True,
                                     valign=Gtk.Align.CENTER, css_classes=["game-icon"])
        self.game_row.add_prefix(self.game_icon)
        change = no_focus(Gtk.Button(label="Change", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                     tooltip_text="Pick this clip's game"))
        change.connect("clicked", lambda *_: self.win.pick_game([self.item], self._game_changed))
        self.game_row.add_suffix(change)
        g4.add(self.game_row)
        self._show_game()
        page.append(g4)
        return Gtk.ScrolledWindow(child=page, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)

    # ------------------------------------------------------------ heavy data (background, cache)
    def _load_strip(self):
        """The clip's frame strip (and each other screen's for the "tracks per screen" view)."""
        dur0 = max(self.meta["duration"], 0.1)

        def primary(tex, frames):
            self.timeline.canvas.set_strip(tex, frames)
            if self.rows:
                self.rows[0].set_strip(tex, frames)

        self._strip_for(self.item.path, dur0, 16, primary)
        for k, sc in enumerate(self.screens[1:], 1):
            lo, hi = sc["range"]
            frames = max(2, round(16 * (hi - lo) / dur0))
            self._strip_for(sc["path"], max(hi - lo, 0.1), frames,
                            lambda tex, n, k=k: self.rows[k].set_strip(tex, n))

    def _strip_for(self, path, dur, frames, apply):
        try:
            out = os.path.join(THUMB_DIR, cache_key(path, f"strip{frames}") + ".jpg")
        except OSError:
            return
        os.makedirs(THUMB_DIR, exist_ok=True)
        if os.path.exists(out):
            apply(Gdk.Texture.new_from_filename(out), frames)
            return
        # Only decodes keyframes: very fast even on a long clip.
        vf = (f"tpad=stop_mode=clone:stop_duration=4,fps={frames / dur:.5f},"
              f"scale=160:90:force_original_aspect_ratio=increase,crop=160:90,tile={frames}x1")

        def done(ok, _out, _err):
            if ok and os.path.exists(out) and self.player:
                apply(Gdk.Texture.new_from_filename(out), frames)

        JOBS.add(["ffmpeg", "-v", "error", "-nostdin", "-y", "-skip_frame", "nokey", "-i", path,
                  "-an", "-vf", vf, "-frames:v", "1", "-q:v", "5", out], done, front=True)

    PEAKS = 1500          # values per track (max useful width of a track)
    PEAKS_FLOOR = -54.0   # dB: floor of the scale (a voice or a quiet game stay readable)

    def _load_peaks(self):
        """Waveforms of all the tracks: a single read of the file, peaks computed by ffmpeg (in C) —
        the UI only receives 1500 values per track. Cached: instant reopening."""
        lanes = list(self.lanes)
        if not lanes:
            return
        try:
            cached = os.path.join(THUMB_DIR, cache_key(self.item.path, f"peaks{self.PEAKS}") + ".json")
        except OSError:
            return
        if os.path.exists(cached):
            try:
                with open(cached) as f:
                    for lane, vals in zip(lanes, json.load(f)):
                        lane.set_peaks(vals)
                return
            except (OSError, ValueError):
                pass
        tmp = tempfile.mkdtemp(prefix="clips-peaks-")
        per = max(1, int(self.meta["duration"] * 8000 / self.PEAKS) + 1)   # samples (at 8 kHz) per value
        graph = ";".join(
            f"[0:a:{i}]aformat=channel_layouts=mono,aresample=8000,asetnsamples=n={per}:p=0,"
            f"astats=metadata=1:reset=1:measure_overall=none:measure_perchannel=Peak_level,"
            f"ametadata=mode=print:key=lavfi.astats.1.Peak_level:file={tmp}/{i}.txt[o{i}]" for i in range(len(lanes)))
        argv = ["nice", "-n", "5", "ffmpeg", "-v", "error", "-nostdin", "-i", self.item.path, "-filter_complex", graph]
        for i in range(len(lanes)):
            argv += ["-map", f"[o{i}]"]
        argv += ["-f", "null", "-"]

        def done(ok, _out, _err):
            tracks = []
            for i in range(len(lanes)):
                vals = []
                try:
                    with open(os.path.join(tmp, f"{i}.txt")) as f:
                        for line in f:
                            if line.startswith("lavfi.astats"):
                                db = float(line.partition("=")[2])   # "-inf" for silence
                                vals.append(max(0.0, (db - self.PEAKS_FLOOR) / -self.PEAKS_FLOOR))
                except (OSError, ValueError):
                    vals = []
                tracks.append(vals)
            shutil.rmtree(tmp, ignore_errors=True)
            if not ok or not any(tracks):
                return
            try:
                with open(cached, "w") as f:
                    json.dump([[round(v, 3) for v in t] for t in tracks], f)
            except OSError:
                pass
            if self.player:   # editor still open
                for lane, vals in zip(lanes, tracks):
                    lane.set_peaks(vals)

        JOBS.add(argv, done, front=True)

    # ------------------------------------------------------------ several screens
    def _screen_at(self, t):
        """Screen shown at time t: the switch's one, if it has a frame at that moment (otherwise screen 1)."""
        k = angle_at(self.angles, t)
        if k:
            lo, hi = self.screens[k]["range"]
            if not lo <= t < hi:
                return 0
        return k

    def _follow(self, pos, force=False):
        """Shows the right screen and keeps the other players in sync with the clip (audio comes from the clip)."""
        k = self._screen_at(pos) if self.preview_screen is None else self.preview_screen
        missing = False
        if k and self.preview_screen is not None:   # chosen screen without a frame at this time: black screen + note
            lo, hi = self.screens[k]["range"]
            missing = not lo <= pos < hi
        shown = -1 if missing else k
        if shown != self.shown:
            self.shown = shown
            self.picture.set_paintable(Gdk.Paintable.new_empty(16, 9) if missing
                                       else (self.vplayers[k] if k else self.player).paintable)
            self.preview_note.set_label(f"Screen {k + 1}: no frame at this time")
            self.preview_note.set_visible(missing)
        if self.screen_group and self.screen_group.get_active_name() != str(angle_at(self.angles, pos)):
            self.updating_screen = True
            self.screen_group.set_active_name(str(angle_at(self.angles, pos)))
            self.updating_screen = False
        playing = self.player.playing
        self.follow_tick += 1
        for j, vp in self.vplayers.items():
            lo, hi = self.screens[j]["range"]
            target = pos - self.screens[j]["offset"]
            # Only played if it's shown or will be within the second: just one extra video decode.
            soon = any(lo <= t < hi and pos <= t <= pos + 1.0 and s == j for t, s in self.angles)
            wanted = lo <= pos < hi and (j == k or (soon and self.preview_screen is None))
            if playing and wanted:
                if not vp.playing:
                    vp.seek(max(target, 0))
                    vp.play()
                elif force or self.follow_tick % 6 == 0:   # drift (cuts, stutters): realign
                    if abs(vp.position() - target) > 0.12:
                        vp.seek(max(target, 0))
            else:
                if vp.playing:
                    vp.pause()
                if j == k and (force or abs(getattr(vp, "shown_at", -1) - target) > 0.01):
                    vp.shown_at = target   # paused: the exact frame of the displayed time
                    vp.seek(max(target, 0))

    def _on_preview_pick(self, group, _):
        name = group.get_active_name()
        if name is None:
            return
        self.preview_screen = None if name == "m" else int(name)
        self.shown = None   # forces the picture change
        self._follow(self.player.position() if self.player.playing else self.timeline.pos, force=True)

    def _on_view(self, group, _):
        mode = group.get_active_name()
        if mode is None:
            return
        self.timeline.show_rows(mode == "tracks")
        top = self._top_h()
        for spacer in (self.headers_spacer, self.vbar_spacer):
            spacer.set_size_request(-1, top)
        self._set_edit_h(self.edit_h)   # the audio tracks area makes room (the page doesn't move)
        cfg = load_config()
        cfg["multiscreen_view"] = mode
        save_config(cfg)

    def _row_view(self, k, t):
        """Click on screen k's track: preview of that screen, at the clicked time (the edit doesn't change)."""
        self._on_seek(t, True)
        self.preview_group.set_active_name(str(k))

    def _row_switch(self, k, t):
        """Right click on screen k's track: switch the edit to that screen at the clicked time."""
        self._on_seek(t, True)
        self._set_angle_at(t, k)

    def _on_screen_toggle(self, group, _):
        if getattr(self, "updating_screen", False) or group.get_active_name() is None:
            return
        self._set_angle_here(int(group.get_active_name()))

    def _switch_here(self):
        """B: switch the edit at the playhead — to the screen watched in the preview if it's forced
        (Ctrl+1, Ctrl+2…), otherwise to the next screen."""
        pos = self.timeline.pos
        current = angle_at(self.angles, pos)
        if self.preview_screen is not None and self.preview_screen != current:
            target = self.preview_screen
        else:
            target = (current + 1) % len(self.screens)
        self._set_angle_at(pos, target)
        self.win.toast(f"Edit: screen {target + 1} from {fmt_clock(pos)}")

    def _set_angle_here(self, k):
        """Screen k from the playhead on (until the next switch)."""
        self._set_angle_at(self.timeline.pos, k)

    def _set_angle_at(self, pos, k):
        angles = [a for a in self.angles if a[0] == 0.0 or abs(a[0] - pos) > 0.05]
        if pos < 0.05:
            angles[0] = [0.0, k]
        else:
            angles.append([pos, k])
        self._set_angles(angles)

    def _set_angles(self, angles):
        angles = sorted([list(a) for a in angles])
        if not angles or angles[0][0] > 0.0:
            angles.insert(0, [0.0, angles[0][1] if angles else 0])
        clean = []
        for t, k in angles:   # a switch to the screen already shown is pointless
            if not clean or clean[-1][1] != k:
                clean.append([t, k])
        self.angles = clean
        self.timeline.set_angles(clean)
        set_clip_angles(self.item.path, clean)
        self._changed(immediate=True)
        self._follow(self.timeline.pos, force=True)
        self._update_sensitivity()
        self._update_summary()

    # ------------------------------------------------------------ playback
    def toggle_play(self):
        if not self.player:
            return
        if self.player.playing:
            self.player.pause()
        else:
            pos = self.player.position()
            if not (self.t_in <= pos < self.t_out - 0.05):
                pos = self.t_in
            self.player.play_segments(self.kept_segments(), pos, self.loop)
            self.player.play()
        self._update_play_icon()

    def _on_video_click(self, gesture, n_press, _x, _y):
        if n_press == 2:   # double click: fullscreen (the 1st click played/paused, undo it)
            self.toggle_play()
            self.toggle_fullscreen()
        else:
            self.toggle_play()

    # ------------------------------------------------------------ fullscreen
    # ------------------------------------------------------------ monitor volume (preview only)
    def _build_listen_volume(self):
        """Speaker at the bottom left of the video: on hover, a volume bar for monitoring.
        Click: mute / unmute. Has no effect on the exported audio."""
        self.listen = float(self.cfg.get("preview_volume", 1.0))
        self.listen_before_mute = self.listen or 1.0
        box = Gtk.Box(spacing=2, css_classes=["osd", "listen-box"], halign=Gtk.Align.START, valign=Gtk.Align.END,
                      margin_start=12, margin_bottom=12)
        self.listen_btn = no_focus(Gtk.Button(css_classes=["flat", "circular"], tooltip_text=
                                              "Monitor volume (doesn't affect the export) · click: mute"))
        self.listen_btn.connect("clicked", lambda *_: self._set_listen(0.0 if self.listen > 0 else self.listen_before_mute))
        self.listen_scale = no_focus(Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1))
        self.listen_scale.set_draw_value(False)
        self.listen_scale.set_size_request(110, -1)
        self.listen_scale.set_value(self.listen * 100)
        self.listen_scale.connect("value-changed", lambda sc: self._set_listen(sc.get_value() / 100, from_scale=True))
        reveal = Gtk.Revealer(child=self.listen_scale, transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT,
                              transition_duration=150)
        box.append(self.listen_btn)
        box.append(reveal)
        hover = Gtk.EventControllerMotion()
        hover.connect("enter", lambda *_: reveal.set_reveal_child(True))
        hover.connect("leave", lambda *_: reveal.set_reveal_child(False))
        box.add_controller(hover)
        self.listen_box = box
        self._listen_icon()
        return box

    def _listen_icon(self):
        v = self.listen
        self.listen_btn.set_icon_name("audio-volume-muted-symbolic" if v <= 0 else "audio-volume-low-symbolic"
                                      if v < 0.34 else "audio-volume-medium-symbolic" if v < 0.67
                                      else "audio-volume-high-symbolic")

    def _set_listen(self, v, from_scale=False):
        self.listen = max(0.0, min(1.0, v))
        if self.listen > 0:
            self.listen_before_mute = self.listen
        if not from_scale:
            self.listen_scale.set_value(self.listen * 100)
        if self.player:
            self.player.set_monitor(self.listen)
        self._listen_icon()
        if getattr(self, "_listen_save", 0):
            GLib.source_remove(self._listen_save)

        def store():   # saved once the slider has settled
            self._listen_save = 0
            cfg = load_config()
            cfg["preview_volume"] = round(self.listen, 2)
            save_config(cfg)
            return False

        self._listen_save = GLib.timeout_add(400, store)

    def _build_fullscreen_osd(self, overlay, dur):
        """Fullscreen floating bar: play, time, progress (draggable), exit.
        Hides itself (along with the cursor) when the mouse stops moving."""
        self.osd_dragging = False
        self.osd_timer = 0
        self.osd_seek_timer = 0
        bar = Gtk.Box(spacing=10, css_classes=["osd", "fs-bar"], valign=Gtk.Align.END, margin_start=32,
                      margin_end=32, margin_bottom=28, visible=False)
        self.osd_play = no_focus(Gtk.Button(css_classes=["flat", "circular", "play-btn"],
                                            tooltip_text="Play / pause (Space)"))
        self.osd_glyph = PlayGlyph(15)
        self.osd_play.set_child(self.osd_glyph)
        self.osd_play.connect("clicked", lambda *_: self.toggle_play())
        self.osd_time = Gtk.Label(css_classes=["numeric"], width_chars=17, xalign=0)
        self.osd_scale = no_focus(Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, max(dur, 0.01), 0.01))
        self.osd_scale.set_hexpand(True)
        self.osd_scale.set_draw_value(False)
        self.osd_scale.connect("change-value", self._on_osd_scrub)
        leave = no_focus(Gtk.Button(icon_name="view-restore-symbolic", css_classes=["flat", "circular"],
                                    tooltip_text="Leave fullscreen (F or Esc)"))
        leave.connect("clicked", lambda *_: self.toggle_fullscreen())
        for w in (self.osd_play, self.osd_time, self.osd_scale, leave):
            bar.append(w)
        overlay.add_overlay(bar)
        self.osd = bar
        # title at the top left (game icon + clip name)
        top = Gtk.Box(spacing=10, css_classes=["osd", "fs-title"], halign=Gtk.Align.START, valign=Gtk.Align.START,
                      margin_start=32, margin_top=28, visible=False)
        icon = texture(game_art(getattr(self.item, "appid", None)).get("icon"))
        if icon:
            top.append(Gtk.Picture(paintable=icon, width_request=24, height_request=24, can_shrink=True,
                                   css_classes=["game-icon"], valign=Gtk.Align.CENTER))
        self.osd_title = Gtk.Label(label=self.item.stem, css_classes=["heading"])
        top.append(self.osd_title)
        overlay.add_overlay(top)
        self.osd_top = top
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_fs_motion)
        overlay.add_controller(motion)
        self.last_motion = None

    def _on_fs_motion(self, _c, x, y):
        # Hiding the cursor or the bar triggers "motion" events without any real movement:
        # without this filter, the bar showed up again right away and never disappeared.
        if self.last_motion and abs(x - self.last_motion[0]) < 3 and abs(y - self.last_motion[1]) < 3:
            return
        self.last_motion = (x, y)
        self._osd_show()

    def _on_osd_scrub(self, _scale, _scroll, value):
        value = min(max(value, 0), self.meta["duration"])
        self.osd_dragging = True
        self._on_seek(value, False)
        if self.osd_seek_timer:
            GLib.source_remove(self.osd_seek_timer)

        def final():
            self.osd_seek_timer = 0
            self.osd_dragging = False
            self._on_seek(value, True)
            return False

        self.osd_seek_timer = GLib.timeout_add(180, final)   # accurate seek on release
        self._osd_show()
        return False

    def _osd_show(self):
        if not self.fullscreen:
            return
        self.osd.set_opacity(1)
        self.osd_top.set_opacity(1)
        self.picture.set_cursor(None)
        if self.osd_timer:
            GLib.source_remove(self.osd_timer)
        self.osd_timer = GLib.timeout_add(2500, self._osd_hide)

    def _osd_hide(self):
        self.osd_timer = 0
        if self.fullscreen and not self.osd_dragging:   # while playing as well as paused
            self.osd.set_opacity(0)
            self.osd_top.set_opacity(0)
            self.picture.set_cursor(Gdk.Cursor.new_from_name("none"))
        return False

    def toggle_fullscreen(self):
        """Fullscreen: edge-to-edge video + floating bar (F or Esc to leave)."""
        if self.fullscreen and self.fullscreen_only:   # launched from the library: go back to it
            self.fullscreen = False
            self.win.unfullscreen()
            GLib.idle_add(lambda: (self.win.nav.pop(), False)[1])
            return
        self.fullscreen = not self.fullscreen
        fs = self.fullscreen
        if fs:
            self.win.fullscreen()
        else:
            self.win.unfullscreen()
        self.toolbar.set_reveal_top_bars(not fs)
        for w in self.fs_hidden + [self.side, self.separator]:
            w.set_visible(not fs)
        for side in ("start", "end", "top", "bottom"):
            getattr(self.main, f"set_margin_{side}")(0 if fs else {"start": 18, "end": 18, "top": 6, "bottom": 18}[side])
        (self.picture.add_css_class if fs else self.picture.remove_css_class)("fullscreen")
        self.fs_btn.set_visible(not fs)
        self.listen_box.set_visible(not fs)
        self.osd.set_visible(fs)
        self.osd_top.set_visible(fs)
        self.osd_scale.clear_marks()
        if fs:
            self.osd_scale.set_value(self.timeline.pos)
            for t in (self.t_in, self.t_out):
                if 0.05 < t < self.meta["duration"] - 0.05:
                    self.osd_scale.add_mark(t, Gtk.PositionType.TOP, None)
            self._update_labels()
            self._osd_show()
        else:
            self.picture.set_cursor(None)
            self._apply_mode(bool(self.adv_switch and self.adv_switch.get_active()))

    def _show_game(self):
        game = getattr(self.item, "game", None)
        self.game_row.set_subtitle(game or "None")
        icon = texture(game_art(getattr(self.item, "appid", None)).get("icon")) if game else None
        self.game_icon.set_paintable(icon)
        self.game_icon.set_visible(icon is not None)

    def _game_changed(self):
        self._show_game()
        # game icon in the header
        first = self.header_title.get_first_child()
        if isinstance(first, Gtk.Image):
            self.header_title.remove(first)
        icon = texture(game_art(getattr(self.item, "appid", None)).get("icon")) if self.item.game else None
        if icon:
            self.header_title.prepend(Gtk.Image(paintable=icon, pixel_size=18, valign=Gtk.Align.CENTER,
                                                css_classes=["game-icon"]))

    def _sync_favorite(self):
        fav = getattr(self.item, "favorite", False)
        self.fav_btn.set_active(fav)
        self.fav_btn.set_icon_name("clips-star-symbolic" if fav else "clips-star-outline-symbolic")
        self.fav_btn.set_tooltip_text("Remove from favorites" if fav else "Add to favorites")
        (self.fav_btn.add_css_class if fav else self.fav_btn.remove_css_class)("on")

    def _on_favorite(self, btn):
        if btn.get_active() != getattr(self.item, "favorite", False):
            if hasattr(self.win, "_set_favorite"):
                self.win._set_favorite(self.item, btn.get_active())
            self._sync_favorite()

    def _on_any_click(self, gesture, _n, x, y):
        if not self.title_label.get_editing():
            return
        target = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        if target is None or not (target == self.title_label or target.is_ancestor(self.title_label)):
            self.title_label.stop_editing(True)   # validates the new name

    def _on_loop(self, btn):
        self.loop = btn.get_active()
        if self.player:
            self.player.loop = self.loop
            self.player.at_end = False

    def _resync_playback(self, t=None):
        """After a change (cut, start/end) during playback: restart from the updated segments."""
        if self.player and self.player.playing:
            self.player.play_segments(self.kept_segments(), self.player.position() if t is None else t, self.loop)

    def _update_play_icon(self):
        if not self.player:
            return
        playing = self.player.playing
        self.play_glyph.set_playing(playing)
        self.osd_glyph.set_playing(playing)
        self.big_play.set_opacity(0 if playing else 1)   # opacity: the layout doesn't move
        self.big_play.set_can_target(not playing)

    def _on_tick(self):
        if not self.player:
            return False
        if self.multi:
            self._follow(self.player.position() if self.player.playing else self.timeline.pos)
        if self.player.playing and self.timeline.canvas.mode is None:   # not during a drag
            pos = self.player.position()
            end = self.player.end_time()
            if self.player.at_end and end is not None and pos >= end - 0.04:
                self.player.pause()   # end of the excerpt, no loop
                self._update_play_icon()
            self.timeline.set_pos(pos)
            self._update_labels(pos)
            if self.fullscreen and not self.osd_dragging:
                self.osd_scale.set_value(pos)
        return True

    def _on_eos(self):
        if self.player:
            self.player.pause()
            self._update_play_icon()

    def _seek_to(self, t):
        self._on_seek(t, True)

    def _on_seek(self, t, final):
        if self.player:
            if not final and self.player.playing:
                # Pause while dragging: otherwise playback and the mouse fight over
                # the position (the playhead "bounces").
                self.scrub_resume = True
                self.player.pause()
            if final and (self.player.playing or self.scrub_resume):
                self.scrub_resume = False
                self.player.play_segments(self.kept_segments(), t, self.loop)
                self.player.play()
                self._update_play_icon()
            else:
                self.player.seek(t, accurate=final)
        self.timeline.set_pos(t)
        self._update_labels(t)

    def _on_range(self, t_in, t_out, preview_t, final):
        self.t_in, self.t_out = t_in, t_out
        if self.player:
            self.player.seek(preview_t, accurate=final)
        self.timeline.set_pos(preview_t)
        self._refresh_lanes()
        self._update_labels(preview_t)
        if final:
            self._resync_playback(preview_t)
            self._after_edit()
            self._changed(immediate=True)

    def mark(self, which):
        pos = self.player.position() if self.player else 0
        if which == "in" and pos < self.t_out - 0.5:
            self._on_range(pos, self.t_out, pos, True)
        elif which == "out" and pos > self.t_in + 0.5:
            self._on_range(self.t_in, pos, pos, True)
        elif which == "reset":
            self._on_range(0.0, self.meta["duration"], pos, True)
        self.timeline.canvas.set_range(self.t_in, self.t_out)

    def _update_labels(self, pos=None):
        if pos is None:
            pos = self.player.position() if self.player else 0
        fine = lambda t: f"{int(t // 60)}:{t % 60:05.2f}"   # hundredths: every frame shows
        label = f"{fine(pos)} / {fine(self.meta['duration'])}"
        if label != self.last_time:   # avoids useless relayouts during playback
            self.last_time = label
            self.time_label.set_label(label)
            self.osd_time.set_label(label)

    def _update_stats(self):
        """Final duration, only shown if the clip is shortened (excerpt or removed sections)."""
        dur = self.meta["duration"]
        kept = self.kept_duration()
        edited = dur - kept > 0.05
        if edited:
            self.final_label.set_label(f"<span alpha='60%'>Final duration</span> <b>{fmt_clock(kept)}</b>"
                                       f" <span alpha='60%'>· −{dur - kept:.1f} s</span>")
        self.final_label.set_visible(edited)

    # ------------------------------------------------------------ removed sections
    def kept_segments(self):
        segs, cur = [], self.t_in
        for a, b in self.cuts:
            if b <= self.t_in or a >= self.t_out:
                continue
            if a > cur:
                segs.append((cur, min(a, self.t_out)))
            cur = max(cur, b)
        if cur < self.t_out:
            segs.append((cur, self.t_out))
        return [(a, b) for a, b in segs if b - a > 0.05]

    def kept_duration(self):
        return sum(b - a for a, b in self.kept_segments())

    def _add_cut_here(self, length=2.0):
        """Removes a section starting at the playhead (shortened if another section or the end of the
        excerpt is too close). Adjust it afterwards with the mouse: move, pull the edges, right click."""
        pos = self.timeline.pos
        if self.timeline.canvas.cut_at(pos) is not None:
            self.win.toast("This moment is already in a removed section")
            return
        g0 = max([self.t_in] + [b for a, b in self.cuts if b <= pos])
        g1 = min([self.t_out] + [a for a, b in self.cuts if a >= pos])
        a = max(pos, g0)
        b = min(a + length, g1)
        if b - a < length:   # too close to the end: move the start back into the free space
            a = max(g0, b - length)
        if b - a < 0.2:
            self.win.toast("Not enough room here to remove a section")
            return
        self._set_cuts_user(self.cuts + [[a, b]])

    def _set_cuts_user(self, cuts):
        saved = self.cuts
        self.cuts = merge_ranges(cuts)
        empty = not self.kept_segments()
        self.cuts = saved
        if empty:
            self.win.toast("Can't cut everything")
            self.timeline.canvas.set_cuts(self.cuts)
            return
        self._set_cuts(cuts)
        self._changed(immediate=True)

    def _set_cuts(self, cuts):
        self.cuts = merge_ranges(cuts)
        self.timeline.canvas.set_cuts(self.cuts)
        self._refresh_lanes()
        self._update_cut_hint()
        self._resync_playback()
        self._after_edit()

    def _restore_cut(self, idx):
        if idx is None:
            return
        removed = self.cuts[idx]
        self._set_cuts([c for i, c in enumerate(self.cuts) if i != idx])
        self._changed(immediate=True)
        toast = Adw.Toast(title=f"Section {fmt_clock(removed[0])} → {fmt_clock(removed[1])} put back into the clip",
                          button_label="Undo", timeout=4)
        toast.connect("button-clicked", lambda *_: self.undo())
        self.win.toasts.add_toast(toast)

    # ------------------------------------------------------------ undo / redo
    def _snapshot(self):
        return {
            "range": (self.t_in, self.t_out),
            "cuts": [tuple(c) for c in self.cuts],
            "angles": [tuple(a) for a in self.angles],
            "tracks": [(round(s.level, 3), s.mute, s.solo) for s in self.states],
            "points": [[tuple(p) for p in lane.points] for lane in self.lanes],
        }

    def _changed(self, immediate=False):
        """A change to the edit. Continuous gestures (slider, drag) make a single step: wait for
        half a second of calm before recording it."""
        if self.restoring or self.committed is None:
            return
        if self.commit_timer:
            GLib.source_remove(self.commit_timer)
            self.commit_timer = 0
        if immediate:
            self._commit()
        else:
            self.commit_timer = GLib.timeout_add(500, lambda: (self._commit(), False)[1])

    def _commit(self):
        if self.commit_timer:
            GLib.source_remove(self.commit_timer)
            self.commit_timer = 0
        if self.committed is None:
            return
        cur = self._snapshot()
        if cur != self.committed:
            self.undo_stack.append(self.committed)
            del self.undo_stack[:-200]
            self.redo_stack.clear()
            self.committed = cur

    def undo(self):
        self._commit()
        if self.undo_stack:
            self.redo_stack.append(self.committed)
            self._restore(self.undo_stack.pop())

    def redo(self):
        self._commit()
        if self.redo_stack:
            self.undo_stack.append(self.committed)
            self._restore(self.redo_stack.pop())

    def _restore(self, snap):
        self.restoring = True
        try:
            self.t_in, self.t_out = snap["range"]
            self.timeline.canvas.set_range(self.t_in, self.t_out)
            if self.multi:
                self._set_angles([list(a) for a in snap["angles"]])
            for st, (level, mute, solo) in zip(self.states, snap["tracks"]):
                st.level, st.mute, st.solo = level, mute, solo
            for lane, pts in zip(self.lanes, snap["points"]):
                lane.set_points(pts)
            self._set_cuts([list(c) for c in snap["cuts"]])   # replays playback and updates everything
            self._on_mix()
            self._update_labels()
        finally:
            self.restoring = False
        self.committed = snap

    # ------------------------------------------------------------ edit points (Ctrl+← / Ctrl+→)
    def _edit_points(self):
        pts = {0.0, self.meta["duration"], self.t_in, self.t_out}
        for a, b in self.cuts:
            pts.update((a, b))
        if self.multi:
            pts.update(t for t, _k in self.angles)
        if self.adv_switch and self.adv_switch.get_active():   # volume curve points
            for lane in self.lanes:
                pts.update(p[0] for p in lane.points)
        return sorted(pts)

    def _jump_edit(self, direction):
        pos = self.timeline.pos
        pts = self._edit_points()
        if direction > 0:
            t = next((p for p in pts if p > pos + 0.02), None)
        else:
            t = next((p for p in reversed(pts) if p < pos - 0.02), None)
        if t is not None:
            self._seek_to(min(t, max(self.meta["duration"] - 0.02, 0)))

    # sections with the keyboard: C = start, V = end
    def _set_cut_start(self, t):
        self.cut_start = t
        self.timeline.canvas.set_pending(t)
        self._update_cut_hint()

    def _cut_mark_start(self):
        if self.player:
            self._set_cut_start(self.player.position())

    def _cut_mark_end(self):
        if not self.player:
            return
        pos = self.player.position()
        if self.cut_start is None:
            self.win.toast("First press C at the start of the section to cut")
            return
        a, b = sorted((self.cut_start, pos))
        self._set_cut_start(None)
        if b - a < 0.1:
            self.win.toast("Section too short")
            return
        self._set_cuts_user(self.cuts + [[a, b]])

    # frame by frame: ← / → (Shift = 1 second)
    def _step_frame(self, direction):
        """Steps forward or back by exactly one frame (snapped to the frame grid), playback paused."""
        if self.player.playing:
            self.player.pause()
            self._update_play_icon()
        fps = max(self.meta.get("fps") or 60, 1)
        frame = round(self.timeline.pos * fps) + direction
        t = min(max(frame / fps, 0.0), max(self.meta["duration"] - 1 / fps, 0.0))
        self.player.seek(t + 0.25 / fps, accurate=True)   # slight offset: lands squarely in the target frame
        self.timeline.set_pos(t)
        self._update_labels(t)

    def _update_cut_hint(self):
        text = (f"Section started at {fmt_clock(self.cut_start)} · V to end it · Esc to cancel"
                if self.cut_start is not None else "")
        self.cut_hint.set_label(text)
        self._update_stats()

    # ------------------------------------------------------------ audio
    def _on_advanced(self, sw, _):
        self._apply_mode(sw.get_active())
        cfg = load_config()
        cfg["advanced_audio"] = sw.get_active()
        save_config(cfg)
        self._on_mix()

    def _on_height_changed(self, scale):
        self._set_lane_height(scale.get_value())

    def _set_lane_height(self, h, save=True):
        for lane in self.lanes:
            lane.set_height(h)
        hdr = self.headers.get_first_child()
        while hdr:
            hdr.set_size_request(HEADER_W, int(h))
            hdr = hdr.get_next_sibling()
        if save:   # saved once the slider is released (not at every notch)
            if getattr(self, "_height_save", 0):
                GLib.source_remove(self._height_save)

            def store():
                self._height_save = 0
                cfg = load_config()
                cfg["lane_height"] = int(h)
                save_config(cfg)
                return False

            self._height_save = GLib.timeout_add(400, store)

    def _on_lane_scroll(self, _c, _dx, dy):
        state = _c.get_current_event_state()
        if not state & Gdk.ModifierType.CONTROL_MASK:
            return False
        self.height_scale.set_value(self.height_scale.get_value() - dy * 8)
        return True

    def _apply_mode(self, advanced):
        has = bool(self.states)
        if not has:
            return
        self.height_box.set_visible(advanced)
        self.headers_col.set_visible(advanced)
        self.timeline.show_lanes(advanced)
        self.simple_box.set_visible(not advanced)
        self.help_label.set_label(HELP_ADVANCED if advanced else HELP_SIMPLE)
        self._set_edit_h(self.edit_h)
        if advanced:
            # When advanced mode opens, all the tracks fit in the area if possible
            # (height reduced if needed, without going below 36 px); otherwise the area scrolls.
            n = len(self.lanes)
            room = self.edit_h - self._top_h() - Timeline.LANE_GAP
            h = self.height_scale.get_value()
            if n * (h + Timeline.LANE_GAP) - Timeline.LANE_GAP > room:
                h = max(36, (room + Timeline.LANE_GAP) / n - Timeline.LANE_GAP)
                self.height_scale.handler_block_by_func(self._on_height_changed)
                self.height_scale.set_value(h)
                self.height_scale.handler_unblock_by_func(self._on_height_changed)
                self._set_lane_height(h, save=False)
        self._update_vbar()

    def _mix_state(self):
        """Per track: (gain, effective mute, curve points) taking solo and the mode into account."""
        solo = any(s.solo for s in self.states)
        adv = bool(self.adv_switch and self.adv_switch.get_active())
        return [(s.level / 100, s.mute or (solo and not s.solo),
                 [tuple(p) for p in lane.points] if adv else [])
                for s, lane in zip(self.states, self.lanes)]

    def _on_mix(self, live=False):
        if getattr(self, "export_state", None):
            self._update_export_state()
        for i, (gain, muted, points) in enumerate(self._mix_state()):
            self.lanes[i].set_state(gain, muted, self.t_in, self.t_out, self.cuts)
            if self.player:
                self.player.set_track(i, gain, muted, points)

    def _refresh_lanes(self):
        for lane, (gain, muted, _p) in zip(self.lanes, self._mix_state()):
            lane.set_state(gain, muted, self.t_in, self.t_out, self.cuts)

    # ------------------------------------------------------------ keyboard
    def _on_key(self, _c, keyval, _code, state):
        focus = self.win.get_focus()
        if self.title_label.get_editing() or isinstance(focus, (Gtk.Text, Gtk.SpinButton, Gtk.Entry)):
            return False
        k = Gdk.keyval_name(keyval)
        if state & Gdk.ModifierType.CONTROL_MASK:
            if k in ("z", "Z"):
                (self.redo if state & Gdk.ModifierType.SHIFT_MASK else self.undo)()
                return True
            if k in ("y", "Y"):
                self.redo()
                return True
            if k in ("Left", "Right"):
                self._jump_edit(-1 if k == "Left" else 1)
                return True
            pick = {"0": "m", "agrave": "m", "Agrave": "m", "KP_0": "m", "1": "0", "ampersand": "0", "KP_1": "0",
                    "2": "1", "eacute": "1", "Eacute": "1", "KP_2": "1", "3": "2", "quotedbl": "2",
                    "4": "3", "apostrophe": "3"}.get(k)
            if self.preview_group and pick and (pick == "m" or int(pick) < len(self.screens)):
                self.preview_group.set_active_name(pick)
                return True
            return False
        if state & Gdk.ModifierType.ALT_MASK:
            return False
        digit = {"1": 0, "2": 1, "3": 2, "4": 3, "ampersand": 0, "eacute": 1, "quotedbl": 2, "apostrophe": 3,
                 "KP_1": 0, "KP_2": 1, "KP_3": 2, "KP_4": 3}.get(k)
        if self.multi and digit is not None and digit < len(self.screens):
            self._set_angle_here(digit)
            return True
        if self.multi and k in ("b", "B"):
            self._switch_here()
            return True
        if k == "Escape" and self.fullscreen:
            self.toggle_fullscreen()
        elif k in ("f", "F"):
            self.toggle_fullscreen()
        elif k == "space":
            self.toggle_play()
        elif k in ("i", "I"):
            self.mark("in")
        elif k in ("o", "O"):
            self.mark("out")
        elif k in ("x", "X"):
            self._add_cut_here()
        elif k in ("c", "C"):
            self._cut_mark_start()
        elif k in ("v", "V"):
            self._cut_mark_end()
        elif k == "Escape" and self.cut_start is not None:
            self._set_cut_start(None)
        elif k == "Home":
            self._seek_to(self.t_in)
        elif k == "End":
            self._seek_to(max(self.t_in, self.t_out - 0.1))
        elif k in ("Left", "Right") and self.player and not isinstance(focus, Gtk.Scale):
            direction = -1 if k == "Left" else 1
            if state & Gdk.ModifierType.SHIFT_MASK:
                self._seek_to(min(max(self.timeline.pos + direction, 0), self.meta["duration"]))
            else:
                self._step_frame(direction)
        else:
            return False
        return True

    # ------------------------------------------------------------ presets
    def _set_combo(self, row, values, value):
        row.set_selected(values.index(value) if value in values else 0)

    def _on_normalize(self, row, _):
        cfg = load_config()
        cfg["normalize_audio"] = row.get_active()
        save_config(cfg)
        self._update_summary()

    def _after_edit(self):
        self._update_stats()
        has_cuts = len(self.kept_segments()) > 1
        if has_cuts and self.presets.get_active_name() == "copy":
            self.win.toast("\"Original\" can't remove sections: Discord preset selected")
            self._select_preset("discord")
        elif self.presets.get_active_name() == "discord":
            self._select_preset("discord")   # the bitrate depends on the final duration
        else:
            self._update_sensitivity()
            self._update_summary()

    def _select_preset(self, key):
        self.updating = True
        m = self.meta
        codecs = [c for c, _ in CODECS]
        if key == "discord":
            limit = self.cfg["discord_limit_mb"]
            bps = limit * 1e6 * 8 * 0.94 / max(self.kept_duration(), 0.1) - 160_000
            h = 1440 if bps >= 9e6 else 1080 if bps >= 5e6 else 720 if bps >= 2e6 else 480
            fps = 60 if bps >= 1.5e6 else 30
            self._set_combo(self.res_row, self.heights, h if h < m["height"] else 0)
            self._set_combo(self.fps_row, self.fps_choices, fps if fps < m["fps"] else 0)
            self._set_combo(self.codec_row, codecs, "h264")
            self.size_row.set_value(limit)
        elif key == "hq":
            self._set_combo(self.res_row, self.heights, 0)
            self._set_combo(self.fps_row, self.fps_choices, 0)
            self._set_combo(self.codec_row, codecs, "h264")
            self.size_row.set_value(0)
            self.quality_row.set_value(20)
        elif key == "light":
            self._set_combo(self.res_row, self.heights, 720 if 720 < m["height"] else 0)
            self._set_combo(self.fps_row, self.fps_choices, 30 if 30 < m["fps"] else 0)
            self._set_combo(self.codec_row, codecs, "h264")
            self.size_row.set_value(0)
            self.quality_row.set_value(27)
        if self.presets.get_active_name() != key:
            self.presets.set_active_name(key)
        self.updating = False
        self._update_sensitivity()
        self._update_summary()

    def _on_preset(self, group, _):
        if not self.updating and group.get_active_name():
            self._select_preset(group.get_active_name())

    def _on_custom(self, *_):
        self._update_sensitivity()
        if not self.updating:
            self.updating = True
            self.presets.set_active(Gtk.INVALID_LIST_POSITION)
            self.updating = False
        self._update_summary()

    def _export_screen(self):
        """None = edit, k = screen k only; always 0 for a single-screen clip."""
        if not self.multi or not self.screen_row:
            return 0
        choice = self.screen_choices[self.screen_row.get_selected()]
        if choice is None and len(self.angles) == 1:   # no switch: the edit is a single screen
            return self.angles[0][1]
        return choice

    def _update_sensitivity(self):
        # "Original" copies a single file without re-encoding: impossible for an edit, another screen or
        # removed sections (it can only cut at the start and the end).
        copy_ok = self._export_screen() == 0 and len(self.kept_segments()) <= 1
        self.preset_toggles["copy"].set_enabled(copy_ok)
        if not copy_ok and self.presets.get_active_name() == "copy":
            self._select_preset("hq")
            return
        copy = self.presets.get_active_name() == "copy"
        for r in (self.res_row, self.fps_row, self.codec_row, self.size_row):
            r.set_sensitive(not copy)
        self.quality_row.set_sensitive(not copy and self.size_row.get_value() == 0)
        self.norm_row.set_sensitive(not copy)

    def _settings(self):
        key = self.presets.get_active_name() or "custom"
        return {
            "screen": self._export_screen(),
            "copy": key == "copy",
            "height": self.heights[self.res_row.get_selected()],
            "fps": self.fps_choices[self.fps_row.get_selected()],
            "codec": CODECS[self.codec_row.get_selected()][0],
            "target": int(self.size_row.get_value()),
            "qp": int(self.quality_row.get_value()),
        }

    def _update_summary(self):
        s = self._settings()
        m = self.meta
        dur = self.kept_duration()
        key = self.presets.get_active_name()
        intro = dict((k, d) for k, _l, d in PRESETS).get(key, "Custom settings")
        if key == "discord":
            intro = f"To send it on Discord without Nitro (under {self.cfg['discord_limit_mb']} MB)"
        if self.multi:
            sc = s["screen"]
            switches = len(self.angles) - 1
            intro += (f" · edit, {switches} switch{'es' if switches > 1 else ''}" if sc is None
                      else f" · screen {sc + 1} only")
        intro = f"<span weight='600'>{intro}</span>\n"
        if s["copy"]:
            est = self.item.size * dur / max(m["duration"], 0.1)
            text = (f"{intro}<b>{m['height']}p{m['fps']}</b> · ≈ <b>{fmt_size(est)}</b> · {fmt_clock(dur)}\n"
                    "<small>Mixed tracks + separate tracks kept · cut snapped to keyframes</small>")
        else:
            h = s["height"] or m["height"]
            fps = s["fps"] or m["fps"]
            codec = dict(CODECS)[s["codec"]].split(" ")[0]
            size = f"≤ <b>{s['target']} MB</b>" if s["target"] else "size depends on the content"
            kind = "sw" if Exporter.force_sw else ENCODER_DETECTOR.get(s["codec"])[0]
            enc = BACKEND_LABELS[kind] if ENCODER_DETECTOR.ready or Exporter.force_sw else "detecting…"
            norm = " · normalized volume" if self.norm_row.get_active() else ""
            text = (f"{intro}<b>{h}p{fps}</b> · {codec} · {size} · {fmt_clock(dur)}\n"
                    f"<small>Encoding: {enc}{norm}</small>")
        self.summary.set_markup(text)
        if getattr(self, "tile_acomp", None):
            self._update_export_state()

    # ------------------------------------------------------------ export & sharing
    def _export_path(self, tag, ext=".mp4"):
        os.makedirs(exports_dir(self.cfg), exist_ok=True)
        return os.path.join(exports_dir(self.cfg), f"{self.item.stem}-{tag}{ext}")

    def _latest_export(self, ext=".mp4"):
        prefix = self.item.stem + "-"
        try:
            files = [e for e in os.scandir(exports_dir(self.cfg))
                     if e.is_file() and e.name.startswith(prefix) and e.name.endswith(ext)]
        except OSError:
            return None
        return max(files, key=lambda e: e.stat().st_mtime).path if files else None

    def _refresh_share(self):
        m = self.meta
        n = m["audio_tracks"]
        tracks = f" · {n} mixed tracks" if n > 1 else ""
        # Several tracks: send a copy with mixed audio (otherwise only the 1st track would be heard).
        gains = share_gains(self.item.path, n, self.cfg)
        # Not right away: the video, the frame strip and the waveforms go first.
        def later():
            if self.player:
                SHARE.prepare(self.item.path, gains)
                if gains:
                    SHARE.prepare(self.item.path, gains, audio=True)
            return False

        GLib.timeout_add(2500, later)
        self.tile_orig.set_file(self.item.path, f"{fmt_size(self.item.size)} · {m['height']}p{m['fps']}{tracks}",
                                self.item.texture,
                                resolve=lambda: SHARE.get_sync(self.item.path, gains, m["duration"]))
        limit = self.cfg["discord_limit_mb"] * 1e6
        for tile, ext, hint in ((self.tile_comp, ".mp4", "Not exported yet"),
                                (self.tile_acomp, ".mp3", "Not exported yet")):
            latest = self._latest_export(ext)
            if latest:
                size = os.path.getsize(latest)
                tile.base_subtitle = (f"{'MP3 · ' if ext == '.mp3' else ''}{fmt_size(size)}"
                                      + (" · ready for Discord" if size <= limit else ""))
                tile.set_file(latest, tile.base_subtitle, self.item.texture)
            else:
                tile.base_subtitle = None
                tile.set_file(None, hint, self.item.texture)
        if gains:   # audio of the original clip, mixed tracks, as MP3
            self.tile_aorig.set_file(self.item.path, f"MP3 · {n} track{'s' if n > 1 else ''}"
                                     + (" mixed" if n > 1 else ""),
                                     resolve=lambda: SHARE.get_sync(self.item.path, gains, m["duration"], audio=True))
        else:
            self.tile_aorig.set_file(None, "This clip has no audio")
        self.export_state_now = None   # cards recreated: state reapplied
        self._update_export_state()

    def _set_export_button(self, busy, text=None):
        self.export_btn.set_child(Adw.ButtonContent(
            icon_name="process-stop-symbolic" if busy else "document-save-symbolic",
            label=text or ("Cancel" if busy else "Export")))
        self.export_btn.remove_css_class("destructive-action" if not busy else "suggested-action")
        self.export_btn.add_css_class("destructive-action" if busy else "suggested-action")
        self.progress.set_visible(busy)

    def _export(self, *_):
        """Export = the video (with audio) then, right after, the audio only as MP3 (same edit, same volume):
        both "Edited" cards of the Share panel are up to date in one go. The MP3 only costs an audio pass."""
        if self.exporter:
            self.exporter.cancel()
            return
        s = self._settings()
        m = self.meta
        segments = self.kept_segments()
        plan = video_plan(segments, self.angles, self.screens, only=s["screen"]) if self.multi else None
        if s["copy"]:
            tag = "excerpt"
        else:
            tag = f"{s['height'] or m['height']}p{s['fps'] or m['fps']}"
            tag += f"-{s['target']}mb" if s["target"] else f"-q{s['qp']}"
            if s["codec"] != "h264":
                tag += f"-{s['codec']}"
            if self.multi:
                tag += "-edit" if s["screen"] is None else f"-screen{s['screen'] + 1}"
        out = self._export_path(tag)
        out_audio = self._export_path("audio", ".mp3")
        tracks = [{"gain": g, "muted": mu, "points": p} for g, mu, p in self._mix_state()]
        with_audio = any(not t["muted"] for t in tracks)
        sig = self._edit_sig()
        self.progress.set_fraction(0)
        self._set_export_button(True, "Cancel · preparing")
        started = time.monotonic()
        video = {}

        def progress(f):
            self.progress.set_fraction(f)
            self._set_export_button(True, f"Cancel · {int(f * 100)} %")

        def finish(ok, err):
            self.exporter = None
            self._set_export_button(False)
            self._update_summary()
            if ok:
                self.export_sig = sig
                set_clip_field(self.item.path, "export_sig", sig)
                sizes = fmt_size(os.path.getsize(out))
                if with_audio and os.path.exists(out_audio):
                    sizes += f" + audio {fmt_size(os.path.getsize(out_audio))}"
                self.win.toast(f"Exported in {time.monotonic() - started:.1f} s · {sizes}")
            elif err:
                self.win.toast(f"Export failed: {err}")
            self._refresh_share()

        def video_done(ok, err):
            if not ok or not with_audio:
                finish(ok, err)
                return
            # Audio only: same mix, same normalization gain (no second analysis).
            self._set_export_button(True, "Cancel · audio")
            audio = Exporter(self.item.path, out_audio, segments, 0, 0, "h264", 0, 0, tracks, False,
                             lambda f: None, finish, normalize=self.norm_row.get_active() and not s["copy"],
                             audio_only=True)
            audio.norm_gain = video["exp"].norm_gain if audio.normalize else None
            audio.limit = video["exp"].limit
            self.exporter = audio
            audio.start()

        self.exporter = video["exp"] = Exporter(
            self.item.path, out, segments, s["height"], s["fps"], s["codec"], s["target"], s["qp"], tracks, s["copy"],
            progress, video_done, normalize=self.norm_row.get_active(), screens=self.screens, plan=plan)
        self.exporter.start()

    # ------------------------------------------------------------ export up to date?
    def _edit_sig(self):
        """Fingerprint of the edit (excerpt, cuts, mix, switches): compared with the last export's."""
        return json.dumps({
            "range": [round(self.t_in, 2), round(self.t_out, 2)],
            "cuts": [[round(a, 2), round(b, 2)] for a, b in self.cuts],
            "mix": [[round(g, 3), bool(mu), [[round(t, 2), round(v, 3)] for t, v in p]]
                    for g, mu, p in self._mix_state()],
            "angles": [[round(t, 2), k] for t, k in self.angles] if self.multi else None,
        }, sort_keys=True)

    def _update_export_state(self):
        """Under the Export button: is the last export up to date with the edit? (and "out of date" on the
        Edited cards of the Share panel)."""
        exported = self._latest_export(".mp4") is not None
        if not exported:
            state, css, text = "none", "dim-label", "Not exported yet"
        elif self.export_sig is None:
            state, css, text = "unknown", "dim-label", "Last export: predates change tracking"
        elif self.export_sig == self._edit_sig():
            state, css, text = "ok", "success", "✓ The last export is up to date"
        else:
            state, css, text = "stale", "warning", "⚠ Edit changed since the last export"
        if state == getattr(self, "export_state_now", None):
            return
        self.export_state_now = state
        self.export_state.set_label(text)
        for c in ("dim-label", "success", "warning"):
            self.export_state.remove_css_class(c)
        self.export_state.add_css_class(css)
        for tile in (self.tile_comp, self.tile_acomp):
            base = getattr(tile, "base_subtitle", None)
            if base is not None and tile.path:
                tile.subtitle.set_label(base + (" · out of date" if state == "stale" else ""))
            tile.set_stale(state == "stale" and bool(tile.path))

    # ------------------------------------------------------------ file
    def _on_rename(self, label, _):
        if label.get_editing():
            return
        new = label.get_text().strip().replace("/", "-")
        if not new or new == self.item.stem:
            label.set_text(self.item.stem)
            return
        try:
            dest = rename_clip(self.item.path, new, self.cfg)
        except FileExistsError:
            self.win.toast("A clip already has this name")
            label.set_text(self.item.stem)
            return
        except OSError as e:
            self.win.toast(f"Couldn't rename: {e.strerror}")
            label.set_text(self.item.stem)
            return
        self.item.path = dest
        self.set_title(new)
        self._refresh_share()
        self.win.toast("Clip renamed")

    def _delete(self, *_):
        dialog = Adw.AlertDialog(heading="Move this clip to the trash?", body=self.item.name)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("trash", "Trash")
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r == "trash":
                self.destroy_player()
                self.win.trash_clip(self.item.path)
                self.win.nav.pop()

        dialog.connect("response", resp)
        dialog.present(self.win)

    def destroy_player(self):
        if self.fullscreen:
            self.toggle_fullscreen()
        if self.exporter:
            self.exporter.cancel()
        if self.tick:
            GLib.source_remove(self.tick)
            self.tick = 0
        if self.player:
            self.player.destroy()
            self.player = None
        for vp in self.vplayers.values():
            vp.destroy()
        self.vplayers = {}
