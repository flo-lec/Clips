"""Preferences: audio sources (Moments-style), recording, shortcut, sharing."""
import json
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from clips_common import (APP_DIR, SERVICE, SHORTCUTS, SOURCE_ICONS, available_sounds, layer_shell_available,  # noqa: E402
                          load_config, play_sound, save_config)
from editor import CODECS  # noqa: E402
from media import BACKEND_LABELS, ENCODER_DETECTOR, run_async  # noqa: E402
from widgets import open_uri  # noqa: E402

RECORDER_KEYS = ("monitor", "extra_monitors", "replay_seconds", "fps", "quality", "codec", "sources", "clips_dir")


class Preferences(Adw.PreferencesDialog):
    FPS = [30, 60, 120, 144, 165, 240]
    QUALITY = [("medium", "Medium"), ("high", "High"), ("very_high", "Very high"), ("ultra", "Ultra")]
    GAME_MODES = [("except_chat", "All PC audio except voice chat"),
                  ("all", "All PC audio"),
                  ("app", "A single application")]

    def __init__(self, window):
        super().__init__(title="Preferences", search_enabled=False)
        self.win = window
        self.cfg = load_config()
        self.before = json.dumps({k: self.cfg[k] for k in RECORDER_KEYS}, sort_keys=True)
        self.add(self._page_sources())
        self.add(self._page_recording())
        self.connect("closed", self._on_closed)
        run_async(["gpu-screen-recorder", "--list-application-audio"], self._got_apps)
        run_async(["gpu-screen-recorder", "--list-audio-devices"], self._got_devices)
        run_async(["gpu-screen-recorder", "--list-monitors"], self._got_monitors)

    # ---- Sources page (Moments-style)
    def _page_sources(self):
        page = Adw.PreferencesPage(title="Audio sources", icon_name="audio-speakers-symbolic")
        src = self.cfg["sources"]
        g = Adw.PreferencesGroup(
            title="Recorded tracks",
            description="Like in Moments, each source has its own track, adjustable later in the editor. "
                        "Nothing is rerouted: your audio outputs and inputs stay exactly as they are.")

        # Game
        self.game_row = Adw.ExpanderRow(title="Game", show_enable_switch=True,
                                        enable_expansion=src["game"]["enabled"], expanded=True)
        self.game_row.add_prefix(Gtk.Image(icon_name=SOURCE_ICONS["game"]))
        self.game_mode = Adw.ComboRow(title="Capture", model=Gtk.StringList.new([l for _, l in self.GAME_MODES]))
        modes = [k for k, _ in self.GAME_MODES]
        self.game_mode.set_selected(modes.index(src["game"]["mode"]) if src["game"]["mode"] in modes else 0)
        self.game_app = Adw.ComboRow(title="Application")
        self.game_row.add_row(self.game_mode)
        self.game_row.add_row(self.game_app)
        self.game_row.add_row(self._level_row("game"))
        g.add(self.game_row)

        # Voice chat
        self.chat_row = Adw.ExpanderRow(title="Voice chat", subtitle="Discord, Vesktop…", show_enable_switch=True,
                                         enable_expansion=src["chat"]["enabled"], expanded=True)
        self.chat_row.add_prefix(Gtk.Image(icon_name=SOURCE_ICONS["chat"]))
        self.chat_app = Adw.ComboRow(title="Application")
        self.chat_row.add_row(self.chat_app)
        self.chat_row.add_row(self._level_row("chat"))
        g.add(self.chat_row)

        # Mic
        self.mic_row = Adw.ExpanderRow(title="Mic", show_enable_switch=True,
                                       enable_expansion=src["mic"]["enabled"], expanded=True)
        self.mic_row.add_prefix(Gtk.Image(icon_name=SOURCE_ICONS["mic"]))
        self.mic_dev = Adw.ComboRow(title="Device")
        self.mic_row.add_row(self.mic_dev)
        self.mic_row.add_row(self._level_row("mic"))
        g.add(self.mic_row)
        page.add(g)

        # Advanced: one track per application (Spotify, browser, game…)
        gs = Adw.PreferencesGroup(
            title="One track per application",
            description="Advanced option: each checked application gets its own track, adjustable separately in "
                        "the editor. The rest of the audio goes to \"Other sounds\". Apps are recognized by their name, "
                        "even if they are started later.")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                             tooltip_text="Look for applications playing audio")
        refresh.connect("clicked", lambda *_: run_async(["gpu-screen-recorder", "--list-application-audio"], self._got_apps))
        gs.set_header_suffix(refresh)
        self.split_row = Adw.SwitchRow(title="Split applications", active=src["split"]["enabled"],
                                       subtitle="Off: all app audio goes to the Game track")
        self.split_row.connect("notify::active", self._on_split)
        gs.add(self.split_row)
        self.add_app = Adw.EntryRow(title="Add an application by name", show_apply_button=True)
        self.add_app.connect("apply", self._add_split_app)
        gs.add(self.add_app)
        self.split_group = gs
        self.split_rows = {}
        page.add(gs)

        self.apps = []
        self.devices = []
        self.filling = False
        self._fill_apps([])
        self._fill_devices([])
        self._fill_split([])
        self.game_row.connect("notify::enable-expansion", lambda r, _: self._src("game", "enabled", r.get_enable_expansion()))
        self.chat_row.connect("notify::enable-expansion", lambda r, _: self._src("chat", "enabled", r.get_enable_expansion()))
        self.mic_row.connect("notify::enable-expansion", lambda r, _: self._src("mic", "enabled", r.get_enable_expansion()))
        self.game_mode.connect("notify::selected", self._on_game_mode)
        self.game_app.connect("notify::selected", lambda r, _: self.apps and self._src("game", "app", self.apps[r.get_selected()]))
        self.chat_app.connect("notify::selected", lambda r, _: self.apps and self._src("chat", "app", self.chat_apps[r.get_selected()]))
        self.mic_dev.connect("notify::selected", lambda r, _: self.devices and self._src("mic", "device", self.devices[r.get_selected()][0]))
        self._on_game_mode(self.game_mode, None, save=False)
        return page

    # ---- per-application tracks
    def _fill_split(self, detected):
        split = self.cfg["sources"]["split"]
        chat = self.cfg["sources"]["chat"]["app"].lower()
        names = list(dict.fromkeys(split["apps"] + detected))
        self.split_group.remove(self.add_app)
        for name in names:
            if name.lower() == chat or name in self.split_rows:
                if name in self.split_rows and name in detected:
                    self.split_rows[name].set_subtitle("Playing audio right now")
                continue
            row = Adw.SwitchRow(title=name, active=name in split["apps"],
                                subtitle="Playing audio right now" if name in detected else "")
            row.add_prefix(Gtk.Image(icon_name=SOURCE_ICONS["app"]))
            row.connect("notify::active", lambda r, _, n=name: self._toggle_split_app(n, r.get_active()))
            self.split_rows[name] = row
            self.split_group.add(row)
        self.split_group.add(self.add_app)
        self._split_sensitivity()

    def _split_sensitivity(self):
        on = self.split_row.get_active()
        for row in self.split_rows.values():
            row.set_sensitive(on)
        self.add_app.set_sensitive(on)

    def _on_split(self, row, _):
        self.cfg["sources"]["split"]["enabled"] = row.get_active()
        save_config(self.cfg)
        self._split_sensitivity()

    def _toggle_split_app(self, name, on):
        apps = [a for a in self.cfg["sources"]["split"]["apps"] if a != name]
        if on:
            apps.append(name)
        self.cfg["sources"]["split"]["apps"] = apps
        save_config(self.cfg)

    def _add_split_app(self, entry):
        name = entry.get_text().strip()
        entry.set_text("")
        if not name:
            return
        if name not in self.cfg["sources"]["split"]["apps"]:
            self.cfg["sources"]["split"]["apps"].append(name)
            save_config(self.cfg)
        if name in self.split_rows:
            self.split_rows[name].set_active(True)
        else:
            self._fill_split([])

    def _level_row(self, kind):
        row = Adw.ActionRow(title="Default level", subtitle="Applied when a clip is opened in the editor")
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 200, 5)
        scale.set_value(round(self.cfg["levels"].get(kind, 1.0) * 100))
        scale.set_size_request(160, -1)
        scale.set_valign(Gtk.Align.CENTER)
        scale.add_mark(100, Gtk.PositionType.BOTTOM, None)
        pct = Gtk.Label(label=f"{int(scale.get_value())} %", width_chars=5, css_classes=["numeric", "caption"])

        def changed(s):
            pct.set_label(f"{int(s.get_value())} %")
            self.cfg["levels"][kind] = s.get_value() / 100
            save_config(self.cfg)

        scale.connect("value-changed", changed)
        row.add_suffix(scale)
        row.add_suffix(pct)
        return row

    def _on_game_mode(self, row, _, save=True):
        mode = self.GAME_MODES[row.get_selected()][0]
        self.game_app.set_visible(mode == "app")
        if save:
            self._src("game", "mode", mode)

    def _src(self, kind, key, value):
        if self.filling:
            return
        self.cfg["sources"][kind][key] = value
        save_config(self.cfg)
        self._update_subtitles()

    def _update_subtitles(self):
        s = self.cfg["sources"]
        chat = s["chat"]["app"] if s["chat"]["enabled"] else ""
        mode = s["game"]["mode"]
        if mode == "app" and s["game"]["app"]:
            game = f"Only {s['game']['app']}"
        elif mode == "except_chat" and chat:
            game = f"All PC audio except {chat}"
        else:
            game = "All PC audio"
        self.game_row.set_subtitle(game if s["game"]["enabled"] else "Off")
        self.chat_row.set_subtitle(chat or "Off")
        dev = s["mic"]["device"]
        label = next((d for n, d in self.devices if n == dev), "Default input" if dev == "default_input" else dev)
        self.mic_row.set_subtitle(label if s["mic"]["enabled"] else "Off")

    def _fill_apps(self, detected):
        self.filling = True
        s = self.cfg["sources"]
        base = [s["chat"]["app"], "vesktop", "discord", "WEBRTC VoiceEngine"]
        self.chat_apps = list(dict.fromkeys(a for a in base + detected if a))
        self.apps = list(dict.fromkeys(a for a in [s["game"]["app"]] + detected if a)) or ["(no app detected)"]
        for row, values, current in ((self.chat_app, self.chat_apps, s["chat"]["app"]),
                                     (self.game_app, self.apps, s["game"]["app"])):
            row.set_model(Gtk.StringList.new(values))
            if current in values:
                row.set_selected(values.index(current))
        self.filling = False
        self._update_subtitles()

    def _fill_devices(self, devices):
        self.filling = True
        cur = self.cfg["sources"]["mic"]["device"]
        self.devices = [("default_input", "System default input")] + devices
        if cur not in [n for n, _ in self.devices]:
            self.devices.append((cur, cur))
        self.mic_dev.set_model(Gtk.StringList.new([d for _, d in self.devices]))
        self.mic_dev.set_selected([n for n, _ in self.devices].index(cur))
        self.filling = False
        self._update_subtitles()

    def _got_apps(self, ok, out, _e):
        if ok:
            detected = [line.strip() for line in out.decode().splitlines()
                        if line.strip() and line.strip() not in ("python3",)]
            self._fill_apps(detected)
            self._fill_split(detected)

    def _got_devices(self, ok, out, _e):
        if ok:
            devs = []
            for line in out.decode().splitlines():
                name, _, desc = line.partition("|")
                if name.startswith("alsa_input") or (name and "input" in name and not name.endswith(".monitor")
                                                     and name not in ("default_input",)):
                    devs.append((name, desc or name))
            self._fill_devices(devs)

    # ---- Recording page
    def _page_recording(self):
        page = Adw.PreferencesPage(title="Recording", icon_name="camera-video-symbolic")
        lib = Adw.PreferencesGroup(title="Library")
        game = Adw.SwitchRow(title="Detect the game", active=self.cfg.get("detect_game", True),
                             subtitle="Names each clip after the running Steam game and adds a per-game filter. "
                                      "Doesn't touch any window.")
        game.connect("notify::active", lambda r, _: self._set("detect_game", r.get_active()))
        lib.add(game)
        page.add(lib)
        rec = Adw.PreferencesGroup(title="Replay", description="The last seconds stay in RAM; nothing is written until you save.")
        self.monitors = [self.cfg["monitor"]]
        self.mon_row = Adw.ComboRow(title="Screen", subtitle="With audio",
                                    model=Gtk.StringList.new([self.cfg["monitor"]]))
        rec.add(self.mon_row)
        # Other screens recorded at the same time (no audio): as many as you want, checked in the list.
        self.extra_row = Adw.ExpanderRow(title="Other recorded screens",
                                         subtitle="In the editor, switch from one screen to another or export the one "
                                                  "you want")
        self.extra_checks = []
        rec.add(self.extra_row)
        dur = Adw.SpinRow.new_with_range(5, 600, 5)
        dur.set_title("Kept duration (seconds)")
        dur.set_value(self.cfg["replay_seconds"])
        dur.connect("notify::value", lambda r, _: self._set("replay_seconds", int(r.get_value())))
        rec.add(dur)
        rec.add(self._combo("Frames per second", [str(f) for f in self.FPS], self.FPS, "fps"))
        rec.add(self._combo("Quality", [l for _, l in self.QUALITY], [k for k, _ in self.QUALITY], "quality"))
        rec.add(self._combo("Codec", [l for _, l in CODECS], [k for k, _ in CODECS], "codec"))
        page.add(rec)

        fb = Adw.PreferencesGroup(title="Save confirmation")
        # Sound: switch + choice + preview
        snd = Adw.ExpanderRow(title="Sound", subtitle="Played when a clip is saved",
                              show_enable_switch=True, enable_expansion=self.cfg.get("save_sound", True))
        snd.connect("notify::enable-expansion", lambda r, _: self._set("save_sound", r.get_enable_expansion()))
        sounds = available_sounds()
        keys = [k for k, _l, _p in sounds]
        choice = Adw.ComboRow(title="Chosen sound", model=Gtk.StringList.new([l for _k, l, _p in sounds]))
        if self.cfg.get("sound") in keys:
            choice.set_selected(keys.index(self.cfg["sound"]))
        choice.connect("notify::selected", lambda r, _: (self._set("sound", keys[r.get_selected()]),
                                                         play_sound(keys[r.get_selected()], int(scale.get_value()))))
        listen = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                            css_classes=["flat", "circular"], tooltip_text="Listen")
        listen.connect("clicked", lambda *_: play_sound(keys[choice.get_selected()], int(scale.get_value())))
        choice.add_suffix(listen)
        snd.add_row(choice)
        vol = Adw.ActionRow(title="Volume")
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        scale.set_value(self.cfg.get("sound_volume", 70))
        scale.set_size_request(200, -1)
        scale.set_valign(Gtk.Align.CENTER)
        pct = Gtk.Label(label=f"{int(scale.get_value())} %", width_chars=5, css_classes=["numeric", "caption"])
        self._vol_preview = 0

        def on_vol(sc):
            v = int(sc.get_value())
            pct.set_label(f"{v} %")
            self._set("sound_volume", v)
            if self._vol_preview:   # preview on release (not at every notch)
                GLib.source_remove(self._vol_preview)
            self._vol_preview = GLib.timeout_add(350, lambda: (play_sound(keys[choice.get_selected()], v),
                                                               setattr(self, "_vol_preview", 0), False)[2])

        scale.connect("value-changed", on_vol)
        vol.add_suffix(scale)
        vol.add_suffix(pct)
        snd.add_row(vol)
        fb.add(snd)
        # Popup over the game
        ok = layer_shell_available()
        pop = Adw.SwitchRow(title="On-screen popup", active=self.cfg.get("save_popup", True),
                            subtitle="Small \"Clip saved\" toast over the game, without stealing its focus"
                            if ok else "Requires gtk4-layer-shell (sudo pacman -S gtk4-layer-shell)")
        pop.connect("notify::active", lambda r, _: self._set("save_popup", r.get_active()))
        fb.add(pop)
        notif = Adw.SwitchRow(title="Desktop notification", active=self.cfg.get("save_notification", True),
                              subtitle="With the \"Edit\" and \"Open folder\" buttons")
        notif.connect("notify::active", lambda r, _: self._set("save_notification", r.get_active()))
        fb.add(notif)
        page.add(fb)
        setup = Gtk.Button(label="Install", valign=Gtk.Align.CENTER, css_classes=["flat"],
                           tooltip_text="Set up the shortcuts automatically (KDE, GNOME…)")
        setup.connect("clicked", self._setup_shortcut)
        fb = Adw.PreferencesGroup(title="Shortcuts", header_suffix=setup,
                                  description="Global shortcuts, active even in game; can be changed later in "
                                              "your desktop settings")
        for key, title, sub in (
                ("save", "Save a clip", "The last seconds of the replay"),
                ("rec_start", "Start a recording", "Records until stopped, on top of the replay"),
                ("rec_stop", "Stop the recording", "The clip shows up in the library")):
            row = Adw.ActionRow(title=title, subtitle=sub)
            row.add_suffix(Gtk.ShortcutLabel(accelerator=SHORTCUTS[key], valign=Gtk.Align.CENTER))
            fb.add(row)
        page.add(fb)
        # Export encoder: the fastest measured on this machine, or forced
        exp = Adw.PreferencesGroup(title="Exports")
        self.enc_choices = ["auto", "gpu", "cpu"]
        self.enc_row = Adw.ComboRow(title="Encoder", model=Gtk.StringList.new(
            ["Automatic (fastest)", "Graphics card", "Processor"]))
        cur = self.cfg.get("export_encoder", "auto")
        self.enc_row.set_selected(self.enc_choices.index(cur) if cur in self.enc_choices else 0)
        self.enc_row.connect("notify::selected", self._on_encoder)
        exp.add(self.enc_row)
        page.add(exp)
        self._encoder_summary()
        if not ENCODER_DETECTOR.ready:
            ENCODER_DETECTOR.detect(self._encoder_summary)

        share = Adw.PreferencesGroup(title="Sharing")
        lim = Adw.SpinRow.new_with_range(1, 1000, 1)
        lim.set_title("Discord upload limit (MB)")
        lim.set_subtitle("10 MB without Nitro")
        lim.set_value(self.cfg["discord_limit_mb"])
        lim.connect("notify::value", lambda r, _: self._set("discord_limit_mb", int(r.get_value())))
        share.add(lim)
        folder = Adw.ActionRow(title="Clips folder", subtitle=self.cfg["clips_dir"])
        btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        btn.connect("clicked", lambda *_: open_uri(self.cfg["clips_dir"]))
        folder.add_suffix(btn)
        share.add(folder)
        page.add(share)
        return page

    def _got_monitors(self, ok, out, _err):
        lines = [l for l in out.decode().splitlines() if "|" in l] if ok else []
        if not lines:
            return
        names = [l.split("|")[0] for l in lines]
        labels = [l.replace("|", " — ") for l in lines]
        if self.cfg["monitor"] not in names:
            names.insert(0, self.cfg["monitor"])
            labels.insert(0, self.cfg["monitor"])
        self.monitors = names
        self.mon_row.set_model(Gtk.StringList.new(labels))
        self.mon_row.set_selected(names.index(self.cfg["monitor"]))
        self.mon_row.connect("notify::selected", self._on_monitor)
        self.mon_labels = dict(zip(names, labels))
        self._fill_extra_monitors()

    def _fill_extra_monitors(self):
        """One checkbox per detected screen, except the main screen (which has the audio)."""
        for row in self.extra_checks:
            self.extra_row.remove(row)
        self.extra_checks = []
        chosen = [m for m in self.cfg.get("extra_monitors") or [] if m != self.cfg["monitor"]]
        others = [n for n in self.monitors if n != self.cfg["monitor"] and n != "screen"]
        for name in others:
            check = Gtk.CheckButton(active=name in chosen, valign=Gtk.Align.CENTER)
            row = Adw.ActionRow(title=self.mon_labels.get(name, name), activatable_widget=check)
            row.add_prefix(check)
            check.connect("toggled", lambda c, n=name: self._toggle_extra(n, c.get_active()))
            self.extra_row.add_row(row)
            self.extra_checks.append(row)
        self.extra_row.set_sensitive(bool(others))
        if chosen:
            self.extra_row.set_expanded(True)
        self._extra_summary()

    def _toggle_extra(self, name, on):
        chosen = [m for m in self.cfg.get("extra_monitors") or [] if m != name and m != self.cfg["monitor"]]
        if on:
            chosen.append(name)
        order = {n: i for i, n in enumerate(self.monitors)}   # in the order of the detected screens
        self._set("extra_monitors", sorted(chosen, key=lambda n: order.get(n, 99)))
        self._extra_summary()

    def _extra_summary(self):
        chosen = self.cfg.get("extra_monitors") or []
        self.extra_row.set_subtitle(
            ("Recorded at the same time, without audio: " + ", ".join(chosen)) if chosen
            else "None · in the editor, you'll be able to switch from one screen to another or export the one you want")

    def _on_monitor(self, row, _):
        self._set("monitor", self.monitors[row.get_selected()])
        self._set("extra_monitors", [m for m in self.cfg.get("extra_monitors") or [] if m != self.cfg["monitor"]])
        self._fill_extra_monitors()

    def _combo(self, title, labels, values, key):
        row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
        if self.cfg[key] in values:
            row.set_selected(values.index(self.cfg[key]))
        row.connect("notify::selected", lambda r, _: self._set(key, values[r.get_selected()]))
        return row

    def _on_encoder(self, row, _):
        self._set("export_encoder", self.enc_choices[row.get_selected()])
        self._encoder_summary()

    def _encoder_summary(self):
        """Subtitle: encoder used per codec, and measured times (graphics card / processor)."""
        if not ENCODER_DETECTOR.ready:
            self.enc_row.set_subtitle("Measuring speed…")
            return
        parts = []
        for c in ("h264", "hevc", "av1"):
            label = BACKEND_LABELS[ENCODER_DETECTOR.get(c)[0]]
            t = ENCODER_DETECTOR.timings.get(c, {})
            bench = f" ({t['gpu']:.1f} s GPU / {t['cpu']:.1f} s CPU)" if "gpu" in t and "cpu" in t else ""
            parts.append(f"{c.upper()}: {label}{bench}")
        self.enc_row.set_subtitle("\n".join(parts))

    def _set(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)

    def _setup_shortcut(self, *_):
        def done(ok, out, err):
            msg = (out.decode().strip() or err.strip()) if isinstance(out, bytes) else str(out)
            dialog = Adw.AlertDialog(heading="Shortcuts" if ok else "Shortcuts: manual action", body=msg)
            dialog.add_response("ok", "OK")
            dialog.present(self)

        run_async(["python3", os.path.join(APP_DIR, "setup_shortcut.py")], done)

    def _on_closed(self, *_):
        cfg = load_config()
        self.win.refresh_library()
        if json.dumps({k: cfg[k] for k in RECORDER_KEYS}, sort_keys=True) != self.before:
            def done(ok, _o, _e):
                self.win.refresh_recorder_state()
                if ok:
                    self.win.toast("Replay restarted with the new settings")
            run_async(["systemctl", "--user", "try-restart", SERVICE], done)
        else:
            self.win.refresh_recorder_state()
