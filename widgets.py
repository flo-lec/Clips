"""Widgets dessinés au GPU (snapshot GTK) : timeline, pistes audio avec courbe de volume, cartes de partage."""
import math
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402


# ---------------------------------------------------------------- helpers

def fmt_time(t):
    t = max(0.0, t)
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def fmt_size(n):
    return f"{n / 1e6:.1f} Mo" if n < 1e9 else f"{n / 1e9:.2f} Go"


def rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def point(x, y):
    return Graphene.Point().init(x, y)


def rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


def with_alpha(c, a):
    return rgba(c.red, c.green, c.blue, a)


def accent():
    return Adw.StyleManager.get_default().get_accent_color_rgba()


def rounded(x, y, w, h, r):
    rr = Gsk.RoundedRect()
    rr.init_from_rect(rect(x, y, w, h), r)
    return rr


def file_content_provider(path):
    """Contenu à glisser/copier : liste de fichiers (+ text/uri-list explicite pour Electron/Discord)."""
    gfile = Gio.File.new_for_path(path)
    uri = gfile.get_uri() + "\r\n"
    return Gdk.ContentProvider.new_union([
        Gdk.ContentProvider.new_for_value(Gdk.FileList.new_from_array([gfile])),
        Gdk.ContentProvider.new_for_bytes("text/uri-list", GLib.Bytes.new(uri.encode())),
    ])


def show_in_folder(path):
    """Ouvre le gestionnaire de fichiers avec le fichier sélectionné (sinon le dossier)."""
    try:
        Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
            "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1", None,
        ).call("ShowItems", GLib.Variant("(ass)", ([Gio.File.new_for_path(path).get_uri()], "")),
               Gio.DBusCallFlags.NONE, -1, None, None)
    except GLib.Error:
        open_uri(os.path.dirname(path))


def open_uri(path):
    Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)


_TEXTURES = {}


def texture(path):
    """Texture mise en cache (images de jeux Steam, miniatures…) ; None si illisible."""
    if not path:
        return None
    if path not in _TEXTURES:
        try:
            _TEXTURES[path] = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            _TEXTURES[path] = None
    return _TEXTURES[path]


def merge_ranges(ranges, min_len=0.05):
    out = []
    for a, b in sorted((min(a, b), max(a, b)) for a, b in ranges):
        if b - a < min_len:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


# ---------------------------------------------------------------- timeline

class TimelineCanvas(Gtk.Widget):
    """Bande d'images, sélection, passages retirés, graduations. Redessinée seulement quand elle change :
    la tête de lecture est un widget séparé, donc la lecture ne force pas de redessin."""
    HANDLE = 12
    KNOB_H = 16      # bande au-dessus des images : on y attrape la tête de lecture n'importe où
    STRIP_H = 58
    RULER_H = 18
    EDGE = 7         # zone de prise des bords d'un passage retiré
    BAND_H = 7       # bande des écrans (plusieurs écrans) : en bas des images
    # pas de bleu : c'est la couleur du cadre de sélection
    SCREEN_COLORS = [(0.18, 0.76, 0.49), (0.96, 0.47, 0.0), (0.57, 0.38, 0.92), (0.93, 0.76, 0.13)]

    def __init__(self, duration, on_seek, on_range, on_cuts, on_cut_clicked, on_angles=None):
        super().__init__()
        self.duration = max(duration, 0.01)
        self.t_in, self.t_out = 0.0, self.duration
        self.cuts = []
        self.pending_cut = None
        self.moving = None          # passage déplacé : (indice, début, fin, borne basse, borne haute)
        self.strip = None
        self.strip_frames = 1
        self.hover_x = None
        self.on_seek, self.on_range, self.on_cuts, self.on_cut_clicked = on_seek, on_range, on_cuts, on_cut_clicked
        self.on_angles = on_angles
        self.angles = None          # plusieurs écrans : [[instant, écran], …] ; None = un seul écran
        self.angle_drag = None
        self.on_live = None         # appelé à chaque changement visible (lignes « pistes par écran » à redessiner)
        self.pos = 0.0
        self.edge = None
        self.pending_start = None   # début d'un passage à retirer posé au clavier (C), en attente de la fin (V)
        self.set_size_request(-1, self.KNOB_H + self.STRIP_H + self.RULER_H)
        self.set_hexpand(True)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)
        self.drag = drag
        # clic droit sur un passage retiré : on le supprime (le passage revient dans le clip)
        right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right.connect("pressed", self._on_right_click)
        self.add_controller(right)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", lambda *_: self._set_hover(None))
        self.add_controller(motion)
        self.mode = None
        self.start_x = 0
        self.moved = False

    # géométrie
    def x_to_t(self, x):
        return min(max(x / max(self.get_width(), 1), 0), 1) * self.duration

    def t_to_x(self, t):
        return t / self.duration * self.get_width()

    def cut_at(self, t):
        return next((i for i, (a, b) in enumerate(self.cuts) if a <= t <= b), None)

    def _cut_edge(self, x):
        for i, (a, b) in enumerate(self.cuts):
            for side, t in ((0, a), (1, b)):
                if abs(x - self.t_to_x(t)) <= self.EDGE:
                    return i, side
        return None

    def _angle_marker(self, x, y):
        """Bascule d'écran sous la souris (repère en bas des images), sauf la 1re (début du clip)."""
        if not self.angles:
            return None
        band_top = self.KNOB_H + self.STRIP_H - self.BAND_H - 8
        if not band_top <= y <= self.KNOB_H + self.STRIP_H + 2:
            return None
        for i, (t, _k) in enumerate(self.angles):
            if i and abs(x - self.t_to_x(t)) <= 7:
                return i
        return None

    def set_angles(self, angles):
        self.angles = [list(a) for a in angles] if angles is not None else None
        self.queue_draw()

    def _hit(self, x, y):
        # Priorité : tête de lecture (bande du haut ou ±8 px), bascules d'écran, bords des passages, poignées de
        # l'extrait, intérieur d'un passage (on le déplace), puis le reste (on se positionne).
        if y < self.KNOB_H or abs(x - self.t_to_x(self.pos)) <= 8:
            return "scrub"
        if self._angle_marker(x, y) is not None:
            return "angle"
        if self._cut_edge(x):
            return "edge"
        if abs(x - self.t_to_x(self.t_in)) <= self.HANDLE:
            return "in"
        if abs(x - self.t_to_x(self.t_out)) <= self.HANDLE:
            return "out"
        if self.cut_at(self.x_to_t(x)) is not None:
            return "move"
        return "seek"

    def _on_right_click(self, gesture, _n, x, y):
        marker = self._angle_marker(x, y) if self.mode is None else None
        if marker is not None:   # clic droit sur une bascule : supprimée
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            del self.angles[marker]
            self.queue_draw()
            if self.on_live:
                self.on_live()
            if self.on_angles:
                self.on_angles(self.angles)
            return
        if self.mode is None and y >= self.KNOB_H:
            idx = self.cut_at(self.x_to_t(x))
            if idx is not None:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                self.on_cut_clicked(idx)

    # interactions
    def _motion(self, _c, x, y):
        if self.mode is None:
            hit = self._hit(x, y)
            name = {"in": "ew-resize", "out": "ew-resize", "edge": "col-resize", "move": "grab",
                    "scrub": "grab", "angle": "col-resize"}.get(hit, "pointer")
            tips = {"edge": "Glisser pour agrandir ou réduire le passage retiré",
                    "angle": "Bascule d'écran · glisser pour la déplacer · clic droit pour la supprimer",
                    "move": "Passage retiré · glisser pour le déplacer · clic droit pour le supprimer",
                    "seek": "Clic : se placer ici · Maj + glisser : retirer un passage"}
            self.set_tooltip_text(tips.get(hit) if y >= self.KNOB_H else None)
            self.set_cursor(Gdk.Cursor.new_from_name(name))
        self._set_hover(x)

    def _set_hover(self, x):
        self.hover_x = x
        self.queue_draw()

    def _begin(self, _g, x, y):
        self.start_x = x
        self.moved = False
        self.mode = self._hit(x, y)
        shift = self.drag.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK
        if shift and self.mode in ("seek", "move") and y >= self.KNOB_H:
            self.mode = "cut"   # Maj + glisser : dessiner un passage à retirer
        if self.mode == "scrub":
            self.set_cursor(Gdk.Cursor.new_from_name("grabbing"))
            self.on_seek(self.x_to_t(x), False)
        elif self.mode == "angle":
            self.angle_drag = self._angle_marker(x, y)
        elif self.mode == "edge":
            self.edge = self._cut_edge(x)
        elif self.mode == "cut":
            t = self.x_to_t(x)
            self.pending_cut = [t, t]
        elif self.mode == "move":
            # Le passage garde sa durée ; il ne passe ni sur ses voisins ni hors du clip.
            i = self.cut_at(self.x_to_t(x))
            a, b = self.cuts[i]
            lo = max([0.0] + [cb for ca, cb in self.cuts if cb <= a])
            hi = min([self.duration] + [ca for ca, cb in self.cuts if ca >= b])
            self.moving = (i, a, b, lo, hi)
            self.set_cursor(Gdk.Cursor.new_from_name("grabbing"))
        else:
            self._apply(x, final=False)

    def _update(self, g, dx, dy):
        self._update_inner(g, dx, dy)
        if self.on_live:
            self.on_live()

    def _update_inner(self, _g, dx, _dy):
        if abs(dx) > 3:
            self.moved = True
        if self.mode == "scrub":
            self.on_seek(self.x_to_t(self.start_x + dx), False)
        elif self.mode == "angle":
            i = self.angle_drag
            lo = self.angles[i - 1][0] + 0.1
            hi = self.angles[i + 1][0] - 0.1 if i + 1 < len(self.angles) else self.duration - 0.05
            self.angles[i][0] = min(max(self.x_to_t(self.start_x + dx), lo), hi)
            self.queue_draw()
        elif self.mode == "edge":
            i, side = self.edge
            t = self.x_to_t(self.start_x + dx)
            a, b = self.cuts[i]
            self.cuts[i] = [min(t, b - 0.1), b] if side == 0 else [a, max(t, a + 0.1)]
            self.queue_draw()
        elif self.mode == "cut":
            self.pending_cut[1] = self.x_to_t(self.start_x + dx)
            self.queue_draw()
        elif self.mode == "move":
            if self.moved:
                i, a, b, lo, hi = self.moving
                dt = dx / max(self.get_width(), 1) * self.duration
                dt = min(max(dt, lo - a), hi - b)
                self.cuts[i] = [a + dt, b + dt]
                self.queue_draw()
        else:
            self._apply(self.start_x + dx, final=False)

    def _end(self, _g, dx, _dy):
        if self.mode == "scrub":
            self.on_seek(self.x_to_t(self.start_x + dx), True)
            self.set_cursor(Gdk.Cursor.new_from_name("grab"))
        elif self.mode == "angle":
            self.angle_drag = None
            if self.moved and self.on_angles:
                self.on_angles(self.angles)
            elif not self.moved:   # simple clic sur une bascule : on s'y place
                self.on_seek(self.x_to_t(self.start_x), True)
        elif self.mode == "edge":
            self.on_cuts(merge_ranges(self.cuts))
        elif self.mode == "cut":
            a, b = sorted(self.pending_cut)
            self.pending_cut = None
            if b - a >= 0.1:
                self.on_cuts(merge_ranges(self.cuts + [[a, b]]))
        elif self.mode == "move":
            self.moving = None
            self.set_cursor(Gdk.Cursor.new_from_name("grab"))
            if self.moved:
                self.on_cuts(merge_ranges(self.cuts))
            else:   # simple clic dans un passage : on s'y place
                self.on_seek(self.x_to_t(self.start_x), True)
        else:
            self._apply(self.start_x + dx, final=True)
        self.mode = None
        self.queue_draw()

    def _apply(self, x, final):
        t = self.x_to_t(x)
        min_len = min(0.5, self.duration)
        if self.mode == "in":
            self.t_in = min(t, self.t_out - min_len)
            self.on_range(self.t_in, self.t_out, self.t_in, final)
        elif self.mode == "out":
            self.t_out = max(t, self.t_in + min_len)
            self.on_range(self.t_in, self.t_out, self.t_out, final)
        elif self.mode == "seek":
            self.on_seek(t, final)
        self.queue_draw()

    def set_range(self, t_in, t_out):
        self.t_in, self.t_out = t_in, t_out
        self.queue_draw()
        if self.on_live:
            self.on_live()

    def set_pending(self, t):
        self.pending_start = t
        self.queue_draw()

    def set_cuts(self, cuts):
        self.cuts = [list(c) for c in cuts]   # copie : le glissement d'un bord ne touche pas l'historique
        self.queue_draw()
        if self.on_live:
            self.on_live()

    def set_strip(self, texture, frames):
        self.strip, self.strip_frames = texture, frames
        self.queue_draw()

    # rendu
    def _text(self, s, text, x, y, color):
        layout = self.create_pango_layout(text)
        lw, _ = layout.get_pixel_size()
        x = min(max(x, 0), self.get_width() - lw)
        s.save()
        s.translate(point(x, y))
        s.append_layout(layout, color)
        s.restore()

    def do_snapshot(self, s):
        w, h = self.get_width(), self.STRIP_H
        acc = accent()
        fg = self.get_color()
        red = rgba(0.88, 0.11, 0.14)
        s.append_color(with_alpha(fg, 0.05), rect(0, 3, w, self.KNOB_H - 6))   # rail de la tête de lecture
        s.save()
        s.translate(point(0, self.KNOB_H))

        s.push_rounded_clip(rounded(0, 0, w, h, 10))
        s.append_color(with_alpha(fg, 0.08), rect(0, 0, w, h))
        if self.strip:
            fw = h * self.strip.get_width() / self.strip_frames / self.strip.get_height()
            cells = max(1, math.ceil(w / fw))
            for i in range(cells):
                k = min(self.strip_frames - 1, int(i * self.strip_frames / cells))
                x0 = i * fw
                s.push_clip(rect(x0, 0, fw, h))
                s.append_texture(self.strip, rect(x0 - k * fw, 0, self.strip_frames * fw, h))
                s.pop()
        xi, xo = self.t_to_x(self.t_in), self.t_to_x(self.t_out)
        dim = rgba(0, 0, 0, 0.62)
        s.append_color(dim, rect(0, 0, xi, h))
        s.append_color(dim, rect(xo, 0, w - xo, h))

        # passages retirés : rouge hachuré
        cuts = list(self.cuts) + ([sorted(self.pending_cut)] if self.pending_cut else [])
        for a, b in cuts:
            x0, x1 = self.t_to_x(a), self.t_to_x(b)
            s.push_clip(rect(x0, 0, x1 - x0, h))
            s.append_color(rgba(0, 0, 0, 0.55), rect(x0, 0, x1 - x0, h))
            stripes = Gsk.PathBuilder.new()
            for sx in range(int(x0) - h, int(x1) + h, 10):
                stripes.move_to(sx, h)
                stripes.line_to(sx + h, 0)
            s.append_stroke(stripes.to_path(), Gsk.Stroke.new(3), with_alpha(red, 0.55))
            s.pop()
            s.append_color(red, rect(x0, 0, max(x1 - x0, 1), 3))
            s.append_color(red, rect(x0, h - 3, max(x1 - x0, 1), 3))
            for xe in (x0, x1 - 4):   # bords saisissables
                s.append_color(red, rect(xe, 0, 4, h))
                s.append_color(rgba(1, 1, 1, 0.85), rect(xe + 1.5, h / 2 - 6, 1, 12))
            if x1 - x0 > 26:
                self._text(s, "✂", (x0 + x1) / 2 - 6, h / 2 - 9, rgba(1, 1, 1, 0.95))
        s.pop()

        # écrans (plusieurs écrans) : bande de couleur par écran + repères de bascule
        if self.angles:
            bh, yb = self.BAND_H, h - self.BAND_H - 3   # au-dessus du bord du cadre de sélection
            for i, (t, k) in enumerate(self.angles):
                x0 = self.t_to_x(t)
                x1 = self.t_to_x(self.angles[i + 1][0]) if i + 1 < len(self.angles) else w
                col = self.SCREEN_COLORS[k % len(self.SCREEN_COLORS)]
                s.append_color(rgba(*col, 0.95), rect(x0, yb, x1 - x0, bh))
                px = x0 + (14 if i == 0 else 8)   # pastille avec le n° d'écran (hors de la poignée de début)
                if x1 - px > 20:
                    s.push_rounded_clip(rounded(px, yb - 16, 15, 15, 4))
                    s.append_color(rgba(*col, 0.95), rect(px, yb - 16, 15, 15))
                    s.pop()
                    self._text(s, str(k + 1), px + 4, yb - 17, rgba(1, 1, 1, 1))
                if i:   # repère de bascule (saisissable)
                    s.append_color(rgba(0, 0, 0, 0.5), rect(x0 - 2, 0, 4, h))
                    s.append_color(rgba(1, 1, 1, 0.95), rect(x0 - 1, 0, 2, h))
                    s.push_rounded_clip(rounded(x0 - 5, yb - 3, 10, bh + 6, 3))
                    s.append_color(rgba(1, 1, 1, 1), rect(x0 - 5, yb - 3, 10, bh + 6))
                    s.pop()

        # cadre de sélection + poignées
        s.append_color(acc, rect(xi, 0, xo - xi, 3))
        s.append_color(acc, rect(xi, h - 3, xo - xi, 3))
        for x in (xi, xo - 10):
            s.push_rounded_clip(rounded(x, 0, 10, h, 3))
            s.append_color(acc, rect(x, 0, 10, h))
            s.pop()
            s.append_color(rgba(1, 1, 1, 0.9), rect(x + 4, h / 2 - 9, 2, 18))

        if self.hover_x is not None and self.mode is None:
            s.append_color(with_alpha(fg, 0.5), rect(self.hover_x - 0.5, 0, 1, h))

        # graduations
        step = next((st for st in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600) if w / (self.duration / st) >= 60), 600)
        dimfg = with_alpha(fg, 0.45)
        t = 0.0
        while t <= self.duration + 1e-6:
            x = self.t_to_x(t)
            s.append_color(dimfg, rect(x, h + 2, 1, 4))
            self._text(s, f"{int(t // 60)}:{int(t % 60):02d}", x + 3, h + 1, dimfg)
            t += step
        if self.hover_x is not None:
            self._text(s, fmt_time(self.x_to_t(self.hover_x)), self.hover_x + 4, h + 1, fg)
        if self.pending_start is not None:   # repère « début du passage à retirer »
            xs = self.t_to_x(self.pending_start)
            for y in range(0, int(h), 6):
                s.append_color(red, rect(xs - 1, y, 2, 3))
            s.append_color(red, rect(xs, 0, 10, 10))
            self._text(s, "✂", xs + 1, -3, rgba(1, 1, 1, 1))
        s.restore()


class PlayGlyph(Gtk.Widget):
    """Lecture : triangle équilatéral aux coins arrondis, centré exactement (cadre englobant).
    Pause : deux barres arrondies. Même dessin pour tous les boutons de lecture."""

    def __init__(self, size=30, color=None):
        super().__init__(can_target=False, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.size = size
        self.color = color
        self.paused_icon = False
        self.set_size_request(size, size)

    def set_playing(self, playing):
        if self.paused_icon != playing:
            self.paused_icon = playing
            self.queue_draw()

    def do_snapshot(self, s):
        w, h = self.get_width(), self.get_height()
        side = self.size * 0.82
        color = self.color or self.get_color()
        r = side * 0.09   # arrondi des coins
        if self.paused_icon:
            bw, gap = side * 0.28, side * 0.22
            x0 = (w - (2 * bw + gap)) / 2
            for x in (x0, x0 + bw + gap):
                s.push_rounded_clip(rounded(x, (h - side) / 2, bw, side, bw / 2.6))
                s.append_color(color, rect(x, (h - side) / 2, bw, side))
                s.pop()
            return
        th = side * 3 ** 0.5 / 2          # hauteur d'un triangle équilatéral de côté « side »
        x0 = (w - th) / 2                 # cadre englobant centré
        y0 = (h - side) / 2
        path = Gsk.PathBuilder.new()
        path.move_to(x0 + r, y0 + r * 1.2)
        path.line_to(x0 + th - r, h / 2)
        path.line_to(x0 + r, y0 + side - r * 1.2)
        path.close()
        p = path.to_path()
        stroke = Gsk.Stroke.new(r * 2)
        stroke.set_line_join(Gsk.LineJoin.ROUND)   # coins arrondis sans changer la taille
        s.append_fill(p, Gsk.FillRule.WINDING, color)
        s.append_stroke(p, stroke, color)


class Playhead(Gtk.Widget):
    """Poignée + trait de la tête de lecture, sur toute la hauteur (timeline + pistes).
    Les clics sont gérés par le canvas (bande du haut, ±8 px autour du trait)."""
    W = 18

    def __init__(self, timeline):
        # Toute la largeur de la timeline : le trait est placé au dessin, d'après la largeur réelle du moment.
        # Il suit donc toujours la timeline (même quand elle change de largeur), et la lecture ne coûte qu'un
        # redessin, jamais une mise en page.
        super().__init__(can_target=False, halign=Gtk.Align.FILL, valign=Gtk.Align.FILL)
        self.timeline = timeline

    def do_snapshot(self, s):
        tl = self.timeline
        w = self.get_width()
        cx = min(max(tl.canvas.t_to_x(tl.pos), self.W / 2), max(w - self.W / 2, self.W / 2))
        s.save()
        s.translate(point(int(cx - self.W / 2), 0))
        self._draw(s)
        s.restore()

    def _draw(self, s):
        h = self.get_height()
        ruler = (TimelineCanvas.STRIP_H + TimelineCanvas.KNOB_H, TimelineCanvas.RULER_H)
        acc = accent()
        shadow = rgba(0, 0, 0, 0.45)
        cx = self.W / 2
        # le trait saute la règle graduée (lisibilité des temps), puis continue sur les pistes
        for y0, y1 in ((12, ruler[0]), (ruler[0] + ruler[1], h)):
            if y1 > y0:
                s.append_color(shadow, rect(cx - 2.5, y0, 5, y1 - y0))
                s.append_color(rgba(1, 1, 1, 0.98), rect(cx - 1.5, y0, 3, y1 - y0))
        s.push_rounded_clip(rounded(0, 0, self.W, 16, 5))
        s.append_color(rgba(1, 1, 1, 0.98), rect(0, 0, self.W, 16))
        s.pop()
        s.push_rounded_clip(rounded(2, 2, self.W - 4, 12, 4))
        s.append_color(acc, rect(2, 2, self.W - 4, 12))
        s.pop()
        for dx in (-3, 0, 3):
            s.append_color(rgba(1, 1, 1, 0.9), rect(cx + dx - 0.5, 5, 1, 6))


class ScreenRow(Gtk.Widget):
    """Vue « pistes par écran » : la bande d'images d'un écran, placée sur la même échelle de temps que le clip.
    Les passages où le montage montre cet écran sont en pleine lumière, les autres assombris.
    Clic : regarder cet écran dans l'aperçu (à cet instant) ; clic droit : bascule du montage vers cet écran ici."""
    HEIGHT = 40

    def __init__(self, index, label, canvas, on_view, on_switch):
        super().__init__(hexpand=True, height_request=self.HEIGHT)
        self.index, self.label, self.canvas = index, label, canvas
        self.range = (0.0, canvas.duration)   # instants du clip couverts par cet écran
        self.strip, self.frames = None, 1
        self.hover = False
        click = Gtk.GestureClick()
        click.connect("released", lambda _g, n, x, _y: n == 1 and on_view(self.index, self.canvas.x_to_t(x)))
        self.add_controller(click)
        right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right.connect("pressed", lambda _g, _n, x, _y: on_switch(self.index, self.canvas.x_to_t(x)))
        self.add_controller(right)
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._set_hover(True))
        motion.connect("leave", lambda *_: self._set_hover(False))
        self.add_controller(motion)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self.set_tooltip_text(f"Écran {index + 1} — clic : le voir dans l'aperçu · "
                              f"clic droit : basculer le montage sur cet écran à partir d'ici")

    def _set_hover(self, on):
        self.hover = on
        self.queue_draw()

    def set_strip(self, texture, frames):
        self.strip, self.frames = texture, frames
        self.queue_draw()

    def do_snapshot(self, s):
        c = self.canvas
        w, h = self.get_width(), self.get_height()
        fg = self.get_color()
        col = TimelineCanvas.SCREEN_COLORS[self.index % len(TimelineCanvas.SCREEN_COLORS)]
        s.push_rounded_clip(rounded(0, 0, w, h, 8))
        s.append_color(with_alpha(fg, 0.05), rect(0, 0, w, h))
        lo, hi = c.t_to_x(self.range[0]), c.t_to_x(self.range[1])
        if self.strip and hi > lo:   # images de cet écran, sur la partie du clip qu'il couvre
            fw = h * self.strip.get_width() / self.frames / self.strip.get_height()
            cells = max(1, math.ceil((hi - lo) / fw))
            s.push_clip(rect(lo, 0, hi - lo, h))
            for i in range(cells):
                k = min(self.frames - 1, int(i * self.frames / cells))
                x0 = lo + i * fw
                s.push_clip(rect(x0, 0, fw, h))
                s.append_texture(self.strip, rect(x0 - k * fw, 0, self.frames * fw, h))
                s.pop()
            s.pop()
        dim = rgba(0, 0, 0, 0.66)
        angles = c.angles or [[0.0, 0]]
        for i, (t, k) in enumerate(angles):   # assombri là où le montage montre un autre écran
            x0 = c.t_to_x(t)
            x1 = c.t_to_x(angles[i + 1][0]) if i + 1 < len(angles) else w
            if k != self.index:
                s.append_color(dim, rect(x0, 0, x1 - x0, h))
            else:
                s.append_color(rgba(*col, 1), rect(x0, 0, x1 - x0, 3))
        for a, b in c.cuts:   # passages retirés et hors de l'extrait : encore plus sombres
            s.append_color(rgba(0, 0, 0, 0.5), rect(c.t_to_x(a), 0, c.t_to_x(b) - c.t_to_x(a), h))
        s.append_color(rgba(0, 0, 0, 0.5), rect(0, 0, c.t_to_x(c.t_in), h))
        s.append_color(rgba(0, 0, 0, 0.5), rect(c.t_to_x(c.t_out), 0, w - c.t_to_x(c.t_out), h))
        for t, _k in angles[1:]:
            x = c.t_to_x(t)
            s.append_color(rgba(1, 1, 1, 0.9), rect(x - 1, 0, 2, h))
        if self.hover:
            s.append_color(with_alpha(fg, 0.08), rect(0, 0, w, h))
        s.pop()
        # étiquette : pastille de couleur + nom de l'écran
        layout = self.create_pango_layout(f"{self.index + 1}  {self.label}")
        lw, lh = layout.get_pixel_size()
        s.push_rounded_clip(rounded(6, (h - lh - 4) / 2, lw + 12, lh + 4, 6))
        s.append_color(rgba(*col, 0.92), rect(6, (h - lh - 4) / 2, lw + 12, lh + 4))
        s.pop()
        s.save()
        s.translate(point(12, (h - lh) / 2))
        s.append_layout(layout, rgba(1, 1, 1, 1))
        s.restore()


class Timeline(Gtk.Overlay):
    """Canvas (mis en cache par GTK) + pistes du mode avancé en dessous, alignées sur la même échelle.
    La tête de lecture couvre toute la hauteur ; elle est déplacée par marge, donc la lecture ne force
    aucun redessin du canvas ni des pistes."""
    LANE_GAP = 4

    def __init__(self, duration, **callbacks):
        super().__init__(hexpand=True)
        self.canvas = TimelineCanvas(duration, **callbacks)
        self.column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.LANE_GAP)
        self.column.append(self.canvas)
        # Vue « pistes par écran » (plusieurs écrans) : une ligne par écran, sous la bande du haut
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.LANE_GAP, visible=False)
        self.column.append(self.rows)
        self.canvas.on_live = self.refresh_rows
        self.lanes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.LANE_GAP)
        # Seules les pistes défilent (la bande du haut reste en place) ; pas de barre ici : l'éditeur en
        # place une à droite, hors de la timeline, pour garder la même échelle de temps partout.
        self.lanes_scroll = Gtk.ScrolledWindow(child=self.lanes, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                               vscrollbar_policy=Gtk.PolicyType.EXTERNAL, visible=False)
        self.column.append(self.lanes_scroll)
        self.set_child(self.column)
        self.head = Playhead(self)
        self.add_overlay(self.head)
        self.pos = 0.0

    def add_lane(self, lane):
        self.lanes.append(lane)

    def add_screen_row(self, row):
        self.rows.append(row)

    def show_rows(self, on):
        self.rows.set_visible(on)

    def rows_height(self):
        """Hauteur ajoutée par la vue « pistes par écran » (0 si masquée)."""
        n = 0
        child = self.rows.get_first_child()
        while child:
            n += 1
            child = child.get_next_sibling()
        return n * (ScreenRow.HEIGHT + self.LANE_GAP) if self.rows.get_visible() and n else 0

    def refresh_rows(self):
        child = self.rows.get_first_child()
        while child:
            child.queue_draw()
            child = child.get_next_sibling()

    def set_angles(self, angles):
        self.canvas.set_angles(angles)
        self.refresh_rows()

    def show_lanes(self, on):
        self.lanes_scroll.set_visible(on)

    def set_pos(self, t):
        if t != self.pos:
            self.pos = t
            self.canvas.pos = t
            self.head.queue_draw()


# ---------------------------------------------------------------- piste audio

class Lane(Gtk.Widget):
    """Forme d'onde d'une piste ; en mode avancé, courbe de volume éditable (clic = point, glisser = déplacer,
    clic droit / double-clic = supprimer)."""
    PAD = 5
    R = 5

    def __init__(self, duration, color, on_points):
        super().__init__()
        self.duration = max(duration, 0.01)
        self.color = rgba(*color)
        self.peaks = None
        self.volume = 1.0
        self.muted = False
        self.t_in, self.t_out = 0, self.duration
        self.cuts = []
        self.points = []          # [[t, niveau]] niveau 0..2 (1 = 100 %)
        self.advanced = False
        self.on_points = on_points
        self.drag_idx = None
        self.hover_idx = None
        self.set_hexpand(True)
        self._size()

        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._pressed)
        self.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", lambda *_: self._set_hover(None))
        self.add_controller(motion)

    HEIGHT = 46   # hauteur par défaut (réglable : set_height)

    def _size(self):
        self.set_size_request(-1, getattr(self, "height", self.HEIGHT) if self.advanced else 34)

    def set_height(self, h):
        self.height = int(h)
        self._size()
        self.queue_draw()

    def set_advanced(self, on):
        self.advanced = on
        self._size()
        self.set_cursor(Gdk.Cursor.new_from_name("crosshair") if on else None)
        self.set_tooltip_text("Clic : ajouter un point · glisser : régler · clic droit : supprimer" if on else None)
        self.queue_draw()

    def set_peaks(self, peaks):
        self.peaks = peaks
        self.queue_draw()

    def set_state(self, volume, muted, t_in, t_out, cuts):
        self.volume, self.muted, self.t_in, self.t_out, self.cuts = volume, muted, t_in, t_out, cuts
        self.queue_draw()

    def set_points(self, pts):
        self.points = [list(p) for p in pts]
        self.queue_draw()

    # géométrie
    def _xy(self, t, v):
        w, h = self.get_width(), self.get_height()
        return t / self.duration * w, self.PAD + (1 - v / 2) * (h - 2 * self.PAD)

    def _tv(self, x, y):
        w, h = self.get_width(), self.get_height()
        t = min(max(x / max(w, 1), 0), 1) * self.duration
        v = min(max((1 - (y - self.PAD) / max(h - 2 * self.PAD, 1)) * 2, 0), 2)
        if abs(v - 1) < 0.04:
            v = 1.0    # aimant sur 100 %
        return t, v

    def _hit(self, x, y):
        for i, (t, v) in enumerate(self.points):
            px, py = self._xy(t, v)
            if (px - x) ** 2 + (py - y) ** 2 <= (self.R + 4) ** 2:
                return i
        return None

    # interactions
    def _motion(self, _c, x, y):
        if self.advanced:
            self._set_hover(self._hit(x, y))

    def _set_hover(self, i):
        if i != self.hover_idx:
            self.hover_idx = i
            self.queue_draw()

    def _pressed(self, gesture, n_press, x, y):
        if not self.advanced:
            return
        btn = gesture.get_current_button()
        i = self._hit(x, y)
        if i is not None and (btn == Gdk.BUTTON_SECONDARY or n_press == 2):
            del self.points[i]
            self.drag_idx = None
            self._changed()

    def _drag_begin(self, gesture, x, y):
        if not self.advanced or gesture.get_current_button() != Gdk.BUTTON_PRIMARY:
            self.drag_idx = None
            return
        i = self._hit(x, y)
        if i is None:
            t, v = self._tv(x, y)
            self.points.append([t, v])
            self.points.sort()
            i = self.points.index([t, v])
        self.drag_idx = i
        self.drag_origin = (x, y)
        self._changed()

    def _drag_update(self, _g, dx, dy):
        if self.drag_idx is None:
            return
        x0, y0 = self.drag_origin
        t, v = self._tv(x0 + dx, y0 + dy)
        lo = self.points[self.drag_idx - 1][0] + 0.01 if self.drag_idx > 0 else 0
        hi = self.points[self.drag_idx + 1][0] - 0.01 if self.drag_idx < len(self.points) - 1 else self.duration
        self.points[self.drag_idx] = [min(max(t, lo), hi), v]
        self._changed(live=True)

    def _drag_end(self, *_):
        if self.drag_idx is not None:
            self.drag_idx = None
            self._changed()

    def _changed(self, live=False):
        self.queue_draw()
        self.on_points([tuple(p) for p in self.points], live)

    # rendu
    def do_snapshot(self, s):
        w, h = self.get_width(), self.get_height()
        fg = self.get_color()
        s.append_color(with_alpha(fg, 0.04), rect(0, 0, w, h))
        mid = h / 2
        xi, xo = self.t_in / self.duration * w, self.t_out / self.duration * w

        if self.peaks:
            n = len(self.peaks)
            gain = 0 if self.muted else min(self.volume, 2.0) / 2.0 * 1.6
            if self.advanced:
                gain = 0 if self.muted else 0.9
            inside, outside = Gsk.PathBuilder.new(), Gsk.PathBuilder.new()
            step = 3
            for x in range(0, int(w), step):
                p = max(self.peaks[min(n - 1, int(x / w * n))] * gain * self._env_at(x / w * self.duration), 0.03)
                bh = min(p, 1.0) * (h - 2)
                t = x / w * self.duration
                kept = xi <= x <= xo and not any(a <= t <= b for a, b in self.cuts)
                (inside if kept else outside).add_rect(rect(x, mid - bh / 2, step - 1, bh))
            s.append_fill(outside.to_path(), Gsk.FillRule.WINDING, with_alpha(fg, 0.22))
            s.append_fill(inside.to_path(), Gsk.FillRule.WINDING,
                          with_alpha(fg, 0.25) if self.muted else with_alpha(self.color, 0.85))
        else:
            s.append_color(with_alpha(fg, 0.08), rect(0, mid - 1, w, 2))

        if not self.advanced:
            return
        # repère 100 %
        _, y100 = self._xy(0, 1)
        dash = Gsk.PathBuilder.new()
        for x in range(0, int(w), 8):
            dash.move_to(x, y100)
            dash.line_to(x + 4, y100)
        s.append_stroke(dash.to_path(), Gsk.Stroke.new(1), with_alpha(fg, 0.35))
        # courbe
        line = Gsk.PathBuilder.new()
        pts = self.points or [[0, 1.0]]
        x0, y0 = self._xy(0, pts[0][1])
        line.move_to(0, y0)
        for t, v in self.points:
            line.line_to(*self._xy(t, v))
        line.line_to(w, self._xy(0, pts[-1][1])[1])
        s.append_stroke(line.to_path(), Gsk.Stroke.new(2.5), rgba(1, 1, 1, 0.9) if not self.muted else with_alpha(fg, 0.4))
        dots = Gsk.PathBuilder.new()
        for i, (t, v) in enumerate(self.points):
            dots.add_circle(point(*self._xy(t, v)), self.R + (2 if i in (self.hover_idx, self.drag_idx) else 0))
        s.append_fill(dots.to_path(), Gsk.FillRule.WINDING, self.color)
        s.append_stroke(dots.to_path(), Gsk.Stroke.new(1.5), rgba(1, 1, 1, 0.95))
        if self.drag_idx is not None and self.drag_idx < len(self.points):
            t, v = self.points[self.drag_idx]
            px, py = self._xy(t, v)
            layout = self.create_pango_layout(f"{int(round(v * 100))} % · {fmt_time(t)}")
            lw, lh = layout.get_pixel_size()
            s.save()
            s.translate(point(min(max(px + 8, 0), w - lw), max(py - lh - 4, 0)))
            s.append_layout(layout, fg)
            s.restore()

    def _env_at(self, t):
        if not self.advanced or not self.points:
            return 1.0
        p = self.points
        if t <= p[0][0]:
            return p[0][1]
        for (t0, v0), (t1, v1) in zip(p, p[1:]):
            if t <= t1:
                return v0 + (v1 - v0) * (t - t0) / max(t1 - t0, 1e-6)
        return p[-1][1]


# ---------------------------------------------------------------- partage

class ShareTile(Gtk.Box):
    """Carte à glisser-déposer (Discord, navigateur, gestionnaire de fichiers…) avec copie dans le presse-papiers."""

    def __init__(self, title, toast, audio=False):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, css_classes=["card", "share-tile"])
        self.audio = audio
        self.path = None
        self.resolve = None   # fichier réellement envoyé (ex. version au son mixé) ; None = pas encore prêt
        self.toast = toast
        self.texture = None
        if audio:   # fichier son : une icône à la place de la miniature
            self.picture = None
            thumb = Gtk.Box(css_classes=["audio-thumb"], width_request=96, height_request=54, valign=Gtk.Align.CENTER)
            thumb.append(Gtk.Image(icon_name="audio-x-generic-symbolic", pixel_size=24, hexpand=True,
                                   halign=Gtk.Align.CENTER))
        else:
            self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, width_request=96, height_request=54,
                                       can_shrink=True, valign=Gtk.Align.CENTER)
            thumb = self.picture
        # Pastille « pas à jour » (l'édit a changé depuis cet export), en coin de la miniature
        frame = Gtk.Overlay(child=thumb, valign=Gtk.Align.CENTER)
        self.stale_badge = Gtk.Image(icon_name="dialog-warning-symbolic", pixel_size=14, css_classes=["stale-badge"],
                                     halign=Gtk.Align.END, valign=Gtk.Align.START, margin_top=3, margin_end=3,
                                     visible=False)
        frame.add_overlay(self.stale_badge)
        self.append(frame)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True, spacing=2)
        head = Gtk.Box(spacing=6)
        self.title = Gtk.Label(label=title, xalign=0, css_classes=["heading"])
        head.append(self.title)
        self.subtitle = Gtk.Label(xalign=0, css_classes=["caption", "dim-label"], wrap=True,
                                  max_width_chars=24, width_chars=24)
        text.append(head)
        text.append(self.subtitle)
        self.append(text)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copier le fichier (à coller avec Ctrl+V)",
                          valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
        copy.connect("clicked", self._copy)
        folder = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Afficher dans le dossier",
                            valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
        folder.connect("clicked", lambda *_: self.path and show_in_folder(self.path))
        self.buttons = (copy, folder)
        self.append(copy)
        self.append(folder)

        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        drag.connect("prepare", lambda *_: self._content())
        drag.connect("drag-begin", lambda src, _d: self.texture and src.set_icon(self.texture, 48, 27))
        self.add_controller(drag)
        self.set_file(None, "")

    def _content(self):
        if not self.path:
            return None
        path = self.resolve() if self.resolve else self.path
        if path is None:
            self.toast("Préparation du son pour le partage… réessaie dans un instant")
            return None
        return file_content_provider(path)

    def _copy(self, *_):
        content = self._content()
        if content:
            self.get_clipboard().set_content(content)
            self.toast("Fichier copié — colle-le avec Ctrl+V")

    def set_stale(self, stale):
        """Export plus à jour avec l'édit : pastille orange + contour, et info-bulle explicative."""
        self.stale_badge.set_visible(stale)
        (self.add_css_class if stale else self.remove_css_class)("stale")
        self.set_tooltip_text("Pas à jour : l'édit a changé depuis cet export — clique sur Exporter pour le refaire"
                              if stale else ("Glisser vers Discord, un navigateur, le gestionnaire de fichiers…"
                                             if self.path else None))

    def set_file(self, path, subtitle, texture=None, resolve=None):
        self.path = path if path and os.path.exists(path) else None
        self.resolve = resolve
        self.texture = None if self.audio else texture
        if self.picture:
            self.picture.set_paintable(texture)
        self.subtitle.set_label(subtitle)
        for b in self.buttons:
            b.set_sensitive(self.path is not None)
        if self.path:
            self.remove_css_class("disabled")
            self.set_cursor(Gdk.Cursor.new_from_name("grab"))
            self.set_tooltip_text("Glisser vers Discord, un navigateur, le gestionnaire de fichiers…")
        else:
            self.add_css_class("disabled")
            self.set_cursor(None)
            self.set_tooltip_text(None)
