#!/usr/bin/env python3
"""Runs gpu-screen-recorder in replay mode according to ~/.config/clips/config.json.

Started by clips-recorder.service; SIGUSR1 on the main process saves a clip.
The command channel (-ipc) is used for manual recording (record-clip start / stop), written to -ro.
With other screens (extra_monitors), one recorder per screen runs in parallel and this script relays
the signals.
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
    if extras:   # timestamp of the 1st frame: aligns the screens of a manual recording
        cmd += ["-write-first-frame-ts", "yes"]
    # Socket left over from a previous run: gpu-screen-recorder could not open it.
    try:
        os.remove(sock)
    except OSError:
        pass
    return cmd


# Main screen: the audio (each -a = a separate track, mixable later in the editor) and on_save.py,
# which renames the clip and attaches the other screens to it.
cmd = base_cmd(cfg["monitor"], cfg["clips_dir"], GSR_SOCKET)
cmd += ["-ac", "aac", "-sc", os.path.join(APP_DIR, "on_save.py")]
tracks = audio_tracks(cfg)
for t in tracks:
    cmd += ["-a", t["source"]]
# Remember the track order: on_save.py copies it next to each clip.
write_recording(tracks)
set_manual_recording(None)   # a manual recording does not survive a replay restart

if not extras:
    # exec: gpu-screen-recorder becomes the service's main process,
    # which lets "systemctl --user kill -s USR1" target it directly.
    os.execvp(cmd[0], cmd)

# Several screens: this script stays the main process and relays the save (USR1) to all the
# recorders. Each other screen (no audio) writes to .screens/incoming/<screen>/; on_save.py picks it up there.
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

# If one recorder stops, stop the others: systemd restarts the whole thing.
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
