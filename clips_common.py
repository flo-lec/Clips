"""Configuration partagée entre l'appli Clips, le service d'enregistrement et les scripts."""
import copy
import functools
import glob
import json
import os
import subprocess

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "clips")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
RECORDING_FILE = os.path.join(CONFIG_DIR, "recording.json")   # pistes du replay en cours
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "clips")
SERVICE = "clips-recorder.service"

# Enregistrement manuel (en plus du replay) : le service ouvre un canal de commande (IPC) de gpu-screen-recorder.
# Les variables CLIPS_* ne servent qu'aux tests (autre instance, autre dossier).
RUNTIME_DIR = os.environ.get("CLIPS_RUNTIME_DIR") or os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/clips-{os.getuid()}"
GSR_SOCKET = os.path.join(RUNTIME_DIR, "clips-gsr.sock")


def extra_socket(monitor):
    """Canal de commande de l'enregistreur d'un écran supplémentaire."""
    return os.path.join(RUNTIME_DIR, f"clips-gsr-{monitor}.sock")
REC_STATE = os.path.join(RUNTIME_DIR, "clips-manual-recording.json")   # présent = enregistrement en cours

# Raccourcis globaux (installés par setup_shortcut.py, modifiables ensuite dans les réglages du bureau).
SHORTCUTS = {"save": "<Alt>F10", "rec_start": "<Alt>F9", "rec_stop": "<Alt>F11"}


def shortcut_label(key):
    return SHORTCUTS[key].replace("<Alt>", "Alt+").replace("<Control>", "Ctrl+").replace("<Shift>", "Maj+")


def gsr_request(name, timeout=5.0, sock_path=GSR_SOCKET):
    """Envoie une requête au gpu-screen-recorder du replay. Retourne (ok, données ou message d'erreur).
    ok vaut None si le replay n'écoute pas (service arrêté)."""
    import socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(sock_path)
            sock.sendall(json.dumps({"id": 1, "name": name}).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except (FileNotFoundError, ConnectionRefusedError):
        return None, "Le replay n'est pas actif"
    except OSError as e:
        return False, str(e)
    try:
        reply = json.loads(buf.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, "Réponse illisible de gpu-screen-recorder"
    return reply.get("result") == "ok", reply.get("data")


def manual_recording_started():
    """Heure de début (time.time()) de l'enregistrement manuel en cours, sinon None."""
    try:
        with open(REC_STATE) as f:
            return float(json.load(f)["started"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def manual_recording_path():
    """Fichier en cours d'écriture par l'enregistrement manuel (affiché « En cours » dans la bibliothèque)."""
    try:
        with open(REC_STATE) as f:
            return json.load(f).get("path")
    except (OSError, ValueError, AttributeError):
        return None


def set_manual_recording(started, path=None):
    if started is None:
        try:
            os.remove(REC_STATE)
        except OSError:
            pass
    else:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        _write_json(REC_STATE, {"started": started, "path": path})

# Les trois sources façon Moments, dans l'ordre des pistes du fichier.
SOURCE_KINDS = ["game", "chat", "mic"]
SOURCE_LABELS = {"game": "Jeu", "chat": "Chat vocal", "mic": "Micro"}
SOURCE_ICONS = {"game": "input-gaming-symbolic", "chat": "user-available-symbolic",
                "mic": "audio-input-microphone-symbolic", "app": "applications-multimedia-symbolic",
                "other": "applications-multimedia-symbolic"}


# Noms sous lesquels les clients de chat vocal apparaissent dans PipeWire (insensible à la casse).
CHAT_APPS = ["vesktop", "discord", "WEBRTC VoiceEngine", "webcord", "legcord", "armcord",
             "TeamSpeak", "Mumble", "Element", "Slack", "zoom"]


OCEAN = "/usr/share/sounds/ocean/stereo"
SOUNDS = [
    ("success", "Succès", f"{OCEAN}/completion-success.oga"),
    ("outcome", "Réussite", f"{OCEAN}/outcome-success.oga"),
    ("sent", "Envoi", f"{OCEAN}/message-sent-instant.oga"),
    ("highlight", "Message", f"{OCEAN}/message-highlight.oga"),
]


def available_sounds():
    return [s for s in SOUNDS if os.path.exists(s[2])]


def sound_path(key):
    return next((p for k, _l, p in available_sounds() if k == key), None) or \
        next((p for _k, _l, p in available_sounds()), None)


def play_sound(key, volume=None):
    """volume en % (0–100) ; par défaut celui de la config."""
    path = sound_path(key)
    if volume is None:
        volume = load_config().get("sound_volume", 70)
    if path and volume > 0:
        subprocess.Popen(["pw-play", "--volume", f"{volume / 100:.2f}", path], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def layer_shell_available():
    """Le popup par-dessus le jeu nécessite gtk4-layer-shell (couche « overlay » Wayland)."""
    return bool(glob.glob("/usr/lib*/girepository-1.0/Gtk4LayerShell-1.0.typelib")
                or glob.glob("/usr/lib/*/girepository-1.0/Gtk4LayerShell-1.0.typelib")) \
        and os.environ.get("WAYLAND_DISPLAY") is not None


def render_node():
    """Nœud de rendu du GPU (pour VAAPI) : le premier /dev/dri/renderD* disponible."""
    nodes = sorted(glob.glob("/dev/dri/renderD*"))
    return nodes[0] if nodes else None


def _videos_dir():
    try:
        out = subprocess.run(["xdg-user-dir", "VIDEOS"], capture_output=True, text=True).stdout.strip()
        if out and out != os.path.expanduser("~"):
            return out
    except OSError:
        pass
    return os.path.expanduser("~/Videos")


DEFAULTS = {
    "clips_dir": os.path.join(_videos_dir(), "Clips"),
    "monitor": "screen",
    "extra_monitors": [],          # autres écrans filmés en parallèle (sans son) ; le son vient de « monitor »
    "replay_seconds": 30,
    "fps": 60,
    "quality": "very_high",
    "codec": "h264",
    "discord_limit_mb": 10,
    "save_sound": True,
    "sound": "success",            # son de confirmation (voir SOUNDS)
    "sound_volume": 70,            # %
    "save_popup": True,            # encart « Clip enregistré » par-dessus le jeu
    "save_notification": True,     # notification du bureau (boutons Éditer / Ouvrir le dossier)
    # game.mode : "except_chat" (tout sauf l'appli de chat) | "all" (tout le son du PC) | "app" (une appli)
    "sources": {
        "game": {"enabled": True, "mode": "except_chat", "app": ""},
        "chat": {"enabled": True, "app": "vesktop"},
        "mic": {"enabled": True, "device": "default_input"},
        # Avancé : une piste par application listée ; le reste va dans « Autres sons ».
        "split": {"enabled": False, "apps": []},
    },
    # Niveaux par défaut appliqués dans l'éditeur (1.0 = 100 %).
    "levels": {"game": 1.0, "chat": 0.8, "mic": 1.0},
    # Éditeur : courbes de volume par piste (mode avancé).
    "advanced_audio": False,
    "lane_height": 46,             # hauteur des pistes en mode avancé (px)
    # Export : normalisation du volume (niveau standard des plateformes, sans saturation).
    "normalize_audio": True,
    # Détection du jeu (Steam) à la sauvegarde : nom du clip + filtre dans la bibliothèque.
    "detect_game": True,
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    cfg = _merge(DEFAULTS, raw)
    # Ancien format (v1) : record_system / record_mic.
    if "sources" not in raw:
        if "record_system" in raw:
            cfg["sources"]["game"]["enabled"] = raw["record_system"]
        if "record_mic" in raw:
            cfg["sources"]["mic"]["enabled"] = raw["record_mic"]
    for k in ("record_system", "record_mic"):
        cfg.pop(k, None)
    # Ancien réglage « deuxième écran » (un seul) → liste d'écrans.
    second = cfg.pop("monitor2", "")
    if second and "extra_monitors" not in raw:
        cfg["extra_monitors"] = [second]
    return cfg


def extra_monitors(cfg):
    """Écrans filmés en plus de l'écran principal (sans doublon, jamais l'écran principal)."""
    out = []
    for m in cfg.get("extra_monitors") or []:
        if m and m != cfg["monitor"] and m not in out:
            out.append(m)
    return out


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def save_config(cfg):
    _write_json(CONFIG_FILE, cfg)


def exports_dir(cfg):
    return os.path.join(cfg["clips_dir"], "Exports")


def meta_path(clip_path):
    """Fichier annexe (caché, à côté des clips) qui décrit les pistes d'un clip."""
    d, name = os.path.split(clip_path)
    return os.path.join(d, ".meta", name + ".json")


def split_apps(cfg):
    """Applis à enregistrer chacune sur leur piste (mode avancé), sans l'appli de chat."""
    src = cfg["sources"]
    if not src["split"]["enabled"]:
        return []
    chat = src["chat"]["app"].strip().lower() if src["chat"]["enabled"] else ""
    seen, out = set(), []
    for a in src["split"]["apps"]:
        a = a.strip()
        if a and a.lower() != chat and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def audio_tracks(cfg):
    """Pistes à enregistrer : liste de {kind, label, source} ; source = argument -a de gpu-screen-recorder.

    Rien n'est rerouté : gpu-screen-recorder ajoute des liens PipeWire en parallèle, tes sorties restent intactes.
    """
    src = cfg["sources"]
    chat_app = src["chat"]["app"].strip() if src["chat"]["enabled"] else ""
    apps = split_apps(cfg)
    tracks = []
    if src["game"]["enabled"]:
        mode = src["game"]["mode"]
        if mode == "app" and src["game"]["app"].strip():
            arg, label = f"app:{src['game']['app'].strip()}", SOURCE_LABELS["game"]
        else:
            excluded = ([chat_app] if chat_app and (mode == "except_chat" or apps) else []) + apps
            # Plusieurs « app-inverse » dans une même piste = tout sauf toutes ces applis.
            arg = "|".join(f"app-inverse:{a}" for a in excluded) if excluded else "default_output"
            label = "Autres sons" if apps else SOURCE_LABELS["game"]
        tracks.append({"kind": "game", "label": label, "source": arg})
    for a in apps:
        tracks.append({"kind": "app", "label": a, "source": f"app:{a}"})
    if chat_app:
        tracks.append({"kind": "chat", "label": SOURCE_LABELS["chat"], "source": f"app:{chat_app}"})
    if src["mic"]["enabled"]:
        dev = src["mic"]["device"]
        arg = dev if dev in ("default_input", "") else f"device:{dev}"
        tracks.append({"kind": "mic", "label": SOURCE_LABELS["mic"], "source": arg or "default_input"})
    return tracks


def write_recording(tracks):
    _write_json(RECORDING_FILE, {"tracks": tracks})


def read_recording():
    try:
        with open(RECORDING_FILE) as f:
            return json.load(f).get("tracks", [])
    except (OSError, ValueError):
        return []


def save_clip_meta(clip_path, tracks, game=None, appid=None):
    data = {"tracks": tracks}
    if game:
        data["game"] = game
    if appid:
        data["appid"] = appid
    _write_json(meta_path(clip_path), data)


def load_clip_info(clip_path):
    try:
        with open(meta_path(clip_path)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def rename_clip(path, new_stem, cfg=None):
    """Renomme un clip avec son fichier annexe et ses exports. Retourne le nouveau chemin.
    Lève FileExistsError si le nom est pris, OSError en cas d'échec."""
    new_stem = new_stem.strip().replace("/", "-")
    d, ext = os.path.dirname(path), os.path.splitext(path)[1]
    old_stem = os.path.splitext(os.path.basename(path))[0]
    if not new_stem or new_stem == old_stem:
        return path
    dest = os.path.join(d, new_stem + ext)
    if os.path.exists(dest):
        raise FileExistsError(dest)
    others = [f for f in companion_files(path) if f != meta_path(path)]
    os.rename(path, dest)
    if os.path.exists(meta_path(path)):
        os.rename(meta_path(path), meta_path(dest))
    for f in others:   # autres écrans : <ancien nom>.<écran>.mp4 → <nouveau nom>.<écran>.mp4
        base = os.path.basename(f)
        os.rename(f, os.path.join(os.path.dirname(f), new_stem + base[len(old_stem):]))
    ed = exports_dir(cfg or load_config())
    if os.path.isdir(ed):
        for e in os.scandir(ed):
            if e.name.startswith(old_stem + "-"):
                os.rename(e.path, os.path.join(ed, new_stem + e.name[len(old_stem):]))
    return dest


def set_clip_game(clip_path, game, appid=None):
    """Définit (ou retire, game=None) le jeu d'un clip."""
    data = load_clip_info(clip_path)
    data.pop("game", None)
    data.pop("appid", None)
    if game:
        data["game"] = game
        appid = appid or steam_appid(game)
        if appid:
            data["appid"] = str(appid)
    _write_json(meta_path(clip_path), data)


def set_clip_favorite(clip_path, on):
    """Ajoute/retire le clip des favoris (conservé dans son fichier annexe)."""
    data = load_clip_info(clip_path)
    if on:
        data["favorite"] = True
    else:
        data.pop("favorite", None)
    _write_json(meta_path(clip_path), data)


# ---------------------------------------------------------------- plusieurs écrans
# Le 1er écran est le clip lui-même (avec le son). Les autres sont des vidéos sans son rangées dans
# <dossier des clips>/.screens/<nom du clip>.<écran>.mp4 ; le fichier annexe garde l'écran et le décalage
# (secondes : l'image 0 de l'écran k correspond à l'instant « offset » du clip).

def screens_dir(clips_dir):
    return os.path.join(clips_dir, ".screens")


def screen_file(clip_path, monitor):
    d, name = os.path.split(clip_path)
    stem, ext = os.path.splitext(name)
    return os.path.join(screens_dir(d), f"{stem}.{monitor}{ext}")


def clip_screens(clip_path, info=None):
    """[{"monitor", "path", "offset"}] : le clip lui-même d'abord, puis les écrans en plus encore présents."""
    info = load_clip_info(clip_path) if info is None else info
    screens = info.get("screens") or []
    out = [{"monitor": screens[0]["monitor"] if screens else "", "path": clip_path, "offset": 0.0}]
    for sc in screens[1:]:
        path = screen_file(clip_path, sc["monitor"])
        if os.path.exists(path):
            out.append({"monitor": sc["monitor"], "path": path, "offset": float(sc.get("offset", 0.0))})
    return out


def set_clip_screens(clip_path, screens):
    """screens : [{"monitor", "offset"}], le clip lui-même en premier."""
    data = load_clip_info(clip_path)
    data["screens"] = screens
    _write_json(meta_path(clip_path), data)


def set_clip_field(clip_path, key, value):
    """Une valeur du fichier annexe (None = retirée)."""
    data = load_clip_info(clip_path)
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    _write_json(meta_path(clip_path), data)


def set_clip_angles(clip_path, angles):
    """Bascules d'écran du montage : [[instant, n° d'écran], …] (la 1re à 0)."""
    data = load_clip_info(clip_path)
    if angles and angles != [[0.0, 0]]:
        data["angles"] = angles
    else:
        data.pop("angles", None)
    _write_json(meta_path(clip_path), data)


def clip_angles(clip_path, info=None):
    info = load_clip_info(clip_path) if info is None else info
    return [[float(t), int(k)] for t, k in info.get("angles") or [[0.0, 0]]]


def companion_files(clip_path):
    """Fichiers qui accompagnent un clip (annexe, autres écrans) : à renommer / supprimer avec lui."""
    files = [meta_path(clip_path)]
    files += [screen_file(clip_path, sc["monitor"]) for sc in (load_clip_info(clip_path).get("screens") or [])[1:]]
    return [f for f in files if os.path.exists(f)]


def load_clip_meta(clip_path):
    return load_clip_info(clip_path).get("tracks", [])


# ---------------------------------------------------------------- détection du jeu

STEAM_ROOTS = ["~/.local/share/Steam", "~/.steam/steam", "~/.var/app/com.valvesoftware.Steam/.local/share/Steam"]


def _steam_libraries():
    libs = []
    for root in STEAM_ROOTS:
        root = os.path.expanduser(root)
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        if os.path.isdir(os.path.join(root, "steamapps")):
            libs.append(os.path.join(root, "steamapps"))
        try:
            with open(vdf, errors="replace") as f:
                for line in f:
                    parts = line.strip().split('"')
                    if len(parts) >= 4 and parts[1] == "path":
                        libs.append(os.path.join(parts[3].replace("\\\\", "/"), "steamapps"))
        except OSError:
            pass
    return list(dict.fromkeys(os.path.realpath(p) for p in libs if os.path.isdir(p)))


def _steam_name(appid):
    for lib in _steam_libraries():
        try:
            with open(os.path.join(lib, f"appmanifest_{appid}.acf"), errors="replace") as f:
                for line in f:
                    parts = line.strip().split('"')
                    if len(parts) >= 4 and parts[1] == "name":
                        return parts[3]
        except OSError:
            continue
    return None


def detect_game():
    """Jeu Steam en cours : variable SteamAppId des processus de l'utilisateur (aucune fenêtre touchée).
    Retourne (nom, appid) ou (None, None)."""
    counts = {}
    uid = os.getuid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/environ", "rb") as f:
                env = f.read()
        except OSError:
            continue
        for var in (b"SteamAppId=", b"SteamGameId="):
            i = env.find(var)
            if i >= 0:
                val = env[i + len(var):env.find(b"\0", i)].decode(errors="ignore")
                if val.isdigit() and val != "0" and len(val) < 10:
                    counts[val] = counts.get(val, 0) + 1
                break
    for appid, _n in sorted(counts.items(), key=lambda kv: -kv[1]):
        name = _steam_name(appid)
        if name and "steamworks" not in name.lower() and "proton" not in name.lower():
            return name, appid
    return None, None


_APPID_BY_NAME = None


def steam_appid(name):
    """Retrouve l'appid d'un jeu installé à partir de son nom (clips plus anciens sans appid)."""
    global _APPID_BY_NAME
    if _APPID_BY_NAME is None:
        _APPID_BY_NAME = {}
        for lib in _steam_libraries():
            for f in os.listdir(lib):
                if f.startswith("appmanifest_") and f.endswith(".acf"):
                    try:
                        with open(os.path.join(lib, f), errors="replace") as fh:
                            for line in fh:
                                parts = line.strip().split('"')
                                if len(parts) >= 4 and parts[1] == "name":
                                    _APPID_BY_NAME[parts[3].lower()] = f[12:-4]
                                    break
                    except OSError:
                        pass
    return _APPID_BY_NAME.get((name or "").lower())


@functools.lru_cache(maxsize=256)
def steam_installed_games():
    """[(nom, appid)] des jeux Steam installés (sans Proton, runtimes, redistribuables…)."""
    skip = ("proton", "steamworks", "steam linux runtime", "redistributable", "soundtrack")
    out = {}
    for lib in _steam_libraries():
        for f in os.listdir(lib):
            if not (f.startswith("appmanifest_") and f.endswith(".acf")):
                continue
            try:
                with open(os.path.join(lib, f), errors="replace") as fh:
                    for line in fh:
                        parts = line.strip().split('"')
                        if len(parts) >= 4 and parts[1] == "name":
                            if not any(k in parts[3].lower() for k in skip):
                                out[parts[3]] = f[12:-4]
                            break
            except OSError:
                pass
    return sorted(out.items(), key=lambda kv: kv[0].lower())


@functools.lru_cache(maxsize=256)
def game_art(appid):
    """Images du jeu déjà en cache chez Steam (rien n'est téléchargé) :
    icon (32×32), logo (transparent), cover (600×900), header (460×215), hero (fond large)."""
    art = {}
    if not appid:
        return {}
    wanted = {"logo.png": "logo", "library_600x900.jpg": "cover", "library_header.jpg": "header",
              "header.jpg": "header", "library_hero.jpg": "hero"}
    for root in STEAM_ROOTS:
        cache = os.path.join(os.path.expanduser(root), "appcache", "librarycache")
        d = os.path.join(cache, str(appid))
        if os.path.isdir(d):   # format récent : un dossier par jeu
            for dirpath, _dirs, files in os.walk(d):
                for f in files:
                    full = os.path.join(dirpath, f)
                    if f in wanted:
                        art.setdefault(wanted[f], full)
                    elif dirpath == d and f.endswith(".jpg") and len(f) == 44:
                        art.setdefault("icon", full)   # <sha1>.jpg à la racine = icône
        for suffix, key in (("_icon.jpg", "icon"), ("_logo.png", "logo"), ("_library_600x900.jpg", "cover"),
                            ("_header.jpg", "header"), ("_library_hero.jpg", "hero")):
            f = os.path.join(cache, f"{appid}{suffix}")   # ancien format à plat
            if os.path.exists(f):
                art.setdefault(key, f)
        if art:
            break
    return art


def safe_filename(name):
    return "".join("-" if c in '/\\:*?"<>|' else c for c in name).strip(" .") or "Clip"
