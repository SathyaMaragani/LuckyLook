"""LuckyLook brain: a local Ollama model plus tools over this PC. They only look, except remember (memory.md),
remind_me (reminders.json) and the actions (open, close, write, edit, run, move, recycle), which ask the user first.

Run:  python agent.py "where is my resume?"
"""
import base64
import csv
import ctypes
import datetime
import glob
import html
import inspect
import io
import json
import os
import re
import shutil
import sqlite3
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import winreg
from contextlib import closing
from pathlib import Path
from urllib.parse import quote_plus

import indexer

HERE = Path(__file__).parent


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                ("rest", ctypes.c_ulonglong * 6)]


def specs():
    """(graphics card memory, PC memory) in GB. The card's comes from its driver's registry entry; the biggest card
    counts (built-in graphics report only a little, e.g. 0.5 GB, so they don't win over a real card)."""
    vram = 0
    cards = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, cards) as key:
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                with winreg.OpenKey(key, winreg.EnumKey(key, i)) as card:
                    size = winreg.QueryValueEx(card, "HardwareInformation.qwMemorySize")[0]
                    vram = max(vram, size if isinstance(size, int) else int.from_bytes(size, "little"))
            except OSError:  # not a card, or an old driver without the 64-bit size
                pass
    mem = MEMORYSTATUSEX(ctypes.sizeof(MEMORYSTATUSEX))
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
    return vram / 1e9, mem.total / 1e9


def pick_model(pc=None):
    """The chat model for this PC (or for pc = (graphics GB, RAM GB)), from models.json."""
    vram, ram = pc or specs()
    table = json.loads((HERE / "models.json").read_text(encoding="utf-8"))
    for row in table["graphics_card"]:
        if vram >= row["memory_gb"]:
            return row["model"]
    return next(row["model"] for row in table["no_graphics_card"] if ram >= row["memory_gb"])


# Chosen by this PC's specs (models.json). On an RTX 5060 8 GB, bench.py had qwen3.5:9b at 6/6, ~12 s with thinking.
MODEL = pick_model()
OLLAMA = "http://localhost:11434/api/chat"
THINK = True  # let the model reason before answering: more accurate, slower
NUM_CTX = 16384
MAX_STEPS = 8
MAX_TOOL_CHARS = 8000
MAX_READ = 12000
NO_WINDOW = 0x08000000  # don't flash a console when running under pythonw
IMAGE_EXT = indexer.IMAGE_EXT
# browser histories browser_history can search (one per profile). Empty this list to keep browsing out of it.
HISTORY = [os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data\*\History"),
           os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\*\History")]
# Window titles blacked out in look_at_screen's screenshots (PowerShell -like patterns, e.g. an Edge profile you keep
# private: "* - Work - Microsoft*"). Set your own in local_settings.py.
PRIVATE_WINDOWS = []
# The Edge profile open_website uses: its folder in Edge's User Data ("Default", "Profile 1"; see edge://version).
# Never a PRIVATE one: without this, Edge would open the page in whichever profile was used last.
EDGE_PROFILE = "Default"
RUN_SECONDS = 120  # run_command stops a command after this long
REMINDERS = HERE / "reminders.json"
SEARCH = {"youtube": "https://www.youtube.com/results?search_query=", "google": "https://www.google.com/search?q=",
          "bing": "https://www.bing.com/search?q="}  # open_website searches these directly; other sites via Bing

try:
    from local_settings import *  # noqa: F401,F403  your own settings, never uploaded
except ImportError:
    pass


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def when(t):
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


def like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------- tools (read-only, except remember) ----------

def search_content(query: str, limit: int = 10):
    """Full-text search INSIDE documents, PDFs, Word, PowerPoint, Excel, notes, code and zips (the file names in them)
    on this PC, and in descriptions of pictures (photos, screenshots, scans). Returns best matching files with snippets."""
    words = re.findall(r"\w+", query)
    if not words:
        return "empty query"
    match = " OR ".join(f'"{w}"' for w in words)
    with closing(indexer.connect()) as con:
        rows = con.execute(
            "select f.path, f.mtime, snippet(content, 1, '«', '»', '…', 16) from content "
            "join files f on f.id = content.rowid where content match ? "
            "order by bm25(content, 5.0, 1.0) limit ?", (match, int(limit))).fetchall()
    return "\n".join(f"{p} (modified {when(m)})\n  {s}" for p, m, s in rows) or "no matches"


MEANING = {"count": -1}  # all document vectors as one matrix, reloaded when the index has more or fewer


def search_meaning(query: str, limit: int = 10):
    """Find documents (PDF, Word, PowerPoint, notes, text) and pictures by what they're about, when the exact words
    aren't known: "bill from the plumber" finds invoices, "trip ideas" finds travel notes, "my dog at the beach" finds
    photos. Use with search_content."""
    import numpy as np  # only needed here
    with closing(indexer.connect()) as con:
        count = con.execute("select count(*) from vectors where length(vec) > 0").fetchone()[0]
        if not count:
            return "no documents understood by meaning yet (it builds in the background); use search_content"
        if count != MEANING["count"]:
            rows = con.execute("select id, vec from vectors where length(vec) > 0").fetchall()
            MEANING.update(count=count, ids=[i for i, _ in rows],
                           matrix=np.frombuffer(b"".join(v for _, v in rows), dtype=np.float32).reshape(len(rows), -1))
        q = np.array(indexer.embed([f"search_query: {query}"])[0], dtype=np.float32)  # nomic's prefix for queries
        best = np.argsort(MEANING["matrix"] @ q)[::-1][:int(limit)]
        out = []
        for i in best:
            row = con.execute("select f.path, f.mtime, substr(c.body, 1, 160) from files f join content c "
                              "on c.rowid = f.id where f.id = ?", (MEANING["ids"][i],)).fetchone()
            if row:
                out.append(f"{row[0]} (modified {when(row[1])})\n  {' '.join(row[2].split())}…")
    return "\n".join(out) or "no matches"


def find_files(name: str = "", ext: str = "", under: str = "", min_size_mb: float = 0,
               modified_within_days: float = 0, sort: str = "recent", limit: int = 20):
    """Find files anywhere on the PC. name: words that must appear in the file's name or folder path.
    ext: extension(s) like "pdf,docx". under: only inside this folder. sort: "recent", "largest" or "name".
    Also reports the total count and size of all matches."""
    where, args = ["1=1"], []
    for word in re.split(r"[\s_\-]+", name):
        if not word:
            continue
        where.append("path like ? escape '\\'")
        args.append(f"%{like(word)}%")
    exts = ["." + e.lstrip(".") for e in re.split(r"[,\s]+", ext.lower()) if e]
    if exts:
        where.append(f"ext in ({','.join('?' * len(exts))})")
        args += exts
    if under:
        where.append("path like ? escape '\\'")
        args.append(like(under.rstrip("\\/") + "\\") + "%")
    if float(min_size_mb):
        where.append("size >= ?")
        args.append(float(min_size_mb) * 1024 * 1024)
    if float(modified_within_days):
        where.append("mtime >= ?")
        args.append(time.time() - float(modified_within_days) * 86400)
    order = {"largest": "size desc", "name": "name"}.get(sort, "mtime desc")
    w = " and ".join(where)
    with closing(indexer.connect()) as con:
        count, total = con.execute(f"select count(*), sum(size) from files where {w}", args).fetchone()
        rows = con.execute(f"select path, size, mtime from files where {w} order by {order} limit ?",
                           args + [int(limit)]).fetchall()
    head = f"{count:,} matching files, {human(total or 0)} total. Showing {len(rows)}:"
    return "\n".join([head] + [f"{p}  {human(s)}  {when(m)}" for p, s, m in rows])


def find_folders(name: str, under: str = "", limit: int = 20):
    """Find folders anywhere on the PC whose folder name contains these words (e.g. "proj" finds "MyProjects").
    Results closest to the drive root come first. Use list_dir on a result to see what's inside."""
    where, args = [], []
    for word in name.split():
        where.append("name like ? escape '\\'")
        args.append(f"%{like(word)}%")
    if under:
        where.append("path like ? escape '\\'")
        args.append(like(under.rstrip("\\/") + "\\") + "%")
    if not where:
        return "give a folder name"
    with closing(indexer.connect()) as con:
        rows = con.execute(f"select path from folders where {' and '.join(where)} "
                           "order by length(path) - length(replace(path, '\\', '')), path limit ?",
                           args + [int(limit)]).fetchall()
    return "\n".join(p for (p,) in rows) or "no matching folders (the index may not have reached it yet)"


def read_file(path: str):
    """Read the text of one file (document, PDF, Word, code, notes). Long files are cut off."""
    p = Path(path)
    if not p.is_file():
        return "not a file"
    if indexer.SECRET.match(p.name):
        return "refused: looks like a secrets file"
    if p.suffix.lower() not in indexer.DOC_EXT:
        with open(p, "rb") as f:
            if b"\x00" in f.read(4096):
                return "binary file, can't read as text"
    text = indexer.extract(str(p), MAX_READ)
    return text + ("\n…[truncated]" if len(text) >= MAX_READ else "")


def list_dir(path: str):
    """List what's inside a folder."""
    with os.scandir(path) as it:  # skip hidden/system entries like desktop.ini
        entries = sorted((e for e in it if not e.stat(follow_symlinks=False).st_file_attributes & 0x6),
                         key=lambda e: (not e.is_dir(), e.name.lower()))
    lines = [e.name + "\\" if e.is_dir() else f"{e.name}  {human(e.stat().st_size)}" for e in entries[:300]]
    return "\n".join(lines) + (f"\n…and {len(entries) - 300} more" if len(entries) > 300 else "") or "empty folder"


def system_info(kind: str):
    """kind: "disk" (free space per drive), "apps" (installed programs), "processes" (running programs by memory),
    "machine" (Windows version, CPU, GPU, RAM, battery, uptime), "crashes" (blue screens, sudden shutdowns, app
    crashes and hangs in the last 14 days: "why did my PC crash"), "startup" (apps that start with Windows)."""
    if kind == "crashes":
        lines = '-EA SilentlyContinue | ForEach-Object { "{0:yyyy-MM-dd HH:mm}  {1}" -f $_.TimeCreated, ($_.Message -split "\\r?\\n")[0] }'
        since = "StartTime=(Get-Date).AddDays(-14)"
        return powershell(f"'Shutdowns and blue screens:'; Get-WinEvent -FilterHashtable @{{LogName='System'; Id=41,6008,1001; {since}}} "
                          f"-MaxEvents 20 {lines}; ''; 'App crashes and hangs:'; Get-WinEvent -FilterHashtable "
                          f"@{{LogName='Application'; Id=1000,1002; {since}}} -MaxEvents 30 {lines}")
    if kind == "startup":
        return powershell('Get-CimInstance Win32_StartupCommand | ForEach-Object { "$($_.Name)  ($($_.Location))" }')
    if kind == "disk":
        out = []
        for d in (f"{c}:\\" for c in string.ascii_uppercase):
            try:
                u = shutil.disk_usage(d)
                out.append(f"{d}  {human(u.free)} free of {human(u.total)}")
            except OSError:
                pass
        return "\n".join(out)
    if kind == "apps":
        return "\n".join(installed_apps())
    if kind == "processes":
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
        agg = {}
        for row in csv.reader(io.StringIO(out)):
            if len(row) >= 5:
                count, kb = agg.get(row[0], (0, 0))
                agg[row[0]] = (count + 1, kb + int(re.sub(r"\D", "", row[4]) or 0))
        top = sorted(agg.items(), key=lambda kv: -kv[1][1])[:40]
        return "\n".join(f"{name} x{c}  {human(kb * 1024)}" for name, (c, kb) in top)
    if kind == "machine":
        return powershell("Get-CimInstance Win32_OperatingSystem | fl Caption,Version,LastBootUpTime,"
                          "TotalVisibleMemorySize,FreePhysicalMemory; Get-CimInstance Win32_Processor | fl Name;"
                          "Get-CimInstance Win32_VideoController | fl Name; "
                          "Get-CimInstance Win32_Battery | fl EstimatedChargeRemaining")
    return 'kind must be "disk", "apps", "processes", "machine", "crashes" or "startup"'


def recent_files(limit: int = 30):
    """Files and folders the user opened recently (Windows' Recent list), newest first, with when they were opened.
    Use for "what was I working on" or "that file I had open yesterday"."""
    return powershell(
        "$sh = New-Object -ComObject WScript.Shell; Get-ChildItem $env:APPDATA\\Microsoft\\Windows\\Recent\\*.lnk | "
        "Sort-Object LastWriteTime -Descending | Select-Object -First " + str(int(limit)) + " | ForEach-Object { "
        "$t = $sh.CreateShortcut($_.FullName).TargetPath; if ($t) { '{0:yyyy-MM-dd HH:mm}  {1}' -f $_.LastWriteTime, $t } }"
    ) or "nothing in the Recent list"


def look_at_image(path: str, question: str = ""):
    """Look at an image (photo, screenshot, scan) and describe it or read the text in it.
    question: what to find out about it (default: describe it, including any text)."""
    p = Path(path)
    if p.suffix.lower() not in IMAGE_EXT:
        return "can only look at png and jpg images"
    msg = {"role": "user", "content": question or "Describe this image in detail, including any text in it.",
           "images": [base64.b64encode(p.read_bytes()).decode()]}
    # same model and num_ctx as the chat, so Ollama doesn't swap or reload anything
    body = json.dumps({"model": MODEL, "messages": [msg], "stream": False, "think": False,
                       "options": {"num_ctx": NUM_CTX}}).encode()
    req = urllib.request.Request(OLLAMA, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["message"]["content"]


def look_at_screen(question: str = ""):
    """See what's on the user's screen right now, for "what does this error mean", "what am I looking at", etc.
    LuckyLook's own popup doesn't show up in it. question: what to find out."""
    png = Path(tempfile.gettempdir()) / "luckylook_screen.png"
    pats = ",".join("'" + p.replace("'", "''") + "'" for p in PRIVATE_WINDOWS)  # as a PowerShell list
    powershell('Add-Type -Name Dpi -Namespace W -MemberDefinition \'[DllImport("user32.dll")] public static extern '
               "bool SetProcessDPIAware();'; [W.Dpi]::SetProcessDPIAware() | Out-Null; "  # else it grabs a scaled-down corner
               "Add-Type -AssemblyName System.Windows.Forms, System.Drawing, UIAutomationClient, UIAutomationTypes; "
               "$b = [Windows.Forms.Screen]::PrimaryScreen.Bounds; $i = New-Object Drawing.Bitmap $b.Width, $b.Height; "
               "$g = [Drawing.Graphics]::FromImage($i); $g.CopyFromScreen($b.Location, [Drawing.Point]::Empty, $b.Size); "
               "$all = [Windows.Automation.AutomationElement]::RootElement.FindAll('Children', "
               "[Windows.Automation.Condition]::TrueCondition); foreach ($w in $all) { "
               f"$n = $w.Current.Name; if (@({pats}) | Where-Object {{ $n -like $_ }}) {{ "
               "$r = $w.Current.BoundingRectangle; if (-not $r.IsEmpty) { "
               "$g.FillRectangle([Drawing.Brushes]::Black, [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height) } } }; "
               f"$i.Save('{png}')")
    try:
        return look_at_image(str(png), question or "Describe what's on this screen, including any text, errors or dialogs.")
    finally:
        png.unlink(missing_ok=True)  # screenshots can show private things: don't leave one lying around


def clipboard(question: str = ""):
    """What the user has copied: text, copied files, or an image such as a Win+Shift+S snip (which it then looks at).
    Use for "what I copied" or "this snip". question: what to find out about a copied image."""
    png = Path(tempfile.gettempdir()) / "luckylook_clip.png"
    png.unlink(missing_ok=True)
    out = powershell("Add-Type -AssemblyName System.Drawing; $t = Get-Clipboard -Raw; if ($t) { $t } else { "
                     "$f = Get-Clipboard -Format FileDropList; if ($f) { $f.FullName } else { "
                     f"$i = Get-Clipboard -Format Image; if ($i) {{ $i.Save('{png}', [Drawing.Imaging.ImageFormat]::Png) }} }} }}")
    if not png.exists():
        return out[:MAX_READ] or "nothing copied"
    try:
        return look_at_image(str(png), question)
    finally:
        png.unlink()


def browser_history(search: str = "", day: str = "", limit: int = 30):
    """Web pages the user visited in Edge or Chrome (about the last 3 months), newest first, with when.
    search: words in the page title or web address. day: only that date, like 2026-11-09."""
    where, args = ["u.hidden = 0"], []
    if day:
        start = datetime.datetime.fromisoformat(day[:10]).timestamp()  # local midnight
        where.append("v.visit_time between ? and ?")
        args += [(t + 11644473600) * 1e6 for t in (start, start + 86400)]  # browsers count µs from 1601
    for word in search.split():
        where.append("(u.title like ? escape '\\' or u.url like ? escape '\\')")
        args += [f"%{like(word)}%"] * 2
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, db in enumerate(p for g in HISTORY for p in glob.glob(g) if not indexer.private(p)):
            copy = os.path.join(tmp, str(i))  # the browser keeps its own file busy while it runs, so read a copy
            shutil.copyfile(db, copy)
            if os.path.exists(db + "-journal"):
                shutil.copyfile(db + "-journal", copy + "-journal")  # lets SQLite undo a half-written change
            with closing(sqlite3.connect(copy)) as con:  # every visit, so a page seen again today still counts for yesterday
                rows += con.execute("select max(v.visit_time) t, u.title, u.url from visits v join urls u on u.id = v.url "
                                    f"where {' and '.join(where)} group by u.id order by t desc limit ?",
                                    args + [int(limit)]).fetchall()
    rows.sort(reverse=True)
    return "\n".join(f"{when(t / 1e6 - 11644473600)}  {(title or '')[:90]}  {url[:120]}"
                     for t, title, url in rows[:int(limit)]) or "nothing found"


def powershell(cmd):
    return run_powershell(cmd).stdout.strip()


def run_powershell(cmd, **kw):
    # keep non-English file names intact; no progress bars in the output
    cmd = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $ProgressPreference = 'SilentlyContinue'; " + cmd
    # encoded, so quotes inside cmd reach PowerShell untouched
    return subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", base64.b64encode(cmd.encode("utf-16-le")).decode()],
                          capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, **kw)


def installed_apps():
    names = set()
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive, view in [(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY), (winreg.HKEY_CURRENT_USER, 0)]:
        try:
            key = winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                        name = winreg.QueryValueEx(sub, "DisplayName")[0]
                        try:
                            name += " " + winreg.QueryValueEx(sub, "DisplayVersion")[0]
                        except OSError:
                            pass
                        names.add(name.strip())
                except OSError:
                    pass
    return sorted(names, key=str.lower)


# ---------- actions: each one asks the user first ----------

def ask_in_terminal(text):
    return input(f"\nLuckyLook wants to {text}. Allow? [y/N] ").strip().lower() == "y"


CONFIRM = ask_in_terminal  # ask() swaps in the popup's Allow/Deny
NO = "NOT DONE. The user clicked Deny, so nothing changed. Say that it was not done because they said no."
REFUSED = ('refused. Tell the user exactly: "You\'ve put that folder off limits for me, so I can\'t look in it." '
           "Don't give any other reason and don't try another way in.")
REMIND_LOCK = threading.Lock()  # the popup takes due reminders out while the agent may be adding one
PREVIEW_LINES = 30  # of a file write_file is about to save (or a change, or a command), shown with Allow/Deny
RUNS = {".exe", ".bat", ".cmd", ".ps1", ".py", ".pyw", ".vbs", ".js", ".msi", ".lnk", ".reg", ".com", ".scr"}


def open_item(path: str):
    """Open a file or folder with its usual app (a document, a picture, a folder in Explorer). Asks the user first."""
    if not os.path.exists(path):
        return "doesn't exist"
    if not CONFIRM(f"{'run' if Path(path).suffix.lower() in RUNS else 'open'} {path}"):
        return NO
    os.startfile(path)
    return "opened"


def open_app(name: str):
    """Start an installed app by its name in the Start menu (e.g. "Notepad", "Blender 5.2"). Asks the user first.
    If several apps match, it lists them instead: call again with the exact name."""
    apps = json.loads(powershell("Get-StartApps | ConvertTo-Json -Compress") or "[]")  # Store apps too
    apps = [apps] if isinstance(apps, dict) else apps
    words = name.lower().split()
    hits = [a for a in apps if a["Name"].lower() == name.lower()] or \
        [a for a in apps if all(w in a["Name"].lower() for w in words)]
    if not hits:
        return f"no app called {name!r} in the Start menu"
    if len(hits) > 1:
        return "several apps match, which one? " + ", ".join(a["Name"] for a in hits[:15])
    if not CONFIRM(f"start {hits[0]['Name']}"):
        return NO
    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + hits[0]["AppID"]])
    return f"started {hits[0]['Name']}"


def move_item(path: str, to: str):
    """Move or rename a file or folder. to: the new full path, or an existing folder to move it into.
    Never overwrites anything. Asks the user first."""
    if not os.path.exists(path):
        return "doesn't exist"
    dest = os.path.join(to, os.path.basename(path)) if os.path.isdir(to) else to
    if os.path.exists(dest):
        return f"{dest} already exists, so nothing was moved"
    if not CONFIRM(f"move {path} to {dest}"):
        return NO
    shutil.move(path, dest)
    return f"moved to {dest}"


def recycle_item(path: str):
    """Delete a file or folder by sending it to the Recycle Bin, where it can be restored. Asks the user first."""
    if not os.path.exists(path):
        return "doesn't exist"
    if not CONFIRM(f"send {path} to the Recycle Bin"):
        return NO
    return "couldn't delete it" if to_recycle_bin(path) else "sent to the Recycle Bin"


def to_recycle_bin(path):
    """True if it's still there afterwards."""
    kind = "Directory" if os.path.isdir(path) else "File"
    quoted = path.replace("'", "''")
    powershell(f"Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::Delete{kind}"
               f"('{quoted}', 'OnlyErrorDialogs', 'SendToRecycleBin')")
    return os.path.exists(path)


def write_file(path: str, text: str):
    """Create a file with this text: code, notes, a list, anything (e.g. path D:\\Code\\hello.py, text: the whole program).
    path: the full path; missing folders are created. If the file exists, the old one goes to the Recycle Bin first.
    Asks the user first, showing them the text."""
    if os.path.isdir(path):
        return f"{path} is a folder; give the full path of the file"
    old = os.path.exists(path)
    verb = "replace" if old else "create"
    if not CONFIRM(f"{verb} {path}{' (the old one goes to the Recycle Bin)' if old else ''} with:\n\n{shorten(text)}\n"):
        return NO
    if old and to_recycle_bin(path):
        return "couldn't move the old file to the Recycle Bin, so nothing was written"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    Path(path).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return f"{'replaced' if old else 'created'} {path} ({len(text.splitlines())} lines)"


def shorten(text, prefix=""):
    lines = text.splitlines() or [""]
    more = len(lines) - PREVIEW_LINES
    return "\n".join(prefix + s for s in lines[:PREVIEW_LINES]) + (f"\n… {more} more lines" if more > 0 else "")


def edit_file(path: str, old: str, new: str):
    """Change part of a text file (code, settings, notes): replaces the exact text old with new. old must appear
    exactly once, so read_file first and copy it exactly, with a line or two around it. For a new file use write_file.
    The previous version goes to the Recycle Bin. Asks the user first, showing the change."""
    if not os.path.isfile(path):
        return "doesn't exist"
    raw = Path(path).read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    if not old or text.count(old) != 1:
        return (f"the text to replace is in the file {text.count(old) if old else 0} times; it must be exactly once. "
                "read_file and copy it exactly, with more of the lines around it")
    if not CONFIRM(f"change {path} (the old version goes to the Recycle Bin):\n\n{shorten(old, '− ')}\n{shorten(new, '+ ')}\n"):
        return NO
    if to_recycle_bin(path):
        return "couldn't move the old version to the Recycle Bin, so nothing was changed"
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="\r\n" if b"\r\n" in raw else "\n") as f:
        f.write(text.replace(old, new))
    return f"changed {path}"


def run_command(command: str):
    """Run a PowerShell command and get its output: run a script you wrote (python D:\\Code\\hello.py), convert or
    process files, work something out. Runs in the user's home folder and stops after 2 minutes.
    Asks the user first, showing the exact command."""
    low = os.path.normcase(command)
    if any(f in low or os.path.basename(f) in low for f in indexer.PRIVATE):
        return REFUSED
    if not CONFIRM(f"run this command:\n\n{shorten(command)}\n"):
        return NO
    try:
        r = run_powershell(command, cwd=Path.home(), timeout=RUN_SECONDS)
    except subprocess.TimeoutExpired:
        return f"still running after {RUN_SECONDS} seconds, so it was stopped"
    err = r.stderr  # PowerShell sends its errors as XML when run like this: just their text
    if err.startswith("#< CLIXML"):
        err = html.unescape("".join(re.findall(r'<S S="Error">(.*?)</S>', err, re.S))).replace("_x000D__x000A_", "\n")
    return "\n".join(s for s in (f"exit code {r.returncode}", r.stdout.strip(), err.strip()) if s)


def processes(name=""):
    """[(image name, pid)] of running programs whose name contains name, LuckyLook itself left out."""
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    me = {os.getpid(), os.getppid()}
    return [(r[0], int(r[1])) for r in csv.reader(io.StringIO(out))
            if len(r) >= 2 and name.lower() in r[0].lower() and int(r[1]) not in me]


def close_app(name: str, force: bool = False):
    """Close a running app by name ("Chrome", "Word", "Spotify"). It closes the normal way, so it can still ask to save.
    force=True ends it at once and unsaved work is lost: only when the user says kill it, or it's frozen.
    If several programs match, it lists them: call again with the exact name. Asks the user first."""
    procs = processes(name)
    names = sorted({n for n, _ in procs}, key=str.lower)
    exact = [n for n in names if n.lower() in (name.lower(), name.lower() + ".exe")]
    if len(names) > 1 and len(exact) == 1:
        names, procs = exact, [p for p in procs if p[0] == exact[0]]
    if not procs:
        return f"nothing called {name!r} is running"
    if len(names) > 1:
        return "several programs match, which one? " + ", ".join(names[:20])
    if not CONFIRM(f"{'force-close' if force else 'close'} {names[0]}{'' if force else ' (it may ask you to save)'}"):
        return NO
    subprocess.run(["taskkill", *(["/f", "/t"] if force else []), *[a for _, p in procs for a in ("/pid", str(p))]],
                   capture_output=True, creationflags=NO_WINDOW)
    time.sleep(3)
    left = {p for _, p in processes(names[0])} & {p for _, p in procs}
    if not left:
        return f"closed {names[0]}"
    return (f"asked {names[0]} to close, but it's still running ({len(left)} of {len(procs)} processes): it may be "
            "asking to save, or have no window. force=True ends it, but only if the user says so")


def open_website(url: str = "", search: str = "", play: bool = False):
    """Open a website in Microsoft Edge, or search on it. url: an address ("youtube.com", "https://…") or a site name.
    search: what to look for there: url "youtube", search "Shape of You Ed Sheeran" opens YouTube's results for that
    song. play=True (with url "youtube"): plays the top result straight away: for "play …", "put on …".
    With only search, it searches the web. Asks the user first."""
    play = play and bool(search)
    if play:
        url = "youtube"
    if search:
        site = next((k for k in SEARCH if k in url.lower()), "") if url else "bing"
        target = SEARCH[site] + quote_plus(search) if site else SEARCH["bing"] + quote_plus(f"{search} site:{url}")
    else:
        has_scheme = "://" in url or re.match(r"[a-z]+:(?!\d)", url, re.I)  # javascript:…, but not localhost:3000
        target = url if has_scheme else "https://" + (url if "." in url or ":" in url else url + ".com")
    if not re.match(r"https?://[^\s\"]+$", target):
        return "only web addresses (http or https) can be opened"
    profile = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data"), EDGE_PROFILE)
    if indexer.private(profile):
        return REFUSED
    if not CONFIRM(f"play the top YouTube result for “{search}” in Edge" if play else f"open {target} in Edge"):
        return NO
    hit = play and top_video(search)  # after Allow: nothing goes to YouTube before
    if hit:
        target = hit[0]
    os.startfile("msedge", arguments=f'--profile-directory="{EDGE_PROFILE}" "{target}"')
    if hit:
        return f"playing “{hit[1]}” ({target})"
    return f"opened {target}" + (" (couldn't read YouTube's results, so they're shown instead of playing one)" if play else "")


def top_video(search):
    """(watch URL, title) of YouTube's first result (ads aren't videoRenderers), or None if its page couldn't be read."""
    req = urllib.request.Request(SEARCH["youtube"] + quote_plus(search),
                                 headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode("utf-8", "replace")
    except OSError:
        return None
    # ponytail: reads the JSON inside YouTube's page; if YouTube changes it, open_website falls back to the results
    m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})".*?"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', page, re.S)
    return m and ("https://www.youtube.com/watch?v=" + m[1], json.loads(f'"{m[2]}"'))


def remind_me(at: str, text: str):
    """Remind the user later with a Windows notification. at: "YYYY-MM-DD HH:MM", worked out from the time now
    ("in 20 minutes", "at 5 pm", "tomorrow morning" = 09:00). text: what to remind them of."""
    t = datetime.datetime.strptime(at.strip(), "%Y-%m-%d %H:%M")
    if t <= datetime.datetime.now():
        return f"{at} has already passed; work out the time again from now"
    with REMIND_LOCK:
        items = json.loads(REMINDERS.read_text(encoding="utf-8")) if REMINDERS.exists() else []
        REMINDERS.write_text(json.dumps(items + [{"at": f"{t:%Y-%m-%d %H:%M}", "text": text}], ensure_ascii=False),
                             encoding="utf-8")
    return f"set for {t:%A %Y-%m-%d %H:%M}. A notification will pop up then, as long as LuckyLook is running."


def due_reminders(now=None):
    """Takes the reminders that are due out of reminders.json: [text]. LuckyLook shows them as notifications."""
    now = f"{now or datetime.datetime.now():%Y-%m-%d %H:%M}"
    with REMIND_LOCK:
        if not REMINDERS.exists():
            return []
        items = json.loads(REMINDERS.read_text(encoding="utf-8"))
        due = [r for r in items if r["at"] <= now]
        if due:
            REMINDERS.write_text(json.dumps([r for r in items if r["at"] > now], ensure_ascii=False), encoding="utf-8")
    return [r["text"] + ("" if r["at"] == now else f" (was due {r['at']})") for r in due]


def create_folder(path: str):
    """Create a new folder (and any missing folders above it). path: the full path, with the folder name exactly
    as the user said it, spaces and all ("Old Photos" on D: is D:\\Old Photos). Asks the user first."""
    if os.path.exists(path):
        return f"{path} already exists"
    if not CONFIRM(f"create the folder {path}"):
        return NO
    os.makedirs(path)
    return f"created {path}"


def remember(fact: str):
    """Save a lasting fact or preference about the user. Only use when the user asks you to remember something."""
    with open(HERE / "memory.md", "a", encoding="utf-8") as f:
        f.write(f"- {fact.strip()}\n")
    return "saved"


FUNCS = {f.__name__: f for f in [search_content, search_meaning, find_files, find_folders, read_file, list_dir, system_info,
                                  recent_files, look_at_image, look_at_screen, clipboard, browser_history, remember,
                                  remind_me, open_item, open_app, open_website, close_app, move_item, create_folder,
                                  write_file, edit_file, run_command, recycle_item]}
TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def schema(fn):
    params = list(inspect.signature(fn).parameters.values())
    return {"type": "function", "function": {
        "name": fn.__name__, "description": inspect.getdoc(fn),
        "parameters": {"type": "object",
                       "properties": {p.name: {"type": TYPES[p.annotation]} for p in params},
                       "required": [p.name for p in params if p.default is p.empty]}}}


SCHEMAS = [schema(f) for f in FUNCS.values()]


def run_tool(name, args):
    fn = FUNCS.get(name)
    if not fn:
        return f"unknown tool {name}"
    if any(isinstance(v, str) and k not in ("text", "old", "new") and indexer.private(v) for k, v in args.items()):
        return REFUSED  # every path argument (a file's text isn't one)
    try:
        out = str(fn(**args))
    except Exception as e:
        out = f"error: {e}"
    return out if len(out) <= MAX_TOOL_CHARS else out[:MAX_TOOL_CHARS] + "\n…[truncated]"


# ---------- chat loop ----------

def system_prompt():
    def read(n):
        p = HERE / n
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""
    now = datetime.datetime.now()
    yesterday = now - datetime.timedelta(days=1)  # spelled out: the 9B model got "yesterday" wrong doing the maths itself
    return f"""You are LuckyLook, a private assistant running entirely on the user's Windows PC.
Use your tools to look things up on this computer instead of guessing. If a search finds nothing, try other keywords,
or find_files by name, before giving up. Prefer find_files for "where is", search_content for "what does it say",
search_meaning when the user describes a document instead of quoting it (try it when search_content finds nothing).
When the user names a folder ("my projects folder"), find_folders it, then list_dir the best match.
Snippets are partial: when a found file looks relevant, read_file it before answering.
Write every file or folder path in full inside backticks, like `D:\\Docs\\cv.pdf`, so the user can click it.
Never write paths as markdown links or file:/// URLs.
You can also open files, apps and websites, close apps, create folders, write and edit files, run commands, move or rename,
and send things to the Recycle Bin, but only when the user asks for that. Each action asks the user to allow it; if they
don't, leave it. When asked to make a file, write the whole content with write_file instead of pasting it into your answer.
To play a song or video, open_website with url youtube, it as the search, and play=True (to just find one: no play). remind_me sets reminders.
Copy file and folder names exactly as the tools return them. Don't guess what names mean or invent details.
Be concise. Now: {now:%A %Y-%m-%d %H:%M}. Yesterday was {yesterday:%A %Y-%m-%d}.

# About the user
{read("me.md")}

# Remembered facts
{read("memory.md") or "(none yet)"}"""


def ask(question, history=(), on_text=lambda s: print(s, end="", flush=True), on_tool=lambda name, args: None,
        on_confirm=ask_in_terminal):
    """Answers one question, calling tools as needed. history: earlier user/assistant messages. Returns the answer.
    on_confirm(text) -> bool is asked before any action (open, move, delete)."""
    global CONFIRM
    CONFIRM = on_confirm
    messages = [{"role": "system", "content": system_prompt()}, *history, {"role": "user", "content": question}]
    content, retried = "", False
    for _ in range(MAX_STEPS):
        body = json.dumps({"model": MODEL, "messages": messages, "tools": SCHEMAS, "think": THINK,
                           "options": {"num_ctx": NUM_CTX}}).encode()
        req = urllib.request.Request(OLLAMA, body, {"Content-Type": "application/json"})
        content, thinking, calls, error = "", "", [], ""
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                for line in r:
                    chunk = json.loads(line)
                    error = chunk.get("error", "")
                    if error:
                        break
                    msg = chunk.get("message", {})
                    thinking += msg.get("thinking") or ""
                    if msg.get("content"):
                        content += msg["content"]
                        on_text(msg["content"])
                    calls += msg.get("tool_calls") or []
        except urllib.error.URLError as e:
            raise RuntimeError(f"Can't reach Ollama at {OLLAMA}. Is it running? ({e.reason})")
        if (error or not (calls or content.strip())) and not retried:
            # now and then the model stops with neither an answer nor a tool call, or writes a tool call Ollama
            # can't read ("XML syntax error"): ask once more
            retried = True
            continue
        if error:
            raise RuntimeError(error)
        # thinking must go back in the history, or thinking models answer only inside their thinking next turn
        messages.append({"role": "assistant", "content": content, "thinking": thinking,
                         **({"tool_calls": calls} if calls else {})})
        if not calls:
            return content
        for call in calls:
            fn = call["function"]
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args or "{}")
            on_tool(fn["name"], args)
            messages.append({"role": "tool", "tool_name": fn["name"], "content": run_tool(fn["name"], args)})
    return content + "\n(stopped: too many steps)"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ask(" ".join(sys.argv[1:]) or input("Ask: "), on_tool=lambda n, a: print(f"\n[{n} {a}]", flush=True))
    print()
