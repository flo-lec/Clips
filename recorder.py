#!/usr/bin/env python3
"""Lance gpu-screen-recorder en mode replay selon ~/.config/clips/config.json.

Démarré par clips-recorder.service ; SIGUSR1 sur le processus principal sauvegarde un clip.
Le canal de commande (-ipc) sert à l'enregistrement manuel (record-clip start / stop), écrit dans -ro.
Avec d'autres écrans (extra_monitors), un enregistreur par écran tourne en parallèle et ce script relaie
les signaux.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clips_common import (APP_DIR, GSR_SOCKET, audio_tracks, extra_monitors, extra_socket,  # noqa: E402
                          load_config, screens_dir, set_manual_recording, write_recording)

cfg = load_config()
os.makedirs(cfg["clips_dir"], exist_ok=True)
extras = extra_monitors(cfg)


def base_cmd(monitor, out_dir, sock):
    cmd = [
        "gpu-screen-recorder",
        "-w", monitor,
        "-c", "mp4",
        "-f", str(cfg["fps"]),
        "-r", str(cfg["replay_seconds"]),
        "-replay-storage", "ram",
        "-k", cfg["codec"],
        "-q", cfg["quality"],
        "-cursor", "yes",
        "-fallback-cpu-encoding", "yes",
        "-o", out_dir,
        "-ro", out_dir,
        "-ipc", sock,
    ]
    if extras:   # horodatage de la 1re image : aligne les écrans d'un enregistrement manuel
        cmd += ["-write-first-frame-ts", "yes"]
    # Socket d'une exécution précédente : gpu-screen-recorder ne pourrait pas l'ouvrir.
    try:
        os.remove(sock)
    except OSError:
        pass
    return cmd


# Écran principal : le son (chaque -a = une piste séparée, mixable ensuite dans l'éditeur) et on_save.py,
# qui renomme le clip et y rattache les autres écrans.
cmd = base_cmd(cfg["monitor"], cfg["clips_dir"], GSR_SOCKET)
cmd += ["-ac", "aac", "-sc", os.path.join(APP_DIR, "on_save.py")]
tracks = audio_tracks(cfg)
for t in tracks:
    cmd += ["-a", t["source"]]
# Mémorise l'ordre des pistes : on_save.py le recopie à côté de chaque clip.
write_recording(tracks)
set_manual_recording(None)   # un enregistrement manuel ne survit pas à un redémarrage du replay

if not extras:
    # exec : gpu-screen-recorder devient le processus principal du service,
    # ce qui permet à « systemctl --user kill -s USR1 » de le viser directement.
    os.execvp(cmd[0], cmd)

# Plusieurs écrans : ce script reste le processus principal et relaie la sauvegarde (USR1) à tous les
# enregistreurs. Chaque autre écran (sans son) écrit dans .screens/incoming/<écran>/ ; on_save.py l'y reprend.
import signal  # noqa: E402
import subprocess  # noqa: E402

procs = [subprocess.Popen(cmd)]
for mon in extras:
    incoming = os.path.join(screens_dir(cfg["clips_dir"]), "incoming", mon)
    os.makedirs(incoming, exist_ok=True)
    procs.append(subprocess.Popen(base_cmd(mon, incoming, extra_socket(mon))))


def forward(sig, _frame):
    for p in procs:
        if p.poll() is None:
            p.send_signal(sig)


for sig in (signal.SIGUSR1, signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, forward)

# Si un enregistreur s'arrête, on arrête l'autre : systemd relance l'ensemble.
code = None
while code is None:
    try:
        pid, status = os.wait()
    except ChildProcessError:
        break
    code = os.waitstatus_to_exitcode(status)
forward(signal.SIGINT, None)
for p in procs:
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
sys.exit(code or 0)
