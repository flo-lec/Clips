# Clips

> [!TIP]
> **Looking for something maintained? Use [Snappy](https://github.com/florentin-leclercq/Snappy).**
> Snappy is based on this project, runs on Windows and Linux, and is a more complete implementation.

> [!CAUTION]
> ## ⚠️ 100% VIBE CODED ⚠️
>
> **This project was written entirely by an AI (Claude), "vibe coding" style.**
> Not a single line was reviewed or written by hand. The code is not audited, not seriously
> tested, and may contain bugs, security holes or downright absurd things.
>
> **Use at your own risk.** This repository is archived: no issues, no PRs, no support,
> no updates.

> [!WARNING]
> **Only ever used on CachyOS** (Arch-based, KDE Plasma, Wayland). It has never been tried on any other
> distribution or desktop environment — it may or may not work there.

Instant replay, library, editor and sharing for game clips on Linux (à la SteelSeries Moments),
built with GTK4 / libadwaita and using `gpu-screen-recorder` for capture.

## Installation

```sh
bash install.sh         # dependencies (Arch and derivatives) + Clips + replay service
bash install.sh --force # install even if some dependencies are missing
bash uninstall.sh       # uninstall (add --purge to also remove the config and cache)
```

### Global shortcuts

| Shortcut | Action |
| --- | --- |
| <kbd>Alt</kbd>+<kbd>F10</kbd> | Save the last seconds of the replay |
| <kbd>Alt</kbd>+<kbd>F9</kbd> | Start a recording |
| <kbd>Alt</kbd>+<kbd>F11</kbd> | Stop the recording |

`install.sh` tries to register them automatically at the end, but this only works on **KDE Plasma** and
**GNOME**, and a failure is silently ignored. If they don't work, use the **Install** button in
*Preferences → Recording → Shortcuts*, or run:

```sh
python3 ~/.local/share/clips/setup_shortcut.py
```

On other desktops (Hyprland, Sway, i3, XFCE…), that script prints the lines to add to your config yourself.
