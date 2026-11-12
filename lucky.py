"""LuckyLook: the popup (Ctrl+Alt+L), the tab at the top of the screen and the app window, drawn by Windows' own web
view (WebView2, through pywebview) as Apple-style frosted glass. It also keeps the index of the drives up to date.

Run:  pythonw lucky.py          (no console window; errors go to lucky.log)
      pythonw lucky.py --app    (opens the app window; starts LuckyLook first if it isn't running)
"""
import base64
import ctypes
import html
import json
import os
import queue
import re
import shutil
import sqlite3
import string
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).parent
if sys.stderr is None:  # pythonw has no console
    sys.stdout = sys.stderr = open(HERE / "lucky.log", "a", encoding="utf-8", buffering=1)

import webview  # noqa: E402

import agent  # noqa: E402
import indexer  # noqa: E402

MOD_NOREPEAT, WM_HOTKEY = 0x4000, 0x312
HOTKEY = "Ctrl+Alt+L"  # change it from the tray menu (saved in local_settings.py)
CHAT = HERE / "chat.json"  # the popup's conversation, so it's still there after a restart
CHATS = HERE / "chats"  # the app's conversations, one file each
REINDEX_HOURS = 6  # a full pass of all drives this often (and from the tray menu: Reindex now)
LIVE = False  # True: also watch for changes, so a file you save, rename or delete shows up within LIVE_SECONDS.
LIVE_SECONDS = 5  # Costs a little CPU all the time, noticeable on battery.
IDLE_MINUTES = 2  # pictures get described (graphics card busy) only after this long without mouse or keyboard
KEEP_HISTORY = 12  # messages of earlier conversation sent along with each question
WIDGET = True  # tab hanging off the top of the screen: hover pulls it down, click opens the popup (/widget hides it)
PEEK = 5  # pixels of the tab left on screen while it's tucked away
POPUP = (860, 600)  # width, height (at 100% screen scale)
APP = (1180, 760)
TAB = (168, 46)
FROST_SECONDS = 1  # how often the popup's glass catches up with what's behind it while it's open
FROST_K = 16  # the glass is the screen behind, shrunk this many times; the page enlarges and blurs it
SEND_SECONDS = 0.08  # streamed text goes to the window this often
EDGE = 0x00423C3C  # hairline border, 0x00BBGGRR
SCREENSHOTS = os.environ.get("LUCKYLOOK_SCREENSHOTS", "")  # for README pictures: windows show up in screenshots, and
# the next window to open asks this question by itself ("1": none)
# a path in backticks (may contain spaces), or a bare path (no spaces)
PATH_RE = re.compile(r"`([A-Za-z]:\\[^`\n]*)`|\b([A-Za-z]:\\[^\s`\"'<>|*?]*)")

try:
    from local_settings import *  # noqa: F401,F403  your own settings, never uploaded
except ImportError:
    pass

u32 = ctypes.windll.user32
u32.GetWindowLongPtrW.restype = u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
OPEN_APP = u32.RegisterWindowMessageW("LuckyLookOpenApp")  # from a second `lucky.py --app`: open the app window
TOGGLE = u32.RegisterWindowMessageW("LuckyLookToggle")  # from a second `lucky.py`: open the popup


def parse_hotkey(text):
    """"Ctrl+Alt+L" -> (modifier flags, virtual key) for RegisterHotKey. Keys: A-Z, 0-9, F1-F24, Space."""
    *mods, key = text.lower().split("+")
    if re.fullmatch(r"f([1-9]|1\d|2[0-4])", key):
        vk = 0x6F + int(key[1:])
    elif key == "space":
        vk = 0x20
    elif re.fullmatch(r"[a-z0-9]", key):
        vk = ord(key.upper())
    else:
        raise ValueError(f"can't use {text!r} as a hotkey")
    return sum({"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}[m] for m in mods), vk


# ---------- the answer as HTML ----------

def esc(s):
    return html.escape(s, quote=True)


def chip(path, live):
    """A file or folder in an answer: a chip that opens it (programs only show in Explorer, they never run)."""
    if live or not os.path.exists(path):  # while streaming, no disk checks yet
        return f'<span class="path">{esc(path)}</span>'
    p = Path(path)
    kind = "folder" if p.is_dir() else KINDS.get(p.suffix.lower(), "file")
    return (f'<span class="chip" data-path="{esc(path)}" title="{esc(path)}"><i class="ic ic-{kind}"></i>'
            f'<span class="name">{esc(p.name or path)}</span><span class="where">{esc(str(p.parent))}</span>'
            f'<b class="reveal" title="Show in Explorer">⋯</b></span>')


KINDS = {**dict.fromkeys(".pdf .doc .docx .rtf .odt .txt .md".split(), "doc"),
         **dict.fromkeys(".xls .xlsx .csv .ods".split(), "sheet"),
         **dict.fromkeys(".ppt .pptx .odp".split(), "slides"),
         **dict.fromkeys(".png .jpg .jpeg .gif .webp .bmp .heic .svg".split(), "image"),
         **dict.fromkeys(".mp3 .wav .flac .m4a .mp4 .mkv .mov .avi".split(), "media"),
         **dict.fromkeys(".py .js .ts .html .css .json .c .cpp .cs .java .go .rs .ps1 .bat .sh .yml .toml".split(), "code"),
         **dict.fromkeys(".zip .7z .rar .tar .gz".split(), "zip"),
         **dict.fromkeys(".exe .msi .lnk".split(), "app")}
INLINE = re.compile(r"`([^`\n]+)`|\*\*(.+?)\*\*|(https?://[^\s<>`)\]]+)")


def inline(s, live):
    out, pos = [], 0
    for m in INLINE.finditer(s):
        out.append(bare(s[pos:m.start()], live))
        pos = m.end()
        if m[1] is not None:
            out.append(chip(m[1], live) if re.match(r"[A-Za-z]:\\", m[1]) else f"<code>{esc(m[1])}</code>")
        elif m[2] is not None:
            out.append(f"<b>{inline(m[2], live)}</b>")
        else:
            out.append(f'<a href="#" data-url="{esc(m[3])}">{esc(m[3])}</a>')
    return "".join(out) + bare(s[pos:], live)


def bare(s, live):
    """Text with bare paths (no backticks) made into chips, the rest escaped."""
    out, pos = [], 0
    for path, at in find_paths(s):
        out.append(esc(s[pos:at]))
        out.append(chip(path, live))
        pos = at + len(path)
    return "".join(out) + esc(s[pos:])


def markdown(text, live=False):
    """The model's light markdown as safe HTML: everything is escaped first, then styled: **bold**, `code`, ``` blocks,
    # headings, - and 1. lists, | tables |, links, and paths (live: still streaming, so no disk checks)."""
    parts = re.split(r"^```.*$\n?", text, flags=re.M)  # odd parts are code (an unclosed block runs to the end)
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f'<div class="code"><button class="copy">Copy</button><pre><code>{esc(part.rstrip())}</code></pre></div>')
            continue
        lst, rows = None, []
        for line in part.split("\n") + [""]:
            if rows and not line.lstrip().startswith("|"):
                out.append(table(rows, live))
                rows = []
            m = re.match(r"\s*(?:([-*•])|(\d+)[.)])\s+(.*)", line)
            kind = ("ul" if m[1] else "ol") if m else None
            if lst and kind != lst:
                out.append(f"</{lst}>")
                lst = None
            if line.lstrip().startswith("|"):
                rows.append(line)
            elif m:
                if not lst:
                    out.append(f"<{kind}>")
                    lst = kind
                out.append(f"<li>{inline(m[3], live)}</li>")
            elif re.fullmatch(r"\s*(?:---+|\*\*\*+|___+)\s*", line):
                out.append("<hr>")
            elif h := re.match(r"#{1,4}\s+(.*)", line):
                out.append(f"<h4>{inline(h[1], live)}</h4>")
            elif line.strip():
                out.append(f"<p>{inline(line, live)}</p>")
        if lst:
            out.append(f"</{lst}>")
    return "".join(out)


def table(rows, live):
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    cells = [r for r in cells if not all(re.fullmatch(r":?-{2,}:?", c) for c in r)]  # the |---| line
    head, *body = cells or [[]]
    return ("<table><tr>" + "".join(f"<th>{inline(c, live)}</th>" for c in head) + "</tr>" +
            "".join("<tr>" + "".join(f"<td>{inline(c, live)}</td>" for c in r) + "</tr>" for r in body) + "</table>")


def find_paths(text):
    """Yields (path, offset) for Windows paths in text."""
    for m in PATH_RE.finditer(text):
        if m[1]:
            yield m[1].strip(), m.start(1)
        else:
            yield m[2].rstrip(".,;:)]"), m.start(2)


STEPS = {  # what each tool is doing, in plain words, and which argument to show with it
    "search_content": ("Searching inside your files", "query"), "search_meaning": ("Searching by meaning", "query"),
    "find_files": ("Looking for files", "name"), "find_folders": ("Looking for folders", "name"),
    "read_file": ("Reading", "path"), "list_dir": ("Looking in", "path"), "system_info": ("Checking", "kind"),
    "recent_files": ("Checking recent files", ""), "look_at_image": ("Looking at the picture", "path"),
    "look_at_screen": ("Looking at your screen", ""), "clipboard": ("Reading the clipboard", ""),
    "browser_history": ("Searching your browsing history", "search"), "remember": ("Remembering", "fact"),
    "remind_me": ("Setting a reminder", "text"), "open_item": ("Opening", "path"), "open_app": ("Starting", "name"),
    "open_website": ("Opening in Edge", "search"), "close_app": ("Closing", "name"), "move_item": ("Moving", "path"),
    "create_folder": ("Creating a folder", "path"), "write_file": ("Writing", "path"), "edit_file": ("Editing", "path"),
    "run_command": ("Running a command", "command"), "recycle_item": ("Sending to the Recycle Bin", "path"),
}
SEARCHES = {"search_content", "search_meaning", "find_files", "find_folders", "list_dir", "recent_files", "browser_history"}


def step_text(name, args):
    label, key = STEPS.get(name, (name.replace("_", " ").capitalize(), ""))
    detail = str(args.get(key) or args.get("url") or "") if key else ""
    if key == "path" and detail:
        detail = Path(detail).name or detail
    return label, detail[:80]


def result_text(name, out):
    """A tool's output in a few words, for its step: "12 found", or its first line."""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if name in SEARCHES:
        return "nothing found" if not lines or lines[0].startswith("nothing") else f"{len(lines)} found"
    if out.startswith(("refused", "NOT DONE")):
        return "not done"
    if out.startswith(("error", "doesn't", "couldn't", "nothing", "only")):
        return lines[0][:60]
    return "done"


# ---------- chats ----------

def chat_file(cid):
    if not re.fullmatch(r"[0-9a-f]{12}", cid or ""):
        raise ValueError("bad chat id")
    return CHATS / f"{cid}.json"


def list_chats():
    out = []
    for f in sorted(CHATS.glob("*.json"), key=lambda f: -f.stat().st_mtime) if CHATS.exists() else []:
        try:
            out.append({"id": f.stem, "title": json.loads(f.read_text(encoding="utf-8"))["title"], "when": f.stat().st_mtime})
        except (OSError, ValueError, KeyError):
            pass
    return out


def turns(messages):
    """Earlier messages as the page shows them: [{q, html}]."""
    return [{"q": q["content"], "html": markdown(a["content"])} for q, a in zip(messages[::2], messages[1::2])]


# ---------- stats for the info strip and the At a glance panel ----------

class FILETIME(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


def cpu_times():
    idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    t = [(f.high << 32) | f.low for f in (idle, kernel, user)]
    return t[0], t[1] + t[2]  # kernel time includes idle


def ollama_state():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=1) as r:
            models = json.load(r)["models"]
    except OSError:
        return {"state": "off", "name": agent.MODEL}
    m = next((m for m in models if m["name"].startswith(agent.MODEL)), None)
    return {"state": "ready" if m else "asleep", "name": agent.MODEL,
            "vram": round(m["size_vram"] / 1e9, 1) if m else 0, "size": round(m["size"] / 1e9, 1) if m else 0}


def index_counts():
    """The slow numbers (about a second on a big index): files, documents, by meaning, pictures."""
    with closing_ro() as con:
        pics = tuple(indexer.IMAGE_EXT)
        q = f"select count(*) from files where ext in ({','.join('?' * len(pics))})"
        return {"files": con.execute("select count(*) from files").fetchone()[0],
                "meaning": con.execute("select count(*) from vectors").fetchone()[0],
                "pictures": con.execute(q, pics).fetchone()[0],
                "described": con.execute(f"select count(*) from files f cross join content c on c.rowid = f.id "
                                         f"where f.ext in ({','.join('?' * len(pics))}) and c.name <> ''", pics).fetchone()[0]}


class closing_ro:
    def __enter__(self):
        self.con = sqlite3.connect(f"file:{indexer.DB}?mode=ro", uri=True, timeout=2)
        return self.con

    def __exit__(self, *a):
        self.con.close()


def reminders():
    try:
        items = json.loads(agent.REMINDERS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted(items, key=lambda r: r["at"])[:5]


def drives():
    out = []
    for d in string.ascii_uppercase:
        if ctypes.windll.kernel32.GetDriveTypeW(f"{d}:\\") == 3:  # fixed drives
            try:
                u = shutil.disk_usage(f"{d}:\\")
                out.append({"name": f"{d}:", "free": round(u.free / 1e9), "total": round(u.total / 1e9),
                            "pct": round(100 * u.used / u.total)})
            except OSError:
                pass
    return out


# ---------- the glass ----------

def behind(x, y, w, h):
    """A tiny (w/FROST_K by h/FROST_K) average of the screen at x, y, w, h, as a BMP: the page enlarges and blurs it into
    frosted glass. LuckyLook's own windows are left out of screen captures, so this sees what's behind them."""
    g, H = ctypes.windll.gdi32, ctypes.c_void_p
    for f in (u32.GetDC, g.CreateCompatibleDC, g.CreateCompatibleBitmap, g.SelectObject):
        f.restype = H
    sw, sh = max(w // FROST_K, 1), max(h // FROST_K, 1)
    screen = H(u32.GetDC(None))
    dc = H(g.CreateCompatibleDC(screen))
    bmp = H(g.CreateCompatibleBitmap(screen, sw, sh))
    g.SelectObject(dc, bmp)
    g.SetStretchBltMode(dc, 4)  # HALFTONE: average the pixels, don't just drop them
    g.StretchBlt(dc, 0, 0, sw, sh, screen, x, y, w, h, 0xCC0020)  # SRCCOPY
    g.DeleteDC(dc)  # GetDIBits wants the bitmap out of any DC
    head = struct.pack("<IiiHHIIiiII", 40, sw, sh, 1, 24, 0, 0, 0, 0, 0, 0)
    pixels = ctypes.create_string_buffer(((sw * 3 + 3) & ~3) * sh)  # rows padded to 4 bytes, bottom up: BMP's own layout
    g.GetDIBits(screen, bmp, 0, sh, pixels, head, 0)
    g.DeleteObject(bmp)
    u32.ReleaseDC(None, screen)
    return b"BM" + struct.pack("<IHHI", 54 + len(pixels.raw), 0, 0, 54) + head + pixels.raw


def hwnd(window):
    return int(window.native.Handle.ToInt64())


def rect(h):
    r = wintypes.RECT()
    u32.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def dress(window, taskbar=False, private=True):
    """Rounded corners and a hairline border; no taskbar button unless asked; left out of screenshots (so screen
    questions and the frost see what's behind it, and nobody's screen share shows it)."""
    h = hwnd(window)
    if not taskbar:
        ex = u32.GetWindowLongPtrW(h, -20)
        u32.SetWindowLongPtrW(h, -20, (ex | 0x80) & ~0x40000)  # WS_EX_TOOLWINDOW, not WS_EX_APPWINDOW
    dwm = ctypes.windll.dwmapi
    dwm.DwmSetWindowAttribute(h, 33, ctypes.byref(ctypes.c_int(2)), 4)  # rounded corners (Windows 11)
    dwm.DwmSetWindowAttribute(h, 34, ctypes.byref(ctypes.c_int(EDGE)), 4)  # border colour
    if private and not SCREENSHOTS:
        u32.SetWindowDisplayAffinity(h, 0x11)  # WDA_EXCLUDEFROMCAPTURE
    return h


def scale():
    return u32.GetDpiForSystem() / 96


def place(h, x, y, w, ht):
    """Moves and sizes a window in real pixels without showing, activating or raising it (pywebview's own move and
    resize also show the window, which would pop up the hidden popup)."""
    u32.SetWindowPos(h, None, x, y, w, ht, 0x0214)  # SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER


def page(name):
    """ui/<name>.html with the shared style, script and logo put inline (no web server, nothing from the internet)."""
    ui = HERE / "ui"
    text = (ui / f"{name}.html").read_text(encoding="utf-8")
    logo = base64.b64encode((HERE / "assets" / "logo-34.png").read_bytes()).decode()
    return (text.replace('<link rel="stylesheet" href="style.css">', f"<style>{(ui / 'style.css').read_text(encoding='utf-8')}</style>")
            .replace('<script src="chat.js"></script>', f"<script>{(ui / 'chat.js').read_text(encoding='utf-8')}</script>")
            .replace("LOGO.png", f"data:image/png;base64,{logo}"))


class Lucky:
    def __init__(self):
        self.q = queue.Queue()  # from the hotkey, tray and index threads: things to do
        self.reindex = threading.Event()  # Reindex now, from the tray or the app
        self.tray = None
        self.answering = threading.Lock()  # one answer at a time: the actions' Allow/Deny is shared
        self.stop = threading.Event()
        self.waiting = {}  # Allow/Deny cards waiting for a click: id -> (event, [answer])
        self.status, self.status_at = "", 0.0
        self.cpu_last = cpu_times()
        self.counts, self.counts_at = {}, 0.0
        self.widget_on, self.tab_gone, self.tab_at, self.tab_goal = WIDGET, False, None, None
        self.frost_last = {}
        try:  # the popup's conversation from before the last restart
            self.history = json.loads(CHAT.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.history = []
        self.app, self.app_hwnd, self.shown = None, None, False
        self.api = api = Api(self)
        self.popup = webview.create_window("LuckyLook", html=page("popup"), js_api=api, width=POPUP[0], height=POPUP[1],
                                           hidden=True, frameless=True, easy_drag=False, on_top=True,
                                           background_color="#1c1c1e")
        self.tab = webview.create_window("LuckyLook tab", html=page("tab"), js_api=api, width=TAB[0], height=TAB[1],
                                         x=-2000, y=-2000, frameless=True, easy_drag=False, on_top=True, focus=False,
                                         background_color="#0a84ff", min_size=(20, 10))

    # --- startup (pywebview calls this on its own thread once the windows exist) ---

    def started(self):
        for w in (self.popup, self.tab):
            w.events.loaded.wait(20)  # its native window and page exist from here on
        dress(self.popup)
        dress(self.tab, private=False)
        for loop in (self.hotkey_loop, self.pump, self.remind_loop, self.tab_loop, self.frost_loop):
            threading.Thread(target=loop, daemon=True).start()
        if "--app" in sys.argv:
            self.q.put(("app", None))

    # --- background threads ---

    def hotkey_loop(self):
        mods, vk = parse_hotkey(HOTKEY)
        for _ in range(10):  # after a restart, the old LuckyLook may need a moment to let go of it
            if u32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
                break
            time.sleep(0.5)
        else:
            self.q.put(("fatal", f"{HOTKEY} is already in use (LuckyLook already running, or another app). "
                                 "Set another one with HOTKEY in local_settings.py."))
            return
        threading.Thread(target=self.index_loop, daemon=True).start()
        self.tray = Tray(self.q)
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.q.put(("toggle", None))
            else:
                u32.DispatchMessageW(ctypes.byref(msg))  # tray clicks

    def index_loop(self):
        """A full pass every REINDEX_HOURS, counted from the last one (so a restart doesn't redo it), and whenever
        Reindex now is clicked. With LIVE, file changes in between reach the index within LIVE_SECONDS."""
        changes = indexer.Watcher(indexer.ROOTS) if LIVE else None  # before the pass, so changes during it count
        next_full = indexer.last_full() + REINDEX_HOURS * 3600
        meaning = pictures = True  # documents may need meaning vectors, pictures descriptions (each says when done)
        meaning_retry = picture_retry = 0
        while True:
            if self.reindex.is_set() or time.time() >= next_full:
                self.reindex.clear()
                meaning = pictures = True
                next_full = time.time() + REINDEX_HOURS * 3600
                try:
                    indexer.index(log=lambda m: self.q.put(("status", m)))
                except Exception as e:  # e.g. indexer.py is running by hand right now
                    print(f"index error: {e}", flush=True)  # lands in lucky.log
                    self.q.put(("status", f"index error: {e} (retrying in 10 min)"))
                    next_full = time.time() + 600
            if self.reindex.wait(LIVE_SECONDS):
                continue
            if changes:
                paths = changes.take()
                if paths is None:  # Windows dropped some changes: only a full pass can catch up
                    next_full = 0
                    continue
                if paths:
                    try:
                        indexer.update(paths, log=lambda m: None)
                        meaning = pictures = True
                    except RuntimeError:  # indexer.py running by hand: apply these when it's done
                        changes.give_back(paths)
                    except Exception as e:
                        print(f"live index error: {e}", flush=True)
            if meaning and time.time() >= meaning_retry:
                try:
                    meaning = indexer.embed_some() > 0
                except RuntimeError:  # index busy (by hand): next time
                    pass
                except Exception as e:  # e.g. Ollama busy or restarting, or the model isn't downloaded yet
                    print(f"meaning index error: {e} (retrying in a minute)", flush=True)
                    meaning_retry = time.time() + 60
            elif pictures and time.time() >= picture_retry and idle() and plugged_in():
                try:
                    pictures = indexer.describe_some(agent.look_at_image) > 0
                    meaning = True  # the new descriptions get meaning vectors too
                except RuntimeError:  # index busy (by hand): next time
                    pass
                except Exception as e:  # e.g. Ollama not running
                    print(f"picture index error: {e} (retrying in 10 minutes)", flush=True)
                    picture_retry = time.time() + 600

    def pump(self):
        """Carries out what the other threads ask for, one at a time."""
        while True:
            kind, val = self.q.get()
            try:
                if kind == "toggle":
                    self.toggle()
                elif kind == "widget":
                    self.widget_on = not self.widget_on  # tab_loop moves the tab within a second
                elif kind == "app":
                    self.open_app()
                elif kind == "quit":
                    self.quit()
                    return
                elif kind == "status":
                    self.status, self.status_at = val, time.time()
                elif kind in SETTINGS:
                    SETTINGS[kind](self)
                elif kind == "fatal":
                    u32.MessageBoxW(None, val, "LuckyLook", 0x10)
                    self.quit()
                    return
            except Exception as e:
                print(f"{kind}: {e!r}", flush=True)

    def remind_loop(self):
        """Every 15 s: reminders that are due (remind_me) pop up as notifications."""
        while True:
            time.sleep(15)
            if self.tray:
                for text in agent.due_reminders():
                    self.tray.notify("LuckyLook reminder", text)

    def tab_loop(self):
        """Every second: while a full-screen app (video, game, slides) is in front, or after /widget, the tab goes
        all the way up out of sight. (Moved, not hidden: showing a window again can steal the keyboard.)"""
        state = ctypes.c_int()
        while True:
            ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
            gone = not self.widget_on or state.value in (2, 3, 4)  # full-screen app, full-screen game, presentation
            if gone != self.tab_gone or self.tab_at is None:
                self.tab_gone = gone
                self.pull_tab(False)
            time.sleep(1)

    def frost_loop(self):
        """Keeps the glass in step with what's behind: the popup's every FROST_SECONDS while it's open, the app's when
        it comes to the front (and when it opens or moves). Re-blurring a whole big window often costs real CPU."""
        front = None
        while not SCREENSHOTS:  # (then a capture would see the window itself: the glass is set only as it opens)
            time.sleep(FROST_SECONDS)
            if self.shown:
                self.frost(self.popup)
            now = u32.GetForegroundWindow()
            if self.app and now == self.app_hwnd != front:
                self.frost(self.app)
            front = now

    def frost(self, window, at=None):
        try:
            h = hwnd(window)
            data = behind(*(at or rect(h)))
        except Exception:
            return  # e.g. the window was just closed
        if self.frost_last.get(h) != data:
            self.frost_last[h] = data
            window.run_js(f"LL.frost('data:image/bmp;base64,{base64.b64encode(data).decode()}')")

    # --- windows ---

    def toggle(self):
        if self.shown:
            self.popup.hide()
            self.shown = False
            self.popup.run_js("LL.hidden()")
            return
        s = scale()
        box = screen_box(round(POPUP[0] * s), round(POPUP[1] * s))
        self.frost(self.popup, box)  # before it shows, so the glass is right at once
        place(hwnd(self.popup), *box)
        self.popup.show()
        self.shown = True
        self.focus(self.popup)
        self.popup.run_js("LL.shown()")
        self.screenshot_question(self.popup)

    def focus(self, window):
        h = hwnd(window)
        u32.keybd_event(0x12, 0, 0, 0)  # an Alt tap: lets this process bring its window to the front
        u32.keybd_event(0x12, 0, 2, 0)
        u32.SetForegroundWindow(h)

    def open_app(self):
        if self.app:
            self.app.restore()
            self.app.show()
            self.focus(self.app)
            return
        s = scale()
        area = wintypes.RECT()
        u32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)  # screen minus taskbar
        w = min(APP[0], int((area.right - area.left) / s * 0.92))
        h = min(APP[1], int((area.bottom - area.top) / s * 0.92))
        x = round((area.left + area.right) / 2 / s - w / 2)
        y = round((area.top + area.bottom) / 2 / s - h / 2)
        self.app_box = tuple(round(v * s) for v in (x, y, w, h))
        self.app = webview.create_window("LuckyLook app", html=page("app"), js_api=self.api, width=w, height=h, x=x,
                                         y=y, frameless=True, easy_drag=False, background_color="#1c1c1e",
                                         min_size=(760, 520), hidden=True)
        self.app.events.loaded += self.app_loaded
        self.app.events.moved += lambda *a: self.frost(self.app) if self.app else None
        self.app.events.closed += self.app_closed

    def app_loaded(self):
        self.app_hwnd = dress(self.app, taskbar=True)
        place(self.app_hwnd, *self.app_box)
        self.frost(self.app, self.app_box)  # while it's still hidden, so the glass is right at once
        self.app.show()
        self.screenshot_question(self.app)
        self.focus(self.app)

    def screenshot_question(self, window):
        global SCREENSHOTS
        if len(SCREENSHOTS) > 1:
            window.run_js(f"send({json.dumps(SCREENSHOTS)})")
            SCREENSHOTS = "1"

    def app_closed(self):
        self.app = self.app_hwnd = None

    def pull_tab(self, down):
        s = scale()  # in real pixels from here on
        w, h = round(TAB[0] * s), round(TAB[1] * s)
        area = wintypes.RECT()
        u32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)  # screen minus taskbar
        x = (area.left + area.right - w) // 2
        goal = area.top - round(8 * s) if down else area.top - h - 40 if self.tab_gone else area.top - h + round(PEEK * s)
        self.tab.run_js(f"LL.tab({'true' if down else 'false'})")
        self.tab_goal = (x, goal, w, h)
        threading.Thread(target=self.slide, args=(self.tab_goal,), daemon=True).start()

    def slide(self, goal):
        """Moves the tab halfway to goal each frame, so it eases into place."""
        x, y, w, h = goal
        at = self.tab_at if self.tab_at is not None else y
        tab = hwnd(self.tab)
        while self.tab_goal == goal:
            at += (y - at) // 2 if abs(y - at) > 1 else y - at
            place(tab, x, at, w, h)
            self.tab_at = at
            if at == y:
                return
            time.sleep(0.015)

    def js(self, where, code):
        window = self.popup if where == "popup" else self.app
        if window:
            window.run_js(code)

    # --- answering ---

    def ask(self, where, cid, question):
        if not self.answering.acquire(blocking=False):
            self.js(where, "LL.fail('LuckyLook is answering in another window. Try again when it’s done.')")
            return
        try:
            self.answer(where, cid, question)
        finally:
            self.answering.release()

    def answer(self, where, cid, question):
        self.stop.clear()
        if where == "popup":
            history = self.history
        else:
            history = json.loads(chat_file(cid).read_text(encoding="utf-8"))["messages"] if chat_file(cid).exists() else []
        text, sent, steps, start = [""], [0.0], [], time.time()

        def on_text(t):
            text[0] += t
            if time.time() - sent[0] >= SEND_SECONDS:
                sent[0] = time.time()
                self.js(where, f"LL.answer({json.dumps(markdown(text[0], live=True))})")

        def on_tool(name, args):
            label, detail = step_text(name, args)
            steps.append(label)
            self.js(where, f"LL.step({json.dumps(label)}, {json.dumps(detail)})")

        def on_result(name, out):
            self.js(where, f"LL.stepDone({json.dumps(result_text(name, out))})")

        note = ""
        try:
            reply = agent.ask(question, history[-KEEP_HISTORY:], on_text=on_text, on_tool=on_tool, on_result=on_result,
                              on_confirm=lambda t: self.confirm(where, t), stop=self.stop)
        except agent.Stopped:
            reply, note = text[0].strip() + "\n\n(stopped)", "stopped"
        except Exception as e:
            reply, note = text[0], f"⚠ {e}"
        if reply.strip():
            history = history + [{"role": "user", "content": question}, {"role": "assistant", "content": reply}]
            if where == "popup":
                self.history = history[-KEEP_HISTORY:]
                CHAT.write_text(json.dumps(self.history, ensure_ascii=False), encoding="utf-8")  # back after a restart
            else:
                CHATS.mkdir(exist_ok=True)
                old = json.loads(chat_file(cid).read_text(encoding="utf-8")) if chat_file(cid).exists() else {}
                chat_file(cid).write_text(json.dumps({"title": old.get("title") or question[:60], "messages": history},
                                                     ensure_ascii=False), encoding="utf-8")
        meta = {"seconds": round(time.time() - start, 1), "steps": steps, "note": note}
        self.js(where, f"LL.done({json.dumps(markdown(reply))}, {json.dumps(meta)}, {json.dumps(reply)})")

    def confirm(self, where, text):
        """Asked by the agent (on its thread) before it changes anything: an Allow/Deny card, then wait for a click."""
        cid = uuid.uuid4().hex[:8]
        done, answer = threading.Event(), []
        self.waiting[cid] = (done, answer)
        first, _, rest = text.partition("\n\n")
        card = f'<div class="ask-title">LuckyLook wants to {esc(first.strip())}</div>'
        if rest.strip():
            card += f"<pre>{esc(rest.rstrip())}</pre>"
        if where == "popup" and not self.shown:
            self.q.put(("toggle", None))  # it can't be answered unseen
        self.js(where, f"LL.confirm({json.dumps(cid)}, {json.dumps(card)})")
        while not done.wait(0.2):
            if self.stop.is_set():
                answer.append(False)
                break
        del self.waiting[cid]
        return answer[0]

    def stats(self):
        now = time.time()
        if now - self.counts_at > 60:
            self.counts_at = now
            threading.Thread(target=self.count, daemon=True).start()
        idle_t, total_t = cpu_times()
        d_idle, d_total = idle_t - self.cpu_last[0], total_t - self.cpu_last[1]
        self.cpu_last = (idle_t, total_t)
        mem = agent.MEMORYSTATUSEX(ctypes.sizeof(agent.MEMORYSTATUSEX))
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
        power = SYSTEM_POWER_STATUS()
        ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power))
        last = indexer.last_full()
        return {
            "model": ollama_state(), "busy": self.answering.locked(),
            "cpu": round(100 * (1 - d_idle / d_total)) if d_total else 0,
            "ram": {"pct": mem.load, "total": round(mem.total / 2 ** 30)},
            "battery": None if power.flag & 128 else {"pct": power.percent, "charging": power.ac == 1},
            "drives": drives(), "reminders": reminders(),
            "index": {**self.counts, "last": last, "next": last + REINDEX_HOURS * 3600 if last else 0,
                      "status": self.status if now - self.status_at < 15 else ""},
        }

    def count(self):
        try:
            self.counts = index_counts()
        except Exception as e:  # no index yet, or busy
            print(f"index counts: {e}", flush=True)

    # --- tray menu ---

    def quit(self):
        if self.tray:
            self.tray.remove()  # else a dead icon lingers in the tray until the mouse passes over it
        for w in list(webview.windows):
            w.destroy()

    def restart(self):
        """To apply settings: start a fresh LuckyLook, then quit this one."""
        exe = Path(sys.executable).with_name("pythonw.exe")  # no console window, even if this one was started with python
        subprocess.Popen([str(exe if exe.exists() else sys.executable), str(HERE / "lucky.py"), "--restarted"], cwd=HERE)
        self.quit()

    def edit(self, name, example):
        """Opens one of LuckyLook's settings files in Notepad, starting it from its example if it's not there yet."""
        path = HERE / name
        if not path.exists():
            path.write_text((HERE / example).read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.Popen(["notepad.exe", str(path)])

    def change_hotkey(self):
        if not self.shown:
            self.toggle()
        self.popup.run_js(f"LL.hotkeyMode({json.dumps(HOTKEY)})")


class Api:
    """What the pages can call (pywebview's js_api). Only these methods are reachable: state lives on Lucky."""

    def __init__(self, lucky):
        self._l = lucky

    def ask(self, where, cid, text):
        """Returns the chat's id (a new one for the app's first question)."""
        text = (text or "").strip()
        if where == "popup" and text in ("/new", "/widget", "/quit"):
            if text == "/new":
                self._l.history = []
                CHAT.unlink(missing_ok=True)
                self._l.popup.run_js("LL.clear()")
            else:
                self._l.q.put(("widget" if text == "/widget" else "quit", None))
            return ""
        if not text:
            return cid
        if where == "app" and not cid:
            cid = uuid.uuid4().hex[:12]
        threading.Thread(target=self._l.ask, args=(where, cid, text), daemon=True).start()
        return cid

    def stop(self):
        self._l.stop.set()

    def decide(self, cid, ok):
        done, answer = self._l.waiting.get(cid, (None, None))
        if done and not answer:
            answer.append(bool(ok))
            done.set()

    def history(self):
        return turns(self._l.history)

    def chats(self):
        return list_chats()

    def load(self, cid):
        return turns(json.loads(chat_file(cid).read_text(encoding="utf-8"))["messages"])

    def delete(self, cid):
        chat_file(cid).unlink(missing_ok=True)

    def open(self, path):
        if os.path.exists(path):
            if Path(path).suffix.lower() in agent.RUNS:  # a program only shows in Explorer: clicking never runs it
                self.reveal(path)
            else:
                os.startfile(path)

    def reveal(self, path):
        if os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", path])

    def url(self, url):
        agent.edge(url)

    def copy(self, text):
        set_clipboard(text)

    def stats(self):
        return self._l.stats()

    def reindex(self):
        self._l.reindex.set()

    def hide(self):
        if self._l.shown:
            self._l.q.put(("toggle", None))

    def toggle(self):
        self._l.q.put(("toggle", None))

    def tab(self, down):
        self._l.pull_tab(bool(down))

    def open_app(self):
        if self._l.shown:
            self._l.q.put(("toggle", None))
        self._l.q.put(("app", None))

    def minimize(self):
        if self._l.app:
            self._l.app.minimize()

    def close(self):
        if self._l.app:
            self._l.app.destroy()

    def set_hotkey(self, combo):
        parse_hotkey(combo)  # raises if it can't be a hotkey
        settings = HERE / "local_settings.py"
        lines = settings.read_text(encoding="utf-8").splitlines() if settings.exists() else []
        lines = [ln for ln in lines if not ln.startswith("HOTKEY =")] + [f'HOTKEY = "{combo}"']
        settings.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._l.q.put(("restart", None))


def set_clipboard(text):
    k, H = ctypes.windll.kernel32, ctypes.c_void_p
    k.GlobalAlloc.restype = k.GlobalLock.restype = H
    k.GlobalLock.argtypes = k.GlobalUnlock.argtypes = [H]
    u32.SetClipboardData.argtypes = [wintypes.UINT, H]
    data = text.encode("utf-16-le") + b"\0\0"
    mem = k.GlobalAlloc(0x2, len(data))  # GMEM_MOVEABLE
    ctypes.memmove(k.GlobalLock(mem), data, len(data))
    k.GlobalUnlock(mem)
    for _ in range(10):  # another app may have the clipboard open for a moment
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    u32.EmptyClipboard()
    u32.SetClipboardData(13, mem)  # CF_UNICODETEXT; Windows owns the memory now
    u32.CloseClipboard()


SETTINGS = {  # tray menu choices that the pump thread carries out
    "aboutme": lambda p: p.edit("me.md", "me.example.md"),
    "private": lambda p: p.edit("local_settings.py", "local_settings.example.py"),
    "hotkey": Lucky.change_hotkey,
    "restart": Lucky.restart,
    "reindex": lambda p: p.reindex.set(),  # index_loop starts a full pass within a few seconds
}


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("size", wintypes.UINT), ("time", wintypes.DWORD)]


def idle():
    """Away from the PC: no mouse or keyboard for IDLE_MINUTES, and no full-screen game, video or slides."""
    info, state = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO)), ctypes.c_int()
    u32.GetLastInputInfo(ctypes.byref(info))
    ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
    away_ms = (ctypes.windll.kernel32.GetTickCount() - info.time) & 0xFFFFFFFF  # the tick count wraps every 49 days
    return away_ms >= IDLE_MINUTES * 60_000 and state.value not in (2, 3, 4)


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("percent", ctypes.c_ubyte), ("saver", ctypes.c_ubyte),
                ("life", wintypes.DWORD), ("full_life", wintypes.DWORD)]


def plugged_in():
    """On the charger (or a desktop PC): the graphics card can work on pictures without draining the battery."""
    s = SYSTEM_POWER_STATUS()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
    return s.ac != 0  # 0 battery, 1 charger, 255 unknown


class MONITORINFO(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT), ("work", wintypes.RECT), ("flags", wintypes.DWORD)]


def screen_box(w, h):
    """(x, y, w, h) for the popup: centred, a sixth of the way down, on the screen the mouse is on."""
    u32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    u32.MonitorFromPoint.restype = ctypes.c_void_p
    u32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
    pt = wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    info = MONITORINFO(ctypes.sizeof(MONITORINFO))
    u32.GetMonitorInfoW(u32.MonitorFromPoint(pt, 2), ctypes.byref(info))  # 2: the nearest screen
    a = info.work  # minus the taskbar
    return a.left + (a.right - a.left - w) // 2, a.top + (a.bottom - a.top) // 6, w, h


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WM_TRAY, WM_LBUTTONUP, WM_RBUTTONUP = 0x8001, 0x0202, 0x0205


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("proc", WNDPROC), ("cls_extra", ctypes.c_int), ("wnd_extra", ctypes.c_int),
                ("instance", wintypes.HINSTANCE), ("icon", wintypes.HICON), ("cursor", wintypes.HANDLE),
                ("background", wintypes.HBRUSH), ("menu_name", wintypes.LPCWSTR), ("class_name", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("hwnd", wintypes.HWND), ("id", wintypes.UINT), ("flags", wintypes.UINT),
                ("callback", wintypes.UINT), ("icon", wintypes.HICON), ("tip", wintypes.WCHAR * 128),
                ("state", wintypes.DWORD), ("state_mask", wintypes.DWORD), ("info", wintypes.WCHAR * 256),
                ("version", wintypes.UINT), ("info_title", wintypes.WCHAR * 64), ("info_flags", wintypes.DWORD),
                ("guid", ctypes.c_byte * 16), ("balloon_icon", wintypes.HICON)]


class Tray:
    """LuckyLook's icon in the system tray. Click: open or close the popup. Right-click: a menu.
    Runs on the hotkey thread (its message loop delivers the clicks) and talks to the rest only through q."""
    MENU = [(1, f"Open LuckyLook\t{HOTKEY}", "toggle"), (9, "Open LuckyLook app", "app"),
            (2, "Show or hide the top tab", "widget"), (0, None, None),
            (8, "Reindex now", "reindex"), (4, "About me…", "aboutme"), (5, "Private folders and windows…", "private"),
            (6, "Change hotkey…", "hotkey"), (7, "Restart LuckyLook (applies settings)", "restart"), (0, None, None),
            (3, "Quit LuckyLook", "quit")]

    def __init__(self, q):
        H = ctypes.c_void_p
        u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u32.DefWindowProcW.restype = LRESULT
        u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD] + \
            [ctypes.c_int] * 4 + [wintypes.HWND, H, H, H]
        u32.CreateWindowExW.restype = wintypes.HWND
        u32.LoadImageW.restype = wintypes.HICON
        u32.CreatePopupMenu.restype = H
        u32.AppendMenuW.argtypes = [H, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        u32.TrackPopupMenu.argtypes = [H, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, H]
        u32.DestroyMenu.argtypes = [H]
        self.q = q
        self.proc = WNDPROC(self.on_message)  # kept alive: Windows calls it as long as the window exists
        self.explorer_restarted = u32.RegisterWindowMessageW("TaskbarCreated")  # before the window: it gets messages at once
        u32.RegisterClassW(ctypes.byref(WNDCLASSW(proc=self.proc, class_name="LuckyLookTray")))
        self.hwnd = u32.CreateWindowExW(0, "LuckyLookTray", "LuckyLook", 0, 0, 0, 0, 0, None, None, None, None)  # never shown
        icon = u32.LoadImageW(None, str(HERE / "assets" / "logo.ico"), 1, u32.GetSystemMetrics(49), u32.GetSystemMetrics(50),
                              0x10)  # IMAGE_ICON, small-icon size, LR_LOADFROMFILE
        self.nid = NOTIFYICONDATAW(size=ctypes.sizeof(NOTIFYICONDATAW), hwnd=self.hwnd, id=1, flags=0x7,
                                   callback=WM_TRAY, icon=icon, tip=f"LuckyLook ({HOTKEY})")  # message+icon+tip
        self.add()

    def add(self):
        ctypes.windll.shell32.Shell_NotifyIconW(0, ctypes.byref(self.nid))  # NIM_ADD

    def remove(self):
        ctypes.windll.shell32.Shell_NotifyIconW(2, ctypes.byref(self.nid))  # NIM_DELETE

    def notify(self, title, text):
        """A Windows notification from the tray icon."""
        self.nid.flags, self.nid.info, self.nid.info_title, self.nid.info_flags = 0x17, text[:255], title[:63], 0x4
        ctypes.windll.shell32.Shell_NotifyIconW(1, ctypes.byref(self.nid))  # NIM_MODIFY, NIF_INFO: the user's icon
        self.nid.flags = 0x7  # so re-adding the icon later doesn't show it again

    def on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY and lparam & 0xFFFF == WM_LBUTTONUP:
            self.q.put(("toggle", None))
        elif msg == WM_TRAY and lparam & 0xFFFF == WM_RBUTTONUP:
            self.menu()
        elif msg == self.explorer_restarted:  # Explorer crashed or restarted: the icon must be added again
            self.add()
        elif msg in (OPEN_APP, TOGGLE):  # LuckyLook started a second time (Start menu, startup)
            self.q.put(("app" if msg == OPEN_APP else "toggle", None))
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def menu(self):
        m = u32.CreatePopupMenu()
        for cid, text, _ in self.MENU:
            u32.AppendMenuW(m, 0x800 if not cid else 0, cid, text)  # 0x800: separator
        pt = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        u32.SetForegroundWindow(self.hwnd)  # else the menu doesn't close when you click elsewhere
        choice = u32.TrackPopupMenu(m, 0x180, pt.x, pt.y, 0, self.hwnd, None)  # return the choice, don't notify
        u32.DestroyMenu(m)
        for cid, _, kind in self.MENU:
            if cid and cid == choice:
                self.q.put((kind, None))


def already_running():
    """If LuckyLook is running, hand it this start (open the app or the popup) and say so."""
    other = u32.FindWindowW("LuckyLookTray", None)
    if not other or "--restarted" in sys.argv:
        return False
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(other, ctypes.byref(pid))
    u32.AllowSetForegroundWindow(pid.value)  # so it may bring its window to the front
    u32.PostMessageW(other, OPEN_APP if "--app" in sys.argv else TOGGLE, 0, 0)
    return True


if __name__ == "__main__":
    if not already_running():
        lucky = Lucky()
        webview.start(lucky.started, private_mode=True)
