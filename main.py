import ctypes
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
import urllib.request
import tempfile
from dataclasses import dataclass
from tkinter import filedialog

import customtkinter as ctk

try:
    from PIL import Image  # optional, better image scaling
except Exception:
    Image = None

try:
    import winreg  # Windows only
except Exception:
    winreg = None


# =============================
# App Info / Update (GitHub)
# =============================

APP_NAME = "AutoResSwitcher"
APP_VERSION = "v1.0.0"  # your chosen format: v1.0.0
GITHUB_OWNER = "RealSchnobel"
GITHUB_REPO = "AutoResSwitcher"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
UPDATE_ASSET_NAME = "AutoResSwitcher_Setup.exe"


# =============================
# Settings
# =============================

CONFIG_FILE = "config.json"
IMAGES_DIR = "images"

DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_REFRESH_HZ = 0  # 0 => do not force refresh unless user sets it

TEST_SECONDS = 7
STATUS_MSG_SECONDS = 2.5

DISCOVERY_SCAN_LIMIT = 500  # avoid flooding the list


# =============================
# Windows Display API (ctypes)
# =============================

user32 = ctypes.WinDLL("user32", use_last_error=True)

ENUM_CURRENT_SETTINGS = -1
CDS_UPDATEREGISTRY = 0x01
CDS_TEST = 0x02
DISP_CHANGE_SUCCESSFUL = 0
DM_PELSWIDTH = 0x80000
DM_PELSHEIGHT = 0x100000
DM_DISPLAYFREQUENCY = 0x400000


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_uint),
        ("dmOrientation", ctypes.c_short),
        ("dmPaperSize", ctypes.c_short),
        ("dmPaperLength", ctypes.c_short),
        ("dmPaperWidth", ctypes.c_short),
        ("dmScale", ctypes.c_short),
        ("dmCopies", ctypes.c_short),
        ("dmDefaultSource", ctypes.c_short),
        ("dmPrintQuality", ctypes.c_short),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_uint),
        ("dmPelsWidth", ctypes.c_uint),
        ("dmPelsHeight", ctypes.c_uint),
        ("dmDisplayFlags", ctypes.c_uint),
        ("dmDisplayFrequency", ctypes.c_uint),
        ("dmICMMethod", ctypes.c_uint),
        ("dmICMIntent", ctypes.c_uint),
        ("dmMediaType", ctypes.c_uint),
        ("dmDitherType", ctypes.c_uint),
        ("dmReserved1", ctypes.c_uint),
        ("dmReserved2", ctypes.c_uint),
        ("dmPanningWidth", ctypes.c_uint),
        ("dmPanningHeight", ctypes.c_uint),
    ]


def get_current_display_mode():
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    ok = user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(dm))
    if not ok:
        raise OSError("EnumDisplaySettingsW failed")
    return dm


def set_resolution(width: int, height: int, refresh_hz: int | None = None) -> tuple[bool, str]:
    dm = get_current_display_mode()
    dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT
    dm.dmPelsWidth = int(width)
    dm.dmPelsHeight = int(height)

    if refresh_hz is not None and int(refresh_hz) > 0:
        dm.dmFields |= DM_DISPLAYFREQUENCY
        dm.dmDisplayFrequency = int(refresh_hz)

    res_test = user32.ChangeDisplaySettingsW(ctypes.byref(dm), CDS_TEST)
    if res_test != DISP_CHANGE_SUCCESSFUL:
        return False, f"Windows konnte die Auflösung nicht testen (Code {res_test})."

    res_apply = user32.ChangeDisplaySettingsW(ctypes.byref(dm), CDS_UPDATEREGISTRY)
    if res_apply != DISP_CHANGE_SUCCESSFUL:
        return False, f"Windows konnte die Auflösung nicht setzen (Code {res_apply})."

    return True, "OK"


# =============================
# Process detection (no deps)
# =============================

def list_running_process_names_lower() -> set[str]:
    """
    Uses Windows 'tasklist' (no extra packages).
    Returns set of process image names in lowercase, e.g. {'cs2.exe', 'explorer.exe'}.
    """
    try:
        cp = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        names = set()
        for line in cp.stdout.splitlines():
            if not line.strip():
                continue
            if line.startswith('"'):
                parts = [p.strip().strip('"') for p in line.split('","')]
                if parts:
                    names.add(parts[0].lower())
        return names
    except Exception:
        return set()


# =============================
# Config
# =============================

@dataclass
class GameConfig:
    name: str
    process: str
    width: int
    height: int
    refresh_hz: int
    image_path: str
    exe_path: str | None = None
    source: str | None = None  # "steam"/"epic"/"riot"/"ubisoft"/"battlenet"/"manual"/...


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", name.strip())
    return name or "game"


def default_config() -> dict:
    os.makedirs(IMAGES_DIR, exist_ok=True)
    return {
        "custom_scan_paths": [
            # user can add more folders here later, e.g. "D:\\Games"
        ],
        "games": {
            "cs2.exe": {
                "name": "Counter-Strike 2",
                "process": "cs2.exe",
                "width": 1440,
                "height": 1080,
                "refresh_hz": 144,
                "image_path": os.path.join(IMAGES_DIR, "cs2.png"),
                "exe_path": None,
                "source": "preset",
            },
            "valorant-win64-shipping.exe": {
                "name": "Valorant",
                "process": "VALORANT-Win64-Shipping.exe",
                "width": 1568,
                "height": 1080,
                "refresh_hz": 144,
                "image_path": os.path.join(IMAGES_DIR, "valorant.png"),
                "exe_path": None,
                "source": "preset",
            },
        },
    }


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        cfg = default_config()
        save_config(cfg)
        return cfg
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if "games" not in cfg:
            cfg = default_config()
        if "custom_scan_paths" not in cfg:
            cfg["custom_scan_paths"] = []
        save_config(cfg)
        return cfg
    except Exception:
        cfg = default_config()
        save_config(cfg)
        return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# =============================
# Version compare
# =============================

def _normalize_version(v: str) -> tuple[int, ...] | None:
    """
    Accepts 'v1.2.3' or '1.2.3'. Returns tuple (1,2,3).
    If not parseable, returns None.
    """
    v = (v or "").strip()
    if v.lower().startswith("v"):
        v = v[1:]
    if not v:
        return None
    parts = v.split(".")
    out = []
    for p in parts:
        if not p.isdigit():
            return None
        out.append(int(p))
    return tuple(out)


def is_newer_version(remote: str, local: str) -> bool:
    rv = _normalize_version(remote)
    lv = _normalize_version(local)
    if rv is None or lv is None:
        # fallback: simple string compare (not ideal, but safe)
        return (remote or "") != (local or "")
    # pad to same length
    n = max(len(rv), len(lv))
    rv2 = rv + (0,) * (n - len(rv))
    lv2 = lv + (0,) * (n - len(lv))
    return rv2 > lv2


# =============================
# Registry helpers (Windows)
# =============================

def _read_registry_string(root, path: str, name: str) -> str | None:
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(root, path) as k:
            v, _t = winreg.QueryValueEx(k, name)
            if isinstance(v, str) and v.strip():
                return v.strip()
    except Exception:
        return None
    return None


def _enum_subkeys(root, path: str) -> list[str]:
    if winreg is None:
        return []
    try:
        with winreg.OpenKey(root, path) as k:
            i = 0
            out = []
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
            return out
    except Exception:
        return []


def _get_uninstall_entries() -> list[dict]:
    """
    Best-effort: reads uninstall entries (games often appear here).
    """
    if winreg is None:
        return []

    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    entries: list[dict] = []

    for root, base in roots:
        for sub in _enum_subkeys(root, base):
            full = base + "\\" + sub
            name = _read_registry_string(root, full, "DisplayName")
            if not name:
                continue
            publisher = _read_registry_string(root, full, "Publisher") or ""
            install_location = _read_registry_string(root, full, "InstallLocation") or ""
            display_icon = _read_registry_string(root, full, "DisplayIcon") or ""
            uninstall_string = _read_registry_string(root, full, "UninstallString") or ""

            entries.append({
                "name": name,
                "publisher": publisher,
                "install_location": install_location,
                "display_icon": display_icon,
                "uninstall_string": uninstall_string,
            })

    return entries


# =============================
# Game discovery (best effort)
# =============================

def _looks_like_exe_path(s: str) -> str | None:
    if not s:
        return None
    s = s.strip().strip('"')
    # DisplayIcon often: "C:\Path\game.exe,0"
    if "," in s and s.lower().endswith(".exe,0"):
        s = s[: s.lower().rfind(".exe") + 4]
    # sometimes like: C:\Path\game.exe /something
    if ".exe" in s.lower():
        idx = s.lower().rfind(".exe")
        s = s[: idx + 4]
    if s.lower().endswith(".exe") and os.path.exists(s):
        return s
    return None


def _pick_exe_in_folder(folder: str, depth_limit: int = 2) -> str | None:
    """
    Heuristic: choose one .exe within folder, prefer root .exe.
    """
    if not folder or not os.path.isdir(folder):
        return None

    try:
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and f.lower().endswith(".exe"):
                return p
    except Exception:
        pass

    for root, dirs, files in os.walk(folder):
        depth = root[len(folder):].count(os.sep)
        if depth > depth_limit:
            dirs[:] = []
            continue
        for f in files:
            if f.lower().endswith(".exe"):
                return os.path.join(root, f)
    return None


def find_steam_root() -> str | None:
    if winreg is None:
        return None
    candidates = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ]
    for root, path, name in candidates:
        p = _read_registry_string(root, path, name)
        if p and os.path.isdir(p):
            return p
    return None


def parse_steam_library_folders(steam_root: str) -> list[str]:
    libs: list[str] = []
    vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
    if not os.path.exists(vdf):
        return [steam_root] if os.path.isdir(steam_root) else []

    try:
        txt = open(vdf, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        txt = ""

    for m in re.finditer(r'"path"\s*"([^"]+)"', txt, re.IGNORECASE):
        p = m.group(1).replace("\\\\", "\\")
        if os.path.isdir(p):
            libs.append(p)

    if os.path.isdir(steam_root):
        libs.append(steam_root)

    out: list[str] = []
    seen: set[str] = set()
    for p in libs:
        p2 = os.path.normpath(p)
        key = p2.lower()
        if key not in seen:
            out.append(p2)
            seen.add(key)
    return out


def parse_steam_appmanifest_name_and_dir(appmanifest_path: str) -> tuple[str | None, str | None]:
    try:
        txt = open(appmanifest_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None, None
    name_m = re.search(r'"name"\s*"([^"]+)"', txt)
    dir_m = re.search(r'"installdir"\s*"([^"]+)"', txt)
    name = name_m.group(1).strip() if name_m else None
    installdir = dir_m.group(1).strip() if dir_m else None
    return name, installdir


def discover_steam_games() -> list[dict]:
    found: list[dict] = []
    steam = find_steam_root()
    if not steam:
        return found

    libs = parse_steam_library_folders(steam)
    for lib in libs:
        steamapps = os.path.join(lib, "steamapps")
        if not os.path.isdir(steamapps):
            continue

        try:
            entries = os.listdir(steamapps)
        except Exception:
            continue

        for fn in entries:
            if not (fn.startswith("appmanifest_") and fn.endswith(".acf")):
                continue
            manifest = os.path.join(steamapps, fn)
            name, installdir = parse_steam_appmanifest_name_and_dir(manifest)
            if not installdir:
                continue

            game_dir = os.path.join(steamapps, "common", installdir)
            exe = _pick_exe_in_folder(game_dir, depth_limit=2)
            if not exe:
                continue

            found.append({
                "name": name or os.path.splitext(os.path.basename(exe))[0],
                "exe_path": exe,
                "process": os.path.basename(exe),
                "source": "steam",
            })

            if len(found) >= DISCOVERY_SCAN_LIMIT:
                return found
    return found


def discover_epic_games() -> list[dict]:
    """
    Epic manifests:
      C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests\*.item
    JSON contains InstallLocation, DisplayName; sometimes LaunchExecutable.
    """
    found: list[dict] = []
    manifests_dir = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                                 "Epic", "EpicGamesLauncher", "Data", "Manifests")
    if not os.path.isdir(manifests_dir):
        return found

    try:
        files = [os.path.join(manifests_dir, f) for f in os.listdir(manifests_dir) if f.lower().endswith(".item")]
    except Exception:
        return found

    for p in files:
        try:
            data = json.load(open(p, "r", encoding="utf-8", errors="ignore"))
        except Exception:
            continue

        name = (data.get("DisplayName") or data.get("AppName") or "").strip()
        install = (data.get("InstallLocation") or "").strip()
        launch_exe = (data.get("LaunchExecutable") or "").strip()

        exe_path = None
        if install and launch_exe:
            cand = os.path.join(install, launch_exe)
            if os.path.exists(cand) and cand.lower().endswith(".exe"):
                exe_path = cand

        if exe_path is None and install:
            exe_path = _pick_exe_in_folder(install, depth_limit=2)

        if not exe_path:
            continue

        found.append({
            "name": name or os.path.splitext(os.path.basename(exe_path))[0],
            "exe_path": exe_path,
            "process": os.path.basename(exe_path),
            "source": "epic",
        })

        if len(found) >= DISCOVERY_SCAN_LIMIT:
            break

    return found


def discover_riot_games() -> list[dict]:
    """
    Best-effort for Riot:
      default folder often C:\Riot Games\
    We scan shallowly for .exe.
    """
    found: list[dict] = []
    base = r"C:\Riot Games"
    if not os.path.isdir(base):
        return found

    # Scan each top-level folder (League of Legends, VALORANT, etc.)
    try:
        top = [os.path.join(base, d) for d in os.listdir(base)]
    except Exception:
        return found

    for d in top:
        if not os.path.isdir(d):
            continue
        exe = _pick_exe_in_folder(d, depth_limit=3)
        if not exe:
            continue

        name = os.path.basename(d)
        found.append({
            "name": name,
            "exe_path": exe,
            "process": os.path.basename(exe),
            "source": "riot",
        })

        if len(found) >= DISCOVERY_SCAN_LIMIT:
            break

    return found


def discover_from_uninstall_entries() -> list[dict]:
    """
    Best-effort for Ubisoft/Battle.net (and more): uninstall registry entries.
    We filter by publisher/name keywords and try to locate an .exe.
    """
    found: list[dict] = []
    entries = _get_uninstall_entries()

    keywords = [
        "ubisoft", "ubisoft connect",
        "blizzard", "battle.net",
        "riot", "valorant",
    ]

    for e in entries:
        name = (e.get("name") or "").strip()
        publisher = (e.get("publisher") or "").strip()
        hay = (name + " " + publisher).lower()

        if not any(k in hay for k in keywords):
            continue

        exe = (
                _looks_like_exe_path(e.get("display_icon") or "") or
                _looks_like_exe_path(e.get("uninstall_string") or "")
        )

        if exe is None:
            install_loc = (e.get("install_location") or "").strip()
            if install_loc:
                exe = _pick_exe_in_folder(install_loc, depth_limit=2)

        if not exe:
            continue

        source = "registry"
        if "ubisoft" in hay:
            source = "ubisoft"
        elif "battle.net" in hay or "blizzard" in hay:
            source = "battlenet"
        elif "riot" in hay:
            source = "riot"

        found.append({
            "name": name,
            "exe_path": exe,
            "process": os.path.basename(exe),
            "source": source,
        })

        if len(found) >= DISCOVERY_SCAN_LIMIT:
            break

    return found


def discover_custom_paths(paths: list[str]) -> list[dict]:
    """
    Optional extra scan paths from config. Shallow scanning for .exe.
    """
    found: list[dict] = []
    for base in paths or []:
        if not base or not os.path.isdir(base):
            continue
        try:
            for root, dirs, files in os.walk(base):
                depth = root[len(base):].count(os.sep)
                if depth > 3:
                    dirs[:] = []
                    continue
                for f in files:
                    if f.lower().endswith(".exe"):
                        exe = os.path.join(root, f)
                        found.append({
                            "name": os.path.splitext(f)[0],
                            "exe_path": exe,
                            "process": os.path.basename(exe),
                            "source": "custom",
                        })
                if len(found) >= DISCOVERY_SCAN_LIMIT:
                    return found
        except Exception:
            continue
    return found


def discover_all_games(custom_paths: list[str]) -> list[dict]:
    found: list[dict] = []
    found.extend(discover_steam_games())
    found.extend(discover_epic_games())
    found.extend(discover_riot_games())
    found.extend(discover_from_uninstall_entries())
    found.extend(discover_custom_paths(custom_paths))

    # unique by process name
    uniq: list[dict] = []
    seen: set[str] = set()
    for g in found:
        proc = (g.get("process") or "").lower()
        if not proc or proc in seen:
            continue
        seen.add(proc)
        uniq.append(g)
        if len(uniq) >= DISCOVERY_SCAN_LIMIT:
            break
    return uniq


# =============================
# App-like dropdown (custom)
# =============================

class AppDropdown(ctk.CTkFrame):
    """
    More app-like dropdown:
    - non-editable
    - custom popup with scroll
    - modern colors + rounding
    """
    def __init__(self, master, values: list[str], variable: tk.StringVar, command=None):
        super().__init__(master, corner_radius=14, fg_color="#111827")
        self._values = values or []
        self._var = variable
        self._command = command
        self._popup: ctk.CTkToplevel | None = None

        self.grid_columnconfigure(0, weight=1)

        self._label = ctk.CTkLabel(self, textvariable=self._var, anchor="w", text_color="#e5e7eb")
        self._label.grid(row=0, column=0, sticky="ew", padx=(14, 8), pady=10)

        self._btn = ctk.CTkButton(
            self,
            text="▾",
            width=42,
            height=30,
            corner_radius=10,
            fg_color="#1f2937",
            hover_color="#374151",
            text_color="#e5e7eb",
            command=self.toggle,
        )
        self._btn.grid(row=0, column=1, sticky="e", padx=(0, 10), pady=7)

        self.bind("<Button-1>", lambda _e: self.toggle())
        self._label.bind("<Button-1>", lambda _e: self.toggle())

        if not self._var.get() and self._values:
            self._var.set(self._values[0])

    def set_values(self, values: list[str]):
        self._values = values or []
        if self._var.get() not in self._values and self._values:
            self._var.set(self._values[0])

    def toggle(self):
        if self._popup is not None and self._popup.winfo_exists():
            self._close_popup()
        else:
            self._open_popup()

    def _open_popup(self):
        if not self._values:
            return
        self._close_popup()

        self._popup = ctk.CTkToplevel(self)
        self._popup.overrideredirect(True)
        self._popup.attributes("-topmost", True)

        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 6
        w = self.winfo_width()
        self._popup.geometry(f"{w}x300+{x}+{y}")

        container = ctk.CTkFrame(self._popup, corner_radius=16, fg_color="#0b1220")
        container.pack(fill="both", expand=True)

        scroll = ctk.CTkScrollableFrame(
            container,
            corner_radius=16,
            fg_color="#0b1220",
            scrollbar_button_color="#1f2937",
            scrollbar_button_hover_color="#374151",
        )
        scroll.pack(fill="both", expand=True, padx=10, pady=10)

        current = self._var.get()

        def choose(v: str):
            self._var.set(v)
            self._close_popup()
            if callable(self._command):
                self._command(v)

        for v in self._values:
            is_selected = (v == current)
            btn = ctk.CTkButton(
                scroll,
                text=v,
                anchor="w",
                height=38,
                corner_radius=12,
                fg_color="#1f2937" if is_selected else "#111827",
                hover_color="#374151",
                text_color="#e5e7eb",
                command=lambda vv=v: choose(vv),
            )
            btn.pack(fill="x", pady=6)

        self._popup.bind("<FocusOut>", lambda _e: self._close_popup())
        self._popup.bind("<Escape>", lambda _e: self._close_popup())
        self._popup.focus_force()

    def _close_popup(self):
        if self._popup is None:
            return
        try:
            if self._popup.winfo_exists():
                self._popup.destroy()
        except Exception:
            pass
        self._popup = None


# =============================
# Main App
# =============================

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title(APP_NAME)
        self.geometry("960x560")
        self.minsize(960, 560)

        os.makedirs(IMAGES_DIR, exist_ok=True)

        self.cfg = load_config()

        self.active_process: str | None = None
        self.last_applied_process: str | None = None
        self.stop_event = threading.Event()

        self._ctk_image = None
        self._tk_photo = None

        # dropdown mapping (display name -> proc_key)
        self._display_to_proc: dict[str, str] = {}
        self._proc_to_display: dict[str, str] = {}

        self._status_msg_after_id: str | None = None
        self._log_visible = False

        # Update state
        self._update_available = False
        self._update_download_url: str | None = None
        self._update_version: str | None = None

        # Merge found games once at startup
        self._merge_discovered_games_into_config()

        self._build_ui()
        self._refresh_game_list()

        # Start watcher
        self.watcher_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self.watcher_thread.start()

        # Update check at startup
        threading.Thread(target=self.check_updates_on_startup, daemon=True).start()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- UI ----------
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(self, corner_radius=16)
        left.grid(row=0, column=0, sticky="nsew", padx=18, pady=18)
        left.grid_columnconfigure(0, weight=1)

        right = ctk.CTkFrame(self, corner_radius=16)
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 18), pady=18)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(5, weight=1)

        title = ctk.CTkLabel(left, text="Auflösung pro Spiel", font=ctk.CTkFont(size=20, weight="bold"))
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(16, 6))

        # Scan/Manual buttons row (small)
        scan_row = ctk.CTkFrame(left, fg_color="transparent")
        scan_row.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 0))
        scan_row.grid_columnconfigure(0, weight=1)
        scan_row.grid_columnconfigure(1, weight=1)

        self.rescan_btn = ctk.CTkButton(
            scan_row,
            text="Neu scannen",
            fg_color="#334155",
            hover_color="#475569",
            corner_radius=12,
            command=self.rescan_games,
        )
        self.rescan_btn.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.add_btn = ctk.CTkButton(
            scan_row,
            text="Spiel hinzufügen (.exe)",
            fg_color="#334155",
            hover_color="#475569",
            corner_radius=12,
            command=self.add_game_manually,
        )
        self.add_btn.grid(row=0, column=1, sticky="ew")

        form = ctk.CTkFrame(left, corner_radius=14)
        form.grid(row=2, column=0, sticky="ew", padx=16, pady=(12, 14))
        form.grid_columnconfigure(1, weight=1)

        def row_label(text, r):
            lbl = ctk.CTkLabel(form, text=text)
            lbl.grid(row=r, column=0, sticky="w", padx=(14, 10), pady=10)

        row_label("Spiel", 0)
        self.game_var = tk.StringVar(value="")
        self.game_dropdown = AppDropdown(
            form,
            values=["(lädt...)"],
            variable=self.game_var,
            command=lambda _v: self._load_selected_game_into_fields(),
        )
        self.game_dropdown.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=10)

        row_label("Breite", 1)
        self.w_var = tk.StringVar(value=str(DEFAULT_WIDTH))
        self.w_entry = ctk.CTkEntry(form, textvariable=self.w_var)
        self.w_entry.grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=10)

        row_label("Höhe", 2)
        self.h_var = tk.StringVar(value=str(DEFAULT_HEIGHT))
        self.h_entry = ctk.CTkEntry(form, textvariable=self.h_var)
        self.h_entry.grid(row=2, column=1, sticky="ew", padx=(0, 14), pady=10)

        row_label("Hz", 3)
        self.r_var = tk.StringVar(value="144")
        self.r_entry = ctk.CTkEntry(form, textvariable=self.r_var)
        self.r_entry.grid(row=3, column=1, sticky="ew", padx=(0, 14), pady=10)

        row_label("Bild", 4)
        self.image_btn = ctk.CTkButton(
            form,
            text="Bild auswählen",
            fg_color="#334155",
            hover_color="#475569",
            corner_radius=12,
            command=self.choose_image_for_selected_game,
        )
        self.image_btn.grid(row=4, column=1, sticky="ew", padx=(0, 14), pady=10)

        buttons = ctk.CTkFrame(left, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 10))
        buttons.grid_columnconfigure(0, weight=1)
        buttons.grid_columnconfigure(1, weight=1)
        buttons.grid_columnconfigure(2, weight=1)

        self.save_btn = ctk.CTkButton(buttons, text="Speichern / Update", corner_radius=12, command=self.save_selected_game)
        self.save_btn.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.test_btn = ctk.CTkButton(
            buttons,
            text=f"Testen ({TEST_SECONDS}s)",
            fg_color="#334155",
            hover_color="#475569",
            corner_radius=12,
            command=self.test_resolution_7s,
        )
        self.test_btn.grid(row=0, column=1, sticky="ew", padx=(0, 10))

        self.restore_btn = ctk.CTkButton(
            buttons,
            text="Auf Standard zurück",
            fg_color="#334155",
            hover_color="#475569",
            corner_radius=12,
            command=self.set_fields_to_default_resolution,
        )
        self.restore_btn.grid(row=0, column=2, sticky="ew")

        self.status_msg = ctk.CTkLabel(left, text="", text_color="#93c5fd")
        self.status_msg.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 16))

        # Right panel header
        header = ctk.CTkFrame(right, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        header.grid_columnconfigure(0, weight=1)

        status_title = ctk.CTkLabel(header, text="Status", font=ctk.CTkFont(size=20, weight="bold"))
        status_title.grid(row=0, column=0, sticky="w")

        # Buttons on top-right: Update + Log
        top_btns = ctk.CTkFrame(header, fg_color="transparent")
        top_btns.grid(row=0, column=1, sticky="e")
        self.update_btn = ctk.CTkButton(
            top_btns,
            text="Update installieren",
            width=150,
            fg_color="#1d4ed8",
            hover_color="#1e40af",
            corner_radius=12,
            state="disabled",
            command=self.install_update,
        )
        self.update_btn.grid(row=0, column=0, padx=(0, 10))

        self.log_btn = ctk.CTkButton(
            top_btns,
            text="Log",
            width=70,
            fg_color="#334155",
            hover_color="#475569",
            corner_radius=12,
            command=self.toggle_log,
        )
        self.log_btn.grid(row=0, column=1)

        self.version_label = ctk.CTkLabel(right, text=f"Version: {APP_VERSION}", text_color="#b8c0d4")
        self.version_label.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 6))

        self.active_label = ctk.CTkLabel(right, text="Aktives Spiel: (keins)", text_color="#b8c0d4", justify="left")
        self.active_label.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 12))

        self.image_label = ctk.CTkLabel(right, text="Kein Bild", text_color="#b8c0d4")
        self.image_label.grid(row=4, column=0, sticky="nsew", padx=16, pady=(0, 10))

        self.log_box = ctk.CTkTextbox(right, corner_radius=12, height=160)
        self.log_box.grid(row=5, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self.log_box.configure(state="disabled")
        self.log_box.grid_remove()

    # ---------- Helpers ----------
    def _parse_int(self, s: str, field: str) -> int:
        try:
            v = int(str(s).strip())
            if v <= 0:
                raise ValueError
            return v
        except Exception:
            raise ValueError(f"Ungültiger Wert für {field}: '{s}'")

    def _set_status_msg(self, text: str, color: str = "#93c5fd"):
        if self._status_msg_after_id is not None:
            try:
                self.after_cancel(self._status_msg_after_id)
            except Exception:
                pass
            self._status_msg_after_id = None

        self.status_msg.configure(text=text, text_color=color)
        self._status_msg_after_id = self.after(int(STATUS_MSG_SECONDS * 1000), lambda: self.status_msg.configure(text=""))

    def log(self, text: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {text}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def toggle_log(self):
        self._log_visible = not self._log_visible
        if self._log_visible:
            self.log_box.grid()
            self.log_btn.configure(text="Log ✓")
        else:
            self.log_box.grid_remove()
            self.log_btn.configure(text="Log")

    def set_fields_to_default_resolution(self):
        self.w_var.set(str(DEFAULT_WIDTH))
        self.h_var.set(str(DEFAULT_HEIGHT))
        self._set_status_msg("Standard-Auflösung gesetzt: 1920×1080.")
        self.log("Felder auf Standard-Auflösung gesetzt (1920x1080).")

    # ---------- Discovery / Merge ----------
    def _merge_discovered_games_into_config(self) -> int:
        """
        Returns number of newly added games.
        """
        games = self.cfg.setdefault("games", {})
        custom_paths = self.cfg.get("custom_scan_paths", [])
        discovered = discover_all_games(custom_paths)

        added = 0
        for g in discovered:
            proc_key = (g.get("process") or "").lower()
            if not proc_key:
                continue
            if proc_key in games:
                # update exe_path/source if missing (but don't override user's res)
                if not games[proc_key].get("exe_path") and g.get("exe_path"):
                    games[proc_key]["exe_path"] = g.get("exe_path")
                if not games[proc_key].get("source") and g.get("source"):
                    games[proc_key]["source"] = g.get("source")
                continue

            games[proc_key] = {
                "name": g.get("name") or proc_key,
                "process": g.get("process") or proc_key,
                "width": DEFAULT_WIDTH,
                "height": DEFAULT_HEIGHT,
                "refresh_hz": 144,
                "image_path": os.path.join(IMAGES_DIR, f"{_safe_filename(proc_key)}.png"),
                "exe_path": g.get("exe_path"),
                "source": g.get("source") or "scan",
            }
            added += 1

        # enforce your presets (never overwrite user's custom changes if they edited later)
        if "cs2.exe" in games:
            games["cs2.exe"].setdefault("name", "Counter-Strike 2")
            if games["cs2.exe"].get("width") in (None, DEFAULT_WIDTH) and games["cs2.exe"].get("height") in (None, DEFAULT_HEIGHT):
                games["cs2.exe"]["width"] = 1440
                games["cs2.exe"]["height"] = 1080
        if "valorant-win64-shipping.exe" in games:
            games["valorant-win64-shipping.exe"].setdefault("name", "Valorant")
            if games["valorant-win64-shipping.exe"].get("width") in (None, DEFAULT_WIDTH) and games["valorant-win64-shipping.exe"].get("height") in (None, DEFAULT_HEIGHT):
                games["valorant-win64-shipping.exe"]["width"] = 1568
                games["valorant-win64-shipping.exe"]["height"] = 1080

        save_config(self.cfg)
        return added

    def rescan_games(self):
        self._set_status_msg("Scan läuft …")
        self.log("Neu scannen gestartet…")

        def worker():
            try:
                added = self._merge_discovered_games_into_config()
                self.after(0, lambda: self._refresh_game_list())
                self.after(0, lambda: self._set_status_msg(f"Scan fertig: +{added} Spiele."))
                self.after(0, lambda: self.log(f"Scan fertig: {added} neue Spiele hinzugefügt."))
            except Exception as e:
                self.after(0, lambda: self._set_status_msg("Scan fehlgeschlagen.", color="#fca5a5"))
                self.after(0, lambda: self.log(f"Scan Fehler: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def add_game_manually(self):
        path = filedialog.askopenfilename(
            title="Spiel-EXE auswählen",
            filetypes=[("Windows Programme", "*.exe"), ("Alle Dateien", "*.*")],
        )
        if not path:
            return
        if not path.lower().endswith(".exe") or not os.path.exists(path):
            self._set_status_msg("Ungültige EXE.", color="#fca5a5")
            return

        proc = os.path.basename(path)
        proc_key = proc.lower()
        name_guess = os.path.splitext(proc)[0]

        games = self.cfg.setdefault("games", {})
        if proc_key not in games:
            games[proc_key] = {
                "name": name_guess,
                "process": proc,
                "width": DEFAULT_WIDTH,
                "height": DEFAULT_HEIGHT,
                "refresh_hz": 144,
                "image_path": os.path.join(IMAGES_DIR, f"{_safe_filename(proc_key)}.png"),
                "exe_path": path,
                "source": "manual",
            }
            save_config(self.cfg)
            self._refresh_game_list()
            self.game_var.set(self._proc_to_display.get(proc_key, self.game_var.get()))
            self._load_selected_game_into_fields()
            self._set_status_msg("Spiel hinzugefügt.")
            self.log(f"Manuell hinzugefügt: {name_guess} ({proc})")
        else:
            # update exe_path if missing
            if not games[proc_key].get("exe_path"):
                games[proc_key]["exe_path"] = path
                games[proc_key]["source"] = games[proc_key].get("source") or "manual"
                save_config(self.cfg)
            self._set_status_msg("Spiel existiert bereits.")
            self.log(f"Manuell: schon vorhanden ({proc})")

    # ---------- Dropdown mapping ----------
    def _refresh_game_list(self):
        games = self.cfg.get("games", {})
        self._display_to_proc.clear()
        self._proc_to_display.clear()

        name_counts: dict[str, int] = {}
        display_values: list[str] = []

        for proc_key, g in games.items():
            base = str(g.get("name", proc_key)).strip() or proc_key
            n = name_counts.get(base, 0) + 1
            name_counts[base] = n
            display = base if n == 1 else f"{base} #{n}"

            self._display_to_proc[display] = proc_key.lower()
            self._proc_to_display[proc_key.lower()] = display
            display_values.append(display)

        display_values.sort(key=str.lower)
        if not display_values:
            display_values = ["(keine Spiele gefunden)"]

        self.game_dropdown.set_values(display_values)
        if not self.game_var.get() or self.game_var.get().startswith("("):
            self.game_var.set(display_values[0])

        self._load_selected_game_into_fields()

    def _selected_process_key(self) -> str | None:
        sel = self.game_var.get().strip()
        if not sel or sel.startswith("("):
            return None
        return self._display_to_proc.get(sel)

    def _load_selected_game_into_fields(self):
        proc_key = self._selected_process_key()
        if not proc_key:
            return
        g = self.cfg.get("games", {}).get(proc_key)
        if not g:
            return

        self.w_var.set(str(g.get("width", DEFAULT_WIDTH)))
        self.h_var.set(str(g.get("height", DEFAULT_HEIGHT)))
        self.r_var.set(str(g.get("refresh_hz", 0)))

        self._update_preview_image(str(g.get("image_path", "")).strip())

    # ---------- Image ----------
    def _update_preview_image(self, path: str):
        path = (path or "").strip()
        if not path or not os.path.exists(path):
            self._ctk_image = None
            self._tk_photo = None
            self.image_label.configure(text="Kein Bild", image=None)
            return

        if Image is not None:
            try:
                img = Image.open(path)
                self._ctk_image = ctk.CTkImage(light_image=img, dark_image=img, size=(360, 200))
                self._tk_photo = None
                self.image_label.configure(text="", image=self._ctk_image)
                return
            except Exception:
                pass

        try:
            self._tk_photo = tk.PhotoImage(file=path)
            self._ctk_image = None
            self.image_label.configure(text="", image=self._tk_photo)
        except Exception:
            self._ctk_image = None
            self._tk_photo = None
            self.image_label.configure(text="Bild konnte nicht geladen werden", image=None)

    def choose_image_for_selected_game(self):
        proc_key = self._selected_process_key()
        if not proc_key:
            self._set_status_msg("Bitte ein Spiel auswählen.", color="#fca5a5")
            return

        path = filedialog.askopenfilename(
            title="Bild auswählen",
            filetypes=[("Bilder", "*.png;*.jpg;*.jpeg;*.webp;*.bmp"), ("Alle Dateien", "*.*")],
        )
        if not path:
            return

        os.makedirs(IMAGES_DIR, exist_ok=True)
        ext = os.path.splitext(path)[1].lower() or ".png"
        target = os.path.join(IMAGES_DIR, f"{_safe_filename(proc_key)}{ext}")

        try:
            shutil.copy2(path, target)
        except Exception as e:
            self._set_status_msg("Bild konnte nicht übernommen werden.", color="#fca5a5")
            self.log(f"Bild kopieren fehlgeschlagen: {e}")
            return

        self.cfg["games"][proc_key]["image_path"] = target
        save_config(self.cfg)

        self._update_preview_image(target)
        self._set_status_msg("Bild geändert.")
        self.log(f"Bild geändert: {self.cfg['games'][proc_key].get('name', proc_key)}")

    # ---------- Save / Test ----------
    def save_selected_game(self):
        proc_key = self._selected_process_key()
        if not proc_key:
            self._set_status_msg("Bitte ein Spiel auswählen.", color="#fca5a5")
            return

        games = self.cfg.setdefault("games", {})
        if proc_key not in games:
            games[proc_key] = {"name": proc_key, "process": proc_key}

        try:
            w = self._parse_int(self.w_var.get(), "Breite")
            h = self._parse_int(self.h_var.get(), "Höhe")
            r = int(str(self.r_var.get()).strip() or "0")
            if r < 0:
                raise ValueError
        except ValueError as e:
            self._set_status_msg(str(e), color="#fca5a5")
            return

        g = games[proc_key]
        g["width"] = w
        g["height"] = h
        g["refresh_hz"] = r

        save_config(self.cfg)
        self._set_status_msg("Gespeichert.")
        self.log(f"Gespeichert: {g.get('name', proc_key)} -> {w}x{h}" + (f" @{r}Hz" if r else ""))

    def test_resolution_7s(self):
        try:
            w = self._parse_int(self.w_var.get(), "Breite")
            h = self._parse_int(self.h_var.get(), "Höhe")
            r = int(str(self.r_var.get()).strip() or "0")
        except ValueError as e:
            self._set_status_msg(str(e), color="#fca5a5")
            return

        proc_key = self._selected_process_key()
        game_name = self.cfg.get("games", {}).get(proc_key, {}).get("name", "Spiel") if proc_key else "Spiel"

        def worker():
            ok, msg = set_resolution(w, h, r if r > 0 else None)
            if not ok:
                self.after(0, lambda: self._set_status_msg(msg, color="#fca5a5"))
                self.after(0, lambda: self.log(f"Test fehlgeschlagen: {msg}"))
                return

            self.after(0, lambda: self._set_status_msg(f"Teste {game_name}: {w}×{h} für {TEST_SECONDS}s …"))
            self.after(0, lambda: self.log(f"Test gestartet: {game_name} -> {w}x{h}" + (f" @{r}Hz" if r else "")))

            time.sleep(TEST_SECONDS)
            set_resolution(DEFAULT_WIDTH, DEFAULT_HEIGHT, None)

            self.after(0, lambda: self._set_status_msg("Test beendet: zurück auf 1920×1080."))
            self.after(0, lambda: self.log("Test beendet: zurück auf 1920x1080."))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- Auto switching ----------
    def _find_active_game(self, running: set[str]) -> str | None:
        games = self.cfg.get("games", {})
        for proc_key, g in games.items():
            proc = str(g.get("process", proc_key)).lower()
            if proc in running:
                return proc_key.lower()
        return None

    def _apply_game_resolution_if_needed(self, proc_key: str | None):
        if proc_key == self.last_applied_process:
            return

        if proc_key is None:
            if self.last_applied_process is not None:
                set_resolution(DEFAULT_WIDTH, DEFAULT_HEIGHT, None)
                self.log("Kein Spiel aktiv: zurück auf 1920x1080.")
            self.last_applied_process = None
            return

        g = self.cfg.get("games", {}).get(proc_key)
        if not g:
            return

        w = int(g.get("width", DEFAULT_WIDTH))
        h = int(g.get("height", DEFAULT_HEIGHT))
        r = int(g.get("refresh_hz", 0))
        set_resolution(w, h, r if r > 0 else None)
        self.last_applied_process = proc_key
        self.log(f"Aktiv erkannt: {g.get('name', proc_key)} -> {w}x{h}" + (f" @{r}Hz" if r else ""))

    def _ui_set_active(self, proc_key: str | None):
        if proc_key is None:
            self.active_label.configure(text="Aktives Spiel: (keins)")
            self._update_preview_image("")
            return

        g = self.cfg.get("games", {}).get(proc_key, {})
        name = g.get("name", proc_key)
        self.active_label.configure(text=f"Aktives Spiel: {name}")
        self._update_preview_image(g.get("image_path", ""))

    def _watch_loop(self):
        while not self.stop_event.is_set():
            running = list_running_process_names_lower()
            proc_key = self._find_active_game(running)

            self._apply_game_resolution_if_needed(proc_key)

            if proc_key != self.active_process:
                self.active_process = proc_key
                self.after(0, lambda pk=proc_key: self._ui_set_active(pk))

            time.sleep(1.0)

    # ---------- Updates ----------
    def check_updates_on_startup(self):
        self.log("Update-Check gestartet…")
        try:
            req = urllib.request.Request(
                GITHUB_LATEST_RELEASE_API,
                headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)

            tag = str(data.get("tag_name") or "").strip()
            if not tag:
                self.log("Update-Check: kein tag_name gefunden.")
                return

            assets = data.get("assets") or []
            dl_url = None
            for a in assets:
                if str(a.get("name") or "") == UPDATE_ASSET_NAME:
                    dl_url = a.get("browser_download_url")
                    break

            if not dl_url:
                self.log(f"Update-Check: Asset nicht gefunden ({UPDATE_ASSET_NAME}).")
                return

            if is_newer_version(tag, APP_VERSION):
                self._update_available = True
                self._update_download_url = str(dl_url)
                self._update_version = tag
                self.after(0, lambda: self.update_btn.configure(state="normal"))
                self.after(0, lambda: self._set_status_msg(f"Update verfügbar: {tag}"))
                self.log(f"Update verfügbar: {tag}")
            else:
                self.log("Kein Update verfügbar.")
        except Exception as e:
            self.log(f"Update-Check fehlgeschlagen: {e}")

    def install_update(self):
        if not self._update_available or not self._update_download_url:
            return

        version = self._update_version or "(unbekannt)"
        self._set_status_msg(f"Lade Update {version} …")
        self.log(f"Update installieren: Download gestartet ({version})")

        def worker():
            try:
                url = self._update_download_url
                tmp_dir = tempfile.gettempdir()
                out_path = os.path.join(tmp_dir, UPDATE_ASSET_NAME)

                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=20) as resp, open(out_path, "wb") as f:
                    shutil.copyfileobj(resp, f)

                self.after(0, lambda: self._set_status_msg("Update heruntergeladen. Installer startet…"))
                self.log(f"Update heruntergeladen: {out_path}")

                # Start installer and exit app
                subprocess.Popen([out_path], shell=False)
                self.after(500, self.on_close)
            except Exception as e:
                self.after(0, lambda: self._set_status_msg("Update fehlgeschlagen.", color="#fca5a5"))
                self.log(f"Update-Install Fehler: {e}")

        threading.Thread(target=worker, daemon=True).start()

    # ---------- Shutdown ----------
    def on_close(self):
        self.stop_event.set()
        try:
            set_resolution(DEFAULT_WIDTH, DEFAULT_HEIGHT, None)
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    os.makedirs(IMAGES_DIR, exist_ok=True)
    App().mainloop()