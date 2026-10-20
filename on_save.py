#!/usr/bin/env python3
"""Appelé par gpu-screen-recorder après chaque sauvegarde : <chemin> <type>.

1. Tout de suite (rapide) : détecte le jeu, renomme le clip (« Black Mesa 2026-10-14 01-20-08.mp4 »),
   écrit le fichier annexe (pistes + jeu).
2. Dans un processus détaché : son de confirmation + notification (« Éditer » / « Ouvrir le dossier »).
Aucune fenêtre n'est ouverte ni activée : le jeu garde le focus.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clips_common import (APP_DIR, detect_game, layer_shell_available, load_config, play_sound,  # noqa: E402
                          extra_monitors, read_recording, safe_filename, save_clip_meta, screen_file,
                          screens_dir, set_clip_screens)


def _read_ts(path):
    """Fichier .ts de gpu-screen-recorder (horodatage de la 1re image) → microsecondes monotones, puis supprimé."""
    try:
        with open(path) as f:
            value = int(f.read().split()[2])
        os.remove(path)
        return value
    except (OSError, ValueError, IndexError):
        return None


def _duration(path):
    try:
        # durée de la vidéo (pas du fichier : les pistes audio peuvent dépasser de quelques ms)
        return float(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                     "stream=duration", "-of", "csv=p=0", path],
                                    capture_output=True, text=True, timeout=10).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def other_screens(cfg, path, kind):
    """Fichiers des autres écrans sauvegardés en même temps que le clip (dossiers d'arrivée), une fois complets.
    Retourne [(écran, chemin, décalage en s)]."""
    primary_ts = _read_ts(path + ".ts")
    found = []
    for mon in extra_monitors(cfg):
        cand, offset = _wait_screen(cfg, mon, path, kind, primary_ts)
        if cand:
            found.append((mon, cand, offset))
    return found


def _wait_screen(cfg, mon, path, kind, primary_ts):
    incoming = os.path.join(screens_dir(cfg["clips_dir"]), "incoming", mon)
    prefix = "Video_" if kind == "regular" else "Replay_"
    ref = os.path.getmtime(path)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            found = [e for e in os.scandir(incoming) if e.name.startswith(prefix) and e.name.endswith(".mp4")
                     and abs(e.stat().st_mtime - ref) < 20]
        except OSError:
            found = []
        if found:
            cand = max(found, key=lambda e: e.stat().st_mtime).path
            size = os.path.getsize(cand)
            time.sleep(0.4)
            if size and os.path.getsize(cand) == size:   # écriture terminée
                second_ts = _read_ts(cand + ".ts")
                if kind == "regular" and primary_ts and second_ts:
                    offset = (second_ts - primary_ts) / 1e6   # même horloge : décalage exact
                else:   # replay : tous sauvegardés au même instant, donc finissent ensemble
                    d1, d2 = _duration(path), _duration(cand)
                    offset = (d1 - d2) if d1 and d2 else 0.0
                return cand, offset
        time.sleep(0.3)
    return None, None


def attach_screens(cfg, final, found):
    screens = [{"monitor": cfg["monitor"]}]
    for mon, cand, offset in found:
        dest = screen_file(final, mon)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(cand, dest)
            screens.append({"monitor": mon, "offset": round(offset, 3)})
        except OSError:
            pass
    if len(screens) > 1:
        set_clip_screens(final, screens)


def clean_incoming(cfg):
    """Fichiers du 2e écran jamais rattachés (sauvegarde ratée) : effacés au bout d'une heure."""
    root = os.path.join(screens_dir(cfg["clips_dir"]), "incoming")
    limit = time.time() - 3600
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            try:
                if os.path.getmtime(p) < limit:
                    os.remove(p)
            except OSError:
                pass


def finalize(path, kind="replay"):
    """Renomme selon le jeu détecté, rattache le 2e écran et écrit les métadonnées.
    Retourne (chemin final, jeu)."""
    cfg = load_config()
    # Avant le renommage : la bibliothèque voit arriver le clip avec ses écrans déjà rattachés.
    screens = other_screens(cfg, path, kind)
    game, appid = detect_game() if cfg.get("detect_game", True) else (None, None)
    if game and os.path.exists(path):
        stamp = time.strftime("%Y-%m-%d %H-%M-%S", time.localtime(os.path.getmtime(path)))
        base = os.path.join(os.path.dirname(path), f"{safe_filename(game)} {stamp}")
        dest, n = base + os.path.splitext(path)[1], 2
        while os.path.exists(dest):
            dest, n = f"{base} ({n}){os.path.splitext(path)[1]}", n + 1
        try:
            # Métadonnées d'abord : la bibliothèque voit le clip renommé avec son jeu déjà connu.
            save_clip_meta(dest, read_recording(), game, appid)
            attach_screens(cfg, dest, screens)
            os.rename(path, dest)
            return dest, game
        except OSError:
            pass
    try:
        save_clip_meta(path, read_recording(), game, appid)
        attach_screens(cfg, path, screens)
    except OSError:
        pass
    clean_incoming(cfg)
    return path, game


def notify(path, game, kind="replay"):
    cfg = load_config()
    if cfg.get("save_sound", True):
        play_sound(cfg.get("sound", "success"), cfg.get("sound_volume", 70))
    popup = cfg.get("save_popup", True)
    if popup and layer_shell_available():
        subprocess.Popen([sys.executable, os.path.join(APP_DIR, "popup.py"), path, game or "", kind],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        popup = False   # affiché
    if not cfg.get("save_notification", True) and not popup:
        return
    size_mb = os.path.getsize(path) / 1e6 if os.path.exists(path) else 0
    title = "Enregistrement sauvegardé" if kind == "regular" else "Clip enregistré"
    title = f"{title} — {game}" if game else title
    # Icône de Clips + rattachement à l'appli (desktop-entry) ; disparaît seule au bout de 6 s.
    argv = ["notify-send", "-a", "Clips", "-i", "local.clips.Clips", "-h", "string:desktop-entry:local.clips.Clips",
            "-t", "6000", "-w", "-A", "edit=Éditer", "-A", "folder=Ouvrir le dossier"]
    res = subprocess.run(argv + [title, f"{os.path.basename(path)} — {size_mb:.0f} Mo"],
                         capture_output=True, text=True)
    action = res.stdout.strip()
    if action == "edit":
        subprocess.Popen([os.path.join(APP_DIR, "clips.py"), "--edit", path], start_new_session=True)
    elif action == "folder":
        subprocess.Popen(["xdg-open", os.path.dirname(path)], start_new_session=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(0)
    if os.environ.get("CLIPS_ON_SAVE_CHILD"):
        notify(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None,
               sys.argv[3] if len(sys.argv) > 3 else "replay")
    else:
        # gpu-screen-recorder : <chemin> <type> (« replay », ou « regular » pour un enregistrement manuel)
        kind = sys.argv[2] if len(sys.argv) > 2 else "replay"
        final, game = finalize(sys.argv[1], kind)
        env = {**os.environ, "CLIPS_ON_SAVE_CHILD": "1"}
        subprocess.Popen([sys.executable, os.path.abspath(__file__), final, game or "", kind],
                         env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
