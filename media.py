"""Media: ffprobe probes, job queue, GStreamer playback and ffmpeg export (all GPU)."""
import hashlib
import json
import os
import shutil
import subprocess
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstController", "1.0")
from gi.repository import Gio, GLib, Gst, GstController  # noqa: E402

from clips_common import CACHE_DIR, render_node  # noqa: E402


# ---------------------------------------------------------------- processes

def run_async(argv, callback=None, want_stdout=True):
    """Runs a command without blocking the UI; callback(ok, stdout_bytes, stderr_text)."""
    flags = Gio.SubprocessFlags.STDERR_PIPE
    flags |= Gio.SubprocessFlags.STDOUT_PIPE if want_stdout else Gio.SubprocessFlags.STDOUT_SILENCE
    try:
        proc = Gio.Subprocess.new(argv, flags)
    except GLib.Error as e:
        if callback:
            callback(False, b"", e.message)
        return None

    def done(p, res):
        try:
            _, out, err = p.communicate_finish(res)
            ok = p.get_successful()
            out = out.get_data() if out else b""
            err = err.get_data().decode(errors="replace") if err else ""
        except GLib.Error as e:
            ok, out, err = False, b"", e.message
        if callback:
            callback(ok, out, err)

    proc.communicate_async(None, None, done)
    return proc


class JobQueue:
    """Limits the number of ffmpeg/ffprobe running in parallel (thumbnails, probes, waveforms)."""

    def __init__(self, max_parallel=2):
        self.max = max_parallel
        self.running = 0
        self.pending = []

    def add(self, argv, callback, front=False):
        job = (argv, callback)
        self.pending.insert(0, job) if front else self.pending.append(job)
        self._pump()

    def _pump(self):
        while self.running < self.max and self.pending:
            argv, cb = self.pending.pop(0)
            self.running += 1

            def done(ok, out, err, cb=cb):
                self.running -= 1
                cb(ok, out, err)
                self._pump()

            run_async(argv, done)


JOBS = JobQueue(max(2, min(4, (os.cpu_count() or 2) // 4)))


def cache_key(path, extra=""):
    st = os.stat(path)
    return hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}|{extra}".encode()).hexdigest()


# ---------------------------------------------------------------- several screens

_INFO = {}


def video_info(path):
    """(duration, width, height, fps) of a file's video — blocking call (~30 ms), cached."""
    try:
        key = cache_key(path)
    except OSError:
        return None
    if key not in _INFO:
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream=duration,width,height,avg_frame_rate", "-of", "json", path],
                                 capture_output=True, text=True, timeout=10).stdout
            v = json.loads(out)["streams"][0]
            num, _, den = v.get("avg_frame_rate", "60/1").partition("/")
            _INFO[key] = (float(v.get("duration", 0)), int(v["width"]), int(v["height"]),
                          round(float(num) / float(den or 1)) or 60)
        except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError, subprocess.SubprocessError):
            return None
    return _INFO[key]


def angle_at(angles, t):
    """Screen shown at time t according to the switches [[time, screen], …] (sorted)."""
    k = 0
    for at, screen in angles:
        if at <= t + 1e-6:
            k = screen
        else:
            break
    return k


def screen_range(screen):
    """Clip times covered by a screen: [start, end)."""
    info = video_info(screen["path"])
    dur = info[0] if info else 0.0
    return screen["offset"], screen["offset"] + dur


def video_plan(segments, angles, screens, only=None):
    """Pieces (start, end, screen) in clip time, for the kept sections.
    only=None: edit following the switches; only=k: everything on screen k.
    Where a screen has no frame (before its start, after its end), fall back to the main screen."""
    switches = [[0.0, only]] if only is not None else sorted(angles or [[0.0, 0]])
    ranges = {k: screen_range(sc) for k, sc in enumerate(screens) if k}
    plan = []
    joinable = [False]   # false right after a cut: two separate sections never merge

    def add(s, e, k):
        if e - s < 1e-3:
            return
        if joinable[0] and plan and plan[-1][2] == k and abs(plan[-1][1] - s) < 1e-6:
            plan[-1] = (plan[-1][0], e, k)
        else:
            plan.append((s, e, k))
        joinable[0] = True

    for a, b in segments:
        joinable[0] = False
        cuts = [a] + [t for t, _ in switches if a < t < b] + [b]
        for s, e in zip(cuts, cuts[1:]):
            k = angle_at(switches, s)
            if k and k in ranges:
                lo, hi = ranges[k]
                add(s, max(s, min(e, lo)), 0)
                add(max(s, lo), min(e, hi), k)
                add(max(s, min(e, hi)), e, 0)
            else:
                add(s, e, 0)
    return plan


# ---------------------------------------------------------------- sharing

SHARE_DIR = os.path.join(CACHE_DIR, "share")


class ShareCache:
    """"Shareable" version of a clip with several audio tracks: video copied as is (instant), tracks
    mixed into one. Discord, browsers and most players only play the 1st track of a file:
    without this mix, the mic, the voice chat… disappear. Prepared in the background (editor opening, hovering
    a card); the file keeps the clip's name."""
    SYNC_MAX_S = 90   # longer clip not ready yet: don't block the drag, warn instead

    def __init__(self):
        self.waiting = {}   # file being prepared -> callbacks

    @staticmethod
    def needed(gains, audio=False):
        # Video: only if there are several tracks to mix. Audio: always (the audio is extracted as MP3).
        return bool(gains) if audio else len(gains) > 1

    def target(self, src, gains, audio=False):
        key = cache_key(src, json.dumps([round(g, 3) for g in gains]) + ("|mp3" if audio else ""))[:16]
        name = os.path.splitext(os.path.basename(src))[0] + ".mp3" if audio else os.path.basename(src)
        return os.path.join(SHARE_DIR, key, name)

    def _argv(self, src, gains, out, audio=False):
        n = len(gains)
        graph = ";".join(f"[0:a:{i}]volume={g:.3f}[t{i}]" for i, g in enumerate(gains))
        graph += ";" + "".join(f"[t{i}]" for i in range(n)) + f"amix=inputs={n}:normalize=0:dropout_transition=0[a]"
        # In the background, without disturbing playback or the game: low priority, 2 threads.
        a = ["nice", "-n", "15", "ffmpeg", "-v", "error", "-nostdin", "-y", "-threads", "2", "-i", src,
             "-filter_complex", graph]
        if audio:   # audio only: MP3 (plays everywhere, and directly playable in Discord)
            return a + ["-map", "[a]", "-c:a", "libmp3lame", "-b:a", "192k", out]
        # "fast": 2× less computation than the default AAC coder, no audible difference at 192k.
        return a + ["-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-aac_coder", "fast",
                    "-b:a", "192k", "-movflags", "+faststart", out]

    def ready(self, src, gains, audio=False):
        """Path to share if available right away (the clip itself if it has only one track), otherwise None."""
        if not self.needed(gains, audio):
            return src
        try:
            out = self.target(src, gains, audio)
        except OSError:
            return src
        return out if os.path.exists(out) else None

    def prepare(self, src, gains, callback=None, audio=False):
        """Prepares in the background; callback(path) — the original clip if the mix fails."""
        path = self.ready(src, gains, audio)
        if path:
            if callback:
                callback(path)
            return
        out = self.target(src, gains, audio)
        if out in self.waiting:
            if callback:
                self.waiting[out].append(callback)
            return
        self.waiting[out] = [callback] if callback else []
        os.makedirs(os.path.dirname(out), exist_ok=True)
        part = os.path.splitext(out)[0] + ".part" + os.path.splitext(out)[1]

        def done(ok, _out, _err):
            if ok:
                os.replace(part, out)
            for cb in self.waiting.pop(out, []):
                cb(out if ok else src)

        JOBS.add(self._argv(src, gains, part, audio), done)

    def get_sync(self, src, gains, duration, audio=False):
        """For a drag-and-drop: the file to hand over, preparing it on the spot if the clip is short.
        None = long clip not ready yet (preparation started)."""
        path = self.ready(src, gains, audio)
        if path:
            return path
        out = self.target(src, gains, audio)
        if duration > self.SYNC_MAX_S or out in self.waiting:
            self.prepare(src, gains, audio=audio)
            return None
        os.makedirs(os.path.dirname(out), exist_ok=True)
        part = os.path.splitext(out)[0] + ".part" + os.path.splitext(out)[1]
        try:
            subprocess.run(self._argv(src, gains, part, audio), check=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.replace(part, out)
            return out
        except (OSError, subprocess.SubprocessError):
            return src

    @staticmethod
    def prune(max_age_days=3):
        """Share copies are only useful at sending time: delete them after a few days."""
        limit = time.time() - max_age_days * 86400
        try:
            entries = list(os.scandir(SHARE_DIR))
        except OSError:
            return
        for e in entries:
            try:
                if e.is_dir() and e.stat().st_mtime < limit:
                    shutil.rmtree(e.path, ignore_errors=True)
            except OSError:
                pass


SHARE = ShareCache()


META_DIR = os.path.join(CACHE_DIR, "meta")


def probe(path, callback):
    """A clip's info (duration, size, tracks…). Kept in a disk cache: at startup, an already-seen
    library runs no ffprobe."""
    try:
        cached = os.path.join(META_DIR, cache_key(path, "probe1") + ".json")
        with open(cached) as f:
            meta = json.load(f)
        GLib.idle_add(lambda: (callback(meta), False)[1])
        return
    except (OSError, ValueError):
        cached = None if not os.path.exists(path) else os.path.join(META_DIR, cache_key(path, "probe1") + ".json")
    argv = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
            "-show_entries", "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate", path]

    def done(ok, out, _err):
        meta = None
        if ok:
            try:
                data = json.loads(out)
                streams = data.get("streams", [])
                v = next((s for s in streams if s.get("codec_type") == "video"), {})
                num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").partition("/")
                fps = float(num) / float(den) if float(den or 0) else 0
                meta = {
                    "duration": float(data.get("format", {}).get("duration", 0) or 0),
                    "width": v.get("width", 0),
                    "height": v.get("height", 0),
                    "fps": round(fps) if fps else 60,
                    "vcodec": v.get("codec_name", "?"),
                    "audio_tracks": sum(1 for s in streams if s.get("codec_type") == "audio"),
                }
            except (ValueError, ZeroDivisionError):
                meta = None
        if meta and meta["duration"] > 0 and cached:
            try:
                os.makedirs(META_DIR, exist_ok=True)
                with open(cached, "w") as f:
                    json.dump(meta, f)
            except OSError:
                pass
        callback(meta)

    JOBS.add(argv, done, front=True)


# ---------------------------------------------------------------- encoders

ENCODERS = {
    "h264": [("vaapi", "h264_vaapi"), ("nvenc", "h264_nvenc"), ("qsv", "h264_qsv"), ("sw", "libx264")],
    "hevc": [("vaapi", "hevc_vaapi"), ("nvenc", "hevc_nvenc"), ("qsv", "hevc_qsv"), ("sw", "libx265")],
    "av1": [("vaapi", "av1_vaapi"), ("nvenc", "av1_nvenc"), ("qsv", "av1_qsv"), ("sw", "libsvtav1")],
}
BACKEND_LABELS = {"vaapi": "GPU (VA-API)", "nvenc": "NVIDIA GPU", "qsv": "Intel GPU (QSV)", "sw": "CPU"}


class EncoderDetector:
    """Tests once which encoder actually works on this machine, then times the graphics card
    against the processor on a short animated 1440p60 sample: the fastest is used for exports ("Automatic"
    setting). Depending on the machine — and the replay recorders already using the card's
    encoder —, the processor often wins. Results cached."""
    CACHE = os.path.join(CACHE_DIR, "encoders.json")
    SAMPLE = os.path.join(CACHE_DIR, "bench-1440p60.mp4")

    def __init__(self):
        self.best = {}      # encoder that works (graphics card if possible)
        self.fastest = {}   # the faster of the two, measured
        self.timings = {}   # seconds for the sample: {codec: {"gpu": t, "cpu": t}}
        self.signature = self._signature()
        try:
            with open(self.CACHE) as f:
                data = json.load(f)
            if data.get("signature") == self.signature:
                self.best = {k: tuple(v) for k, v in data["best"].items()}
                self.fastest = {k: tuple(v) for k, v in data.get("fastest", {}).items()}
                self.timings = data.get("timings", {})
        except (OSError, ValueError, KeyError):
            pass

    @staticmethod
    def _signature():
        ff = shutil.which("ffmpeg") or ""
        vendors = []
        for card in sorted(os.listdir("/sys/class/drm")) if os.path.isdir("/sys/class/drm") else []:
            try:
                with open(f"/sys/class/drm/{card}/device/vendor") as f:
                    vendors.append(f.read().strip())
            except OSError:
                pass
        return f"{ff}|{os.path.getmtime(ff) if ff else 0}|{render_node()}|{','.join(sorted(set(vendors)))}"

    @property
    def ready(self):
        return len(self.best) == len(ENCODERS) and len(self.fastest) == len(ENCODERS)

    def get(self, codec):
        """Encoder for an export, according to the "export_encoder" preference: auto (fastest measured),
        gpu (graphics card if it works) or cpu (processor)."""
        from clips_common import load_config
        pref = load_config().get("export_encoder", "auto")
        sw = ENCODERS[codec][-1]
        if pref == "cpu":
            return sw
        if pref == "gpu":
            return self.best.get(codec) or sw
        return self.fastest.get(codec) or self.best.get(codec) or sw

    def detect(self, done=None):
        if self.ready:
            if done:
                done()
            return
        queue = [(codec, cands) for codec, cands in ENCODERS.items() if codec not in self.best]

        def save():
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(self.CACHE, "w") as f:
                json.dump({"signature": self.signature, "best": self.best, "fastest": self.fastest,
                           "timings": self.timings}, f)

        def next_codec():
            if not queue:
                self._race(lambda: (save(), done and done()))
                return
            codec, cands = queue.pop(0)
            try_candidate(codec, list(cands))

        def try_candidate(codec, cands):
            if not cands:
                next_codec()
                return
            kind, enc = cands.pop(0)

            def res(ok, _o, _e):
                if ok:
                    self.best[codec] = (kind, enc)
                    next_codec()
                else:
                    try_candidate(codec, cands)

            run_async(self._test_argv(kind, enc), res, want_stdout=False)

        next_codec()

    def _race(self, done):
        """Graphics card against processor, codec by codec, on the same sample (decoding included)."""
        todo = [c for c in ENCODERS if c not in self.fastest]
        if not todo:
            done()
            return
        user_done = done

        def done():   # the sample (~100 MB) is only used for this measurement
            try:
                os.remove(self.SAMPLE)
            except OSError:
                pass
            user_done()

        def with_sample(go):
            if os.path.exists(self.SAMPLE):
                go()
                return
            os.makedirs(CACHE_DIR, exist_ok=True)
            run_async(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                       "testsrc2=s=2560x1440:r=60,noise=alls=10:allf=t", "-t", "2", "-c:v", "libx264", "-preset",
                       "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p", self.SAMPLE],
                      lambda ok, _o, _e: go() if ok else (self.fastest.update({c: self.best[c] for c in todo}),
                                                           done()), want_stdout=False)

        def next_codec():
            if not todo:
                done()
                return
            codec = todo.pop(0)
            gpu, cpu = self.best[codec], ENCODERS[codec][-1]
            if gpu[0] == "sw":   # no usable graphics card: nothing to compare
                self.fastest[codec] = cpu
                next_codec()
                return
            times = {}

            def timed(which, enc, then):
                t0 = time.monotonic()
                run_async(self._bench_argv(enc), lambda ok, _o, _e: (times.__setitem__(
                    which, time.monotonic() - t0 if ok else float("inf")), then()), want_stdout=False)

            def decide():
                self.timings[codec] = {k: round(v, 2) for k, v in times.items() if v != float("inf")}
                # the processor only if it's clearly faster (it runs hotter and takes over the whole machine)
                self.fastest[codec] = cpu if times["cpu"] < times["gpu"] * 0.8 else gpu
                next_codec()

            timed("gpu", gpu, lambda: timed("cpu", cpu, decide))

        with_sample(next_codec)

    def _bench_argv(self, enc):
        kind, name = enc
        base = ["ffmpeg", "-v", "error", "-nostdin"]
        node = render_node()
        if kind == "vaapi":
            return base + ["-init_hw_device", f"vaapi=va:{node}", "-filter_hw_device", "va", "-hwaccel", "vaapi",
                           "-hwaccel_device", "va", "-hwaccel_output_format", "vaapi", "-i", self.SAMPLE,
                           "-vf", "scale_vaapi=format=nv12", "-c:v", name, "-rc_mode", "CQP", "-qp", "22",
                           "-f", "null", "-"]
        if kind == "nvenc":
            return base + ["-hwaccel", "cuda", "-i", self.SAMPLE, "-c:v", name, "-preset", "p5", "-f", "null", "-"]
        if kind == "qsv":
            return base + ["-init_hw_device", "qsv=hw", "-i", self.SAMPLE, "-vf", "format=nv12", "-c:v", name,
                           "-f", "null", "-"]
        preset = ["-preset", "8"] if name == "libsvtav1" else ["-preset", "veryfast"]
        return base + ["-i", self.SAMPLE, "-c:v", name] + preset + ["-crf", "24", "-f", "null", "-"]

    @staticmethod
    def _test_argv(kind, enc):
        src = ["-f", "lavfi", "-i", "color=black:s=1280x720:r=30:d=0.1"]
        base = ["ffmpeg", "-v", "error", "-nostdin"]
        node = render_node()
        if kind == "vaapi":
            if not node:
                return ["false"]
            return base + ["-init_hw_device", f"vaapi=va:{node}", "-filter_hw_device", "va"] + src + \
                ["-vf", "format=nv12,hwupload", "-c:v", enc, "-f", "null", "-"]
        if kind == "qsv":
            return base + ["-init_hw_device", "qsv=hw"] + src + ["-vf", "format=nv12", "-c:v", enc, "-f", "null", "-"]
        return base + src + ["-pix_fmt", "yuv420p", "-c:v", enc, "-f", "null", "-"]


ENCODER_DETECTOR = EncoderDetector()


# ---------------------------------------------------------------- playback

def _has_va_decoder(codec):
    name = {"h264": "vah264dec", "hevc": "vah265dec", "av1": "vaav1dec", "vp9": "vavp9dec"}.get(codec)
    return bool(name and Gst.ElementFactory.find(name))


class Player:
    """GStreamer playback.

    - Video: GPU decoding and zero-copy display (DMABuf) when VA-API is available, otherwise via OpenGL.
    - Audio: one branch per track with its own volume (+ animated volume curve), mixed live.
    """

    def __init__(self, path, n_audio, vcodec="", on_eos=None, on_error=None, headless=None):
        if headless is None:   # automated tests: no picture, no sound
            headless = os.environ.get("CLIPS_HEADLESS") == "1"
        self.on_eos = on_eos
        self.on_error = on_error
        self.volumes = {}
        self.controls = {}
        self.wanted = {}
        self.duration = 0
        self.playing = False   # wanted state (GStreamer's get_state is asynchronous)
        self.segments = None   # sections to play in a row (continuous playback over the cuts)
        self.seg_idx = 0
        self.loop = True
        self.at_end = False
        self.async_pending = False   # a flushing seek is in progress (pipeline not ready yet)
        self.pending_done = False    # end of segment received, move to the next one as soon as possible
        self.pipeline = Gst.Pipeline.new("clips-player")

        src = Gst.ElementFactory.make("filesrc")
        src.props.location = path
        demux = Gst.ElementFactory.make("matroskademux" if path.lower().endswith(".mkv") else "qtdemux")
        self.pipeline.add(src)
        self.pipeline.add(demux)
        src.link(demux)

        if headless:   # tests: no window and no sound
            self.sink = Gst.ElementFactory.make("fakesink")
            self.sink.props.sync = True
            self.paintable = None
            self.video_sink = self.sink
        else:
            self.sink = Gst.ElementFactory.make("gtk4paintablesink")
            self.paintable = self.sink.props.paintable
        if headless:
            pass
        elif _has_va_decoder(vcodec):
            # Zero copy: the VA decoder delivers DMABufs that GTK displays directly.
            self.video_sink = self.sink
        elif self.paintable.props.gl_context:
            self.video_sink = Gst.ElementFactory.make("glsinkbin")
            self.video_sink.props.sink = self.sink
        else:
            self.video_sink = Gst.Bin.new("video-sink")
            conv = Gst.ElementFactory.make("videoconvert")
            self.video_sink.add(conv)
            self.video_sink.add(self.sink)
            conv.link(self.sink)
            self.video_sink.add_pad(Gst.GhostPad.new("sink", conv.get_static_pad("sink")))
        self.pipeline.add(self.video_sink)
        self.video_linked = False

        # One audio output per track (PipeWire does the mixing): no "audiomixer", which drops audio
        # when chaining the kept sections without flushing the pipeline.
        self.headless = headless
        self.has_audio = bool(n_audio)
        self.audio_sinks = []

        demux.connect("pad-added", self._on_demux_pad)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        self.bus_handler = bus.connect("message", self._on_message)
        self.pipeline.set_state(Gst.State.PAUSED)

    def _on_demux_pad(self, _demux, pad):
        name = pad.get_name()
        if name.startswith("video_") and not self.video_linked:
            self.video_linked = True
            self._branch(pad, self.video_sink.get_static_pad("sink"))
        elif name.startswith("audio_") and self.has_audio:
            idx = int(name.split("_")[1])
            conv = Gst.ElementFactory.make("audioconvert")
            res = Gst.ElementFactory.make("audioresample")
            vol = Gst.ElementFactory.make("volume")
            sink = Gst.ElementFactory.make("fakesink" if self.headless else "autoaudiosink")
            if self.headless:
                sink.props.sync = True
            self.volumes[idx] = vol
            self.audio_sinks.append(sink)
            self.wanted.setdefault(idx, (1.0, False, []))   # monitor volume applied even without a setting
            self._apply(idx)
            for e in (conv, res, vol, sink):
                self.pipeline.add(e)
            conv.link(res)
            res.link(vol)
            vol.link(sink)
            for e in (conv, res, vol, sink):
                e.sync_state_with_parent()
            self._branch(pad, conv.get_static_pad("sink"))

    def _branch(self, demux_pad, target_pad):
        q = Gst.ElementFactory.make("queue")
        dec = Gst.ElementFactory.make("decodebin")
        self.pipeline.add(q)
        self.pipeline.add(dec)
        demux_pad.link(q.get_static_pad("sink"))
        q.link(dec)
        dec.connect("pad-added", lambda _d, p: p.link(target_pad))
        q.sync_state_with_parent()
        dec.sync_state_with_parent()

    def _on_message(self, _bus, msg):
        if msg.type == Gst.MessageType.SEGMENT_DONE:
            self.pending_done = True
            self._maybe_next()
        elif msg.type == Gst.MessageType.ASYNC_DONE:
            self.async_pending = False
            self._maybe_next()
        elif msg.type == Gst.MessageType.STATE_CHANGED and msg.src == self.pipeline:
            if msg.parse_state_changed()[1] == Gst.State.PLAYING:
                self._maybe_next()
        elif msg.type == Gst.MessageType.EOS and self.on_eos:
            self.on_eos()
        elif msg.type == Gst.MessageType.ERROR and self.on_error:
            err, _dbg = msg.parse_error()
            self.on_error(err.message)

    monitor = 1.0   # monitor volume (preview only): multiplies each track, never goes into the export

    def set_monitor(self, volume):
        self.monitor = max(0.0, float(volume))
        for idx in list(self.volumes):
            self.wanted.setdefault(idx, (1.0, False, []))
            self._apply(idx)

    def set_track(self, idx, gain, mute, points=None):
        """gain: overall volume; points: [(t, level)] for a volume curve (level 1.0 = 100 %)."""
        self.wanted[idx] = (gain, mute, list(points or []))
        if idx in self.volumes:
            self._apply(idx)

    def _apply(self, idx):
        gain, mute, points = self.wanted[idx]
        gain *= self.monitor
        vol = self.volumes[idx]
        vol.props.mute = mute
        ctl = self.controls.get(idx)
        if points:
            if not ctl:
                cs = GstController.InterpolationControlSource()
                cs.props.mode = GstController.InterpolationMode.LINEAR
                binding = GstController.DirectControlBinding.new_absolute(vol, "volume", cs)
                vol.add_control_binding(binding)
                ctl = self.controls[idx] = (cs, binding)
            cs = ctl[0]
            cs.unset_all()
            # Values held before the first and after the last point.
            pts = [(0.0, points[0][1])] + points + [(points[-1][0] + 36000, points[-1][1])]
            for t, v in pts:
                cs.set(int(max(t, 0) * Gst.SECOND), gain * v)
        else:
            if ctl:
                vol.remove_control_binding(ctl[1])
                del self.controls[idx]
            vol.props.volume = gain

    def play(self):
        self.playing = True
        self.pipeline.set_state(Gst.State.PLAYING)

    def pause(self):
        self.playing = False
        self.pipeline.set_state(Gst.State.PAUSED)

    def position(self):
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        return pos / Gst.SECOND if ok else 0.0

    def seek(self, t, accurate=True):
        """Plain seek (pause, dragging the playhead): leaves segment mode."""
        self.segments = None
        self.at_end = False
        self.pending_done = False
        flags = Gst.SeekFlags.FLUSH | (Gst.SeekFlags.ACCURATE if accurate
                                       else Gst.SeekFlags.KEY_UNIT | Gst.SeekFlags.SNAP_NEAREST)
        self.pipeline.seek_simple(Gst.Format.TIME, flags, int(max(0, t) * Gst.SECOND))

    def play_segments(self, segments, start, loop):
        """Plays the kept sections in a row, without stopping: each end of segment (SEGMENT_DONE)
        chains to the next one with a non-blocking seek (without flushing the pipeline)."""
        self.segments, self.loop, self.at_end = list(segments), loop, False
        idx = next((i for i, (a, b) in enumerate(self.segments) if start < b - 0.03), None)
        if idx is None:
            idx, start = 0, self.segments[0][0]
        self.seg_idx = idx
        a, b = self.segments[idx]
        self._seek_segment(max(start, a), b, flush=True)

    def _seek_segment(self, a, b, flush):
        if flush:
            self.async_pending = True
            self.pending_done = False
        flags = Gst.SeekFlags.SEGMENT | Gst.SeekFlags.ACCURATE | (Gst.SeekFlags.FLUSH if flush else Gst.SeekFlags.NONE)
        self.pipeline.seek(1.0, Gst.Format.TIME, flags, Gst.SeekType.SET, int(a * Gst.SECOND),
                           Gst.SeekType.SET, int(b * Gst.SECOND))

    def _maybe_next(self):
        """A short segment can be read even before playback starts: chaining while the pipeline is
        starting up blocks it. So wait until it is ready AND playing."""
        if (self.pending_done and not self.async_pending
                and self.pipeline.get_state(0)[1] == Gst.State.PLAYING):
            self.pending_done = False
            self._next_segment()

    def _next_segment(self):
        if not self.segments:
            return
        # The message arrives when the segment has been read (not yet played): the queue continues seamlessly.
        self.seg_idx += 1
        if self.seg_idx >= len(self.segments):
            if not self.loop:
                self.at_end = True
                return
            self.seg_idx = 0
        a, b = self.segments[self.seg_idx]
        self._seek_segment(a, b, flush=False)

    def end_time(self):
        return self.segments[-1][1] if self.segments else None

    def destroy(self):
        bus = self.pipeline.get_bus()
        bus.disconnect(self.bus_handler)
        bus.remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)


# ---------------------------------------------------------------- export

def envelope_expr(points, gain, offset):
    """ffmpeg expression (function of t) for a piecewise-linear volume curve.

    t is the output time after "-ss offset"; the points are in the clip's original time.
    """
    T = f"(t+{offset:.4f})"
    if not points:
        return f"{gain:.4f}"
    pts = sorted(points)
    expr = f"{pts[-1][1]:.4f}"
    for (t0, v0), (t1, v1) in reversed(list(zip(pts, pts[1:]))):
        span = max(t1 - t0, 1e-3)
        seg = f"{v0:.4f}+({v1 - v0:.4f})*({T}-{t0:.4f})/{span:.4f}"
        expr = f"if(lt({T},{t1:.4f}),{seg},{expr})"
    expr = f"if(lt({T},{pts[0][0]:.4f}),{pts[0][1]:.4f},{expr})"
    return f"{gain:.4f}*({expr})"


class Exporter:
    """ffmpeg export: removed sections, volume curves, scaling, GPU encoding (automatic CPU fallback).

    segments: [(start, end)] to keep, in original time, sorted.
    tracks  : [{"gain", "muted", "points"}] per audio track of the file.
    """
    force_sw = False

    TARGET_LUFS = -14.0   # usual YouTube / social media level
    TRUE_PEAK = -1.0      # anti-clipping headroom (dBTP)
    LIMITER_DB = 6.0      # maximum gain handed to the limiter

    def __init__(self, src, out, segments, height, fps, codec, target_mb, qp, tracks, copy,
                 on_progress, on_done, normalize=False, audio_only=False, screens=None, plan=None):
        self.src, self.out = src, out
        self.segments = segments
        self.dur = sum(b - a for a, b in segments)
        self.height, self.fps, self.codec = height, fps, codec
        self.target_mb, self.qp = target_mb, qp
        self.tracks, self.copy = tracks, copy
        self.audio_only = audio_only   # MP3 of the mix (gains, curves, cuts, normalization), no video
        # Several screens: pieces (start, end, screen) and the screens' files ({"path", "offset"}).
        self.screens, self.plan = screens or [], plan
        self.multi_screen = bool(plan) and any(k for _s, _e, k in plan)
        self.on_progress, self.on_done = on_progress, on_done
        self.proc = None
        self.cancelled = False
        self.attempt = 0
        self.bitrate_scale = 1.0
        self.backend = None
        self.normalize = normalize and not copy
        self.norm_gain = None   # dB, computed by an audio analysis pass (fast, no video)
        self.limit = False

    def start(self):
        if self.normalize and self.norm_gain is None and self._active():
            self._analyze()
            return
        self._encode()

    def _analyze(self):
        """Pass 1: measure the mix (loudnorm) → fixed gain = target − level, capped by the true peak.
        A fixed gain doesn't "pump", unlike dynamic normalization."""
        A, B = self.segments[0][0], self.segments[-1][1]
        graph, label = self._audio_graph(A, video=False)
        argv = ["ffmpeg", "-v", "info", "-nostdin", "-hide_banner", "-ss", f"{A:.3f}", "-t", f"{B - A:.3f}",
                "-i", self.src, "-filter_complex",
                ";".join(graph) + f";{label}loudnorm=I={self.TARGET_LUFS}:TP={self.TRUE_PEAK}:print_format=json[an]",
                "-map", "[an]", "-f", "null", "-"]

        def done(ok, _out, err):
            if self.cancelled:
                self.on_done(False, None)
                return
            self.norm_gain = 0.0
            try:
                js = json.loads(err[err.rindex("{"):err.rindex("}") + 1])
                i, tp = float(js["input_i"]), float(js["input_tp"])
                if i > -70:
                    # Up to 6 dB beyond the peak: the limiter then only clips isolated peaks.
                    self.norm_gain = max(min(self.TARGET_LUFS - i, self.TRUE_PEAK - tp + self.LIMITER_DB, 30.0), -20.0)
                    self.limit = self.norm_gain > self.TRUE_PEAK - tp
            except (ValueError, KeyError):
                pass
            self._encode()

        self.proc = run_async(argv, done, want_stdout=False)

    def _encode(self):
        if self.audio_only:
            self.backend = ("audio", None)
        elif self.copy and not self.multi_screen:
            self.backend = ("copy", None)
        elif Exporter.force_sw:
            self.backend = ENCODERS[self.codec][-1]
        else:
            self.backend = ENCODER_DETECTOR.get(self.codec)
        try:
            self.proc = Gio.Subprocess.new(self._argv(), Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as e:
            self.on_done(False, e.message)
            return
        stream = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line, stream)
        self.proc.wait_async(None, self._on_exit)

    def cancel(self):
        self.cancelled = True
        if self.proc:
            self.proc.force_exit()

    def _on_line(self, stream, res, _):
        try:
            line, _len = stream.read_line_finish_utf8(res)
        except GLib.Error:
            return
        if line is None:
            return
        if line.startswith("out_time_us="):
            try:
                self.on_progress(min(max(int(line.split("=", 1)[1]) / 1e6 / self.dur, 0), 1))
            except ValueError:
                pass
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line, stream)

    def _on_exit(self, proc, res):
        try:
            proc.wait_finish(res)
        except GLib.Error:
            pass
        if self.cancelled:
            self._cleanup()
            self.on_done(False, None)
            return
        if not proc.get_successful():
            err = proc.get_stderr_pipe().read_bytes(65536, None).get_data().decode(errors="replace").strip()
            if self.backend[0] not in ("sw", "copy", "audio"):
                Exporter.force_sw = True   # processor fallback for this session
                self._encode()
                return
            self._cleanup()
            self.on_done(False, err.splitlines()[-1] if err else "ffmpeg failed")
            return
        size = os.path.getsize(self.out)
        if self.target_mb and size > self.target_mb * 1e6 and self.attempt < 2:
            # The VBR bitrate of GPU encoders isn't exact: try again a bit lower.
            self.attempt += 1
            self.bitrate_scale *= (self.target_mb * 1e6) / size * 0.92
            self.on_progress(0)
            self._encode()
            return
        self.on_done(True, None)

    def _cleanup(self):
        try:
            os.remove(self.out)
        except OSError:
            pass

    def _active(self):
        return [(i, t) for i, t in enumerate(self.tracks) if not t["muted"] and (t["gain"] > 0 or t["points"])]

    def video_bitrate(self):
        audio = 160_000 if self._active() else 0
        total = self.target_mb * 1e6 * 8 * 0.94 / max(self.dur, 0.1)
        return max(int((total - audio) * self.bitrate_scale), 250_000)

    def _argv(self):
        A, B = self.segments[0][0], self.segments[-1][1]
        a = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]
        if self.audio_only:
            graph, label = self._audio_graph(A, video=True)   # video=True: normalization gain included
            return a + ["-ss", f"{A:.3f}", "-t", f"{B - A:.3f}", "-i", self.src, "-filter_complex", ";".join(graph),
                        "-map", label, "-c:a", "libmp3lame", "-b:a", "192k", self.out]
        if self.multi_screen:
            return self._argv_screens(a, A, B)
        if self.copy:
            a += ["-ss", f"{A:.3f}", "-t", f"{B - A:.3f}", "-i", self.src]
            active = self._active()
            if len(self.tracks) > 1 and active:
                # Original: nothing is re-encoded except a mix of the tracks, placed as the 1st track (the one
                # Discord, browsers… play); the separate tracks follow, untouched, for an editing program.
                graph = [f"[0:a:{i}]volume='{envelope_expr(t['points'], t['gain'], A)}':eval=frame[t{i}]"
                         for i, t in active]
                graph.append("".join(f"[t{i}]" for i, _ in active) +
                             f"amix=inputs={len(active)}:normalize=0:dropout_transition=0[mix]")
                a += ["-filter_complex", ";".join(graph), "-map", "0:v:0", "-map", "[mix]", "-map", "0:a",
                      "-c:v", "copy", "-c:a", "copy", "-c:a:0", "aac", "-b:a:0", "192k",
                      "-metadata:s:a:0", "title=Mix", "-disposition:a:0", "default"]
                for k in range(1, len(self.tracks) + 1):
                    a += [f"-disposition:a:{k}", "0"]
            else:
                a += ["-map", "0", "-c", "copy"]
            return a + ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart", self.out]

        kind, enc = self.backend
        multi = len(self.segments) > 1
        node = render_node()
        zero_copy = kind == "vaapi" and not multi   # decoding → scaling → encoding, all on the GPU
        if kind == "vaapi":
            a += ["-init_hw_device", f"vaapi=va:{node}", "-filter_hw_device", "va",
                  "-hwaccel", "vaapi", "-hwaccel_device", "va"]
            if zero_copy:
                a += ["-hwaccel_output_format", "vaapi"]
        elif kind == "nvenc":
            a += ["-hwaccel", "cuda"]
        elif kind == "qsv":
            a += ["-init_hw_device", "qsv=hw"]
        a += ["-ss", f"{A:.3f}", "-t", f"{B - A:.3f}", "-i", self.src]

        graph = []
        n = len(self.segments)
        rel = [(s - A, e - A) for s, e in self.segments]

        audio_graph, audio = self._audio_graph(A, video=True)
        graph += audio_graph

        # removed sections: video trim + concatenation (the audio is already trimmed in _audio_graph)
        video = "[0:v:0]"
        if multi:
            graph.append(f"{video}split={n}" + "".join(f"[vs{k}]" for k in range(n)))
            for k, (s, e) in enumerate(rel):
                graph.append(f"[vs{k}]trim=start={s:.4f}:end={e:.4f},setpts=PTS-STARTPTS[v{k}]")
            graph.append("".join(f"[v{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0[vc]")
            video = "[vc]"

        chain = []
        if self.fps:
            chain.append(f"fps={self.fps}")
        if zero_copy:
            chain.append(f"scale_vaapi=w=-2:h={self.height}:format=nv12" if self.height else "scale_vaapi=format=nv12")
        else:
            if self.height:
                chain.append(f"scale=-2:{self.height}:flags=bicubic")
            if kind == "vaapi":
                chain += ["format=nv12", "hwupload"]
            else:
                chain.append("format=nv12" if kind in ("nvenc", "qsv") else "format=yuv420p")
        graph.append(f"{video}{','.join(chain)}[vout]")

        a += ["-filter_complex", ";".join(graph), "-map", "[vout]"]
        if audio:
            a += ["-map", audio, "-c:a", "aac", "-b:a", "160k"]
        a += ["-c:v", enc] + self._rate_args(kind)
        a += ["-movflags", "+faststart", self.out]
        return a

    def _argv_screens(self, a, A, B):
        """Multi-screen edit: each piece comes from its file (realigned by its offset), resized to the
        clip's size, then chained; the audio comes from the clip (1st screen)."""
        kind, enc = self.backend
        hw = []
        if kind == "vaapi":
            a += ["-init_hw_device", f"vaapi=va:{render_node()}", "-filter_hw_device", "va"]
            hw = ["-hwaccel", "vaapi", "-hwaccel_device", "va"]   # GPU decoding, frames brought back for the edit
        elif kind == "nvenc":
            hw = ["-hwaccel", "cuda"]
        elif kind == "qsv":
            a += ["-init_hw_device", "qsv=hw"]
        dur0, w0, h0, fps0 = video_info(self.src) or (B, 1920, 1080, 60)
        H = self.height or h0
        W = int(round(H * w0 / h0 / 2)) * 2
        fps = self.fps or fps0
        used = sorted({k for _s, _e, k in self.plan})
        base = {}   # screen → (ffmpeg input no., clip time matching its frame 0)
        a += hw + ["-ss", f"{A:.3f}", "-t", f"{B - A:.3f}", "-i", self.src]
        base[0] = (0, A)
        for k in used:
            if k == 0:
                continue
            sc = self.screens[k]
            ss = max(0.0, A - sc["offset"])
            a += hw + ["-ss", f"{ss:.3f}", "-t", f"{B - A + 1:.3f}", "-i", sc["path"]]
            base[k] = (len(base), ss + sc["offset"])

        graph, audio = self._audio_graph(A, video=True)
        labels = {}
        for k in used:
            idx = [i for i, p in enumerate(self.plan) if p[2] == k]
            src = f"[{base[k][0]}:v:0]"
            if len(idx) > 1:
                graph.append(src + f"split={len(idx)}" + "".join(f"[s{i}]" for i in idx))
                labels.update({i: f"[s{i}]" for i in idx})
            else:
                labels[idx[0]] = src
        for i, (s, e, k) in enumerate(self.plan):
            b0 = base[k][1]
            graph.append(f"{labels[i]}trim=start={s - b0:.4f}:end={e - b0:.4f},setpts=PTS-STARTPTS,fps={fps},"
                         f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic,"
                         f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[p{i}]")
        n = len(self.plan)
        graph.append("".join(f"[p{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]")
        if kind == "vaapi":
            graph.append("[vc]format=nv12,hwupload[vout]")
        else:
            graph.append("[vc]format=nv12[vout]" if kind in ("nvenc", "qsv") else "[vc]null[vout]")
        a += ["-filter_complex", ";".join(graph), "-map", "[vout]"]
        if audio:
            a += ["-map", audio, "-c:a", "aac", "-b:a", "160k"]
        a += ["-c:v", enc] + self._rate_args(kind)
        return a + ["-movflags", "+faststart", self.out]

    def _audio_graph(self, A, video):
        """Volume (or curve) per track → mix → trim of the sections → normalization gain.
        Returns (filters, output label); ([], None) if there is no active track."""
        active = self._active()
        if not active:
            return [], None
        graph = []
        for i, t in active:
            graph.append(f"[0:a:{i}]volume='{envelope_expr(t['points'], t['gain'], A)}':eval=frame[t{i}]")
        if len(active) > 1:
            graph.append("".join(f"[t{i}]" for i, _ in active) +
                         f"amix=inputs={len(active)}:normalize=0:dropout_transition=0[amix]")
            label = "[amix]"
        else:
            label = f"[t{active[0][0]}]"
        n = len(self.segments)
        if n > 1:
            graph.append(f"{label}asplit={n}" + "".join(f"[as{k}]" for k in range(n)))
            for k, (s, e) in enumerate(self.segments):
                graph.append(f"[as{k}]atrim=start={s - A:.4f}:end={e - A:.4f},asetpts=PTS-STARTPTS[a{k}]")
            graph.append("".join(f"[a{k}]" for k in range(n)) + f"concat=n={n}:v=0:a=1[acat]")
            label = "[acat]"
        if video and self.norm_gain:
            lim = ",alimiter=limit=0.891:level=0:attack=5:release=50" if self.limit else ""
            graph.append(f"{label}volume={self.norm_gain:.2f}dB{lim}[anorm]")
            label = "[anorm]"
        return graph, label

    def _rate_args(self, kind):
        b = self.video_bitrate() if self.target_mb else 0
        vbr = ["-b:v", str(b), "-maxrate", str(int(b * 1.3)), "-bufsize", str(b * 2)]
        if kind == "vaapi":
            extra = ["-profile:v", "high"] if self.codec == "h264" else []
            return extra + (["-rc_mode", "VBR"] + vbr if b else ["-rc_mode", "CQP", "-qp", str(self.qp)])
        if kind == "nvenc":
            return ["-preset", "p5"] + (["-rc", "vbr"] + vbr if b else ["-rc", "vbr", "-cq", str(self.qp), "-b:v", "0"])
        if kind == "qsv":
            return vbr if b else ["-global_quality", str(self.qp)]
        preset = ["-preset", "8"] if self.codec == "av1" else ["-preset", "veryfast"]
        return preset + (vbr if b else ["-crf", str(self.qp)])
