"""LuckyLook brain: a local Ollama model plus tools over this PC. They only look, except remember (memory.md)
and the actions (open, create, move, recycle), which ask the user first.

Run:  python agent.py "where is my resume?"
"""
import base64
import csv
import datetime
import glob
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
import time
import urllib.error
import urllib.request
import winreg
from contextlib import closing
from pathlib import Path

import indexer

# Picked with bench.py (Nov 2026, RTX 5060 8 GB): qwen3.5:9b fits fully on the GPU. Thinking on = 6/6 correct, ~12s.
# Thinking off = ~7s but sloppier reasoning. qwen3:14b = 6/6 but ~17s (half runs on CPU).
MODEL = "qwen3.5:9b"
OLLAMA = "http://localhost:11434/api/chat"
THINK = True  # let the model reason before answering: more accurate, slower
NUM_CTX = 16384
MAX_STEPS = 8
MAX_TOOL_CHARS = 8000
MAX_READ = 12000
HERE = Path(__file__).parent
NO_WINDOW = 0x08000000  # don't flash a console when running under pythonw
IMAGE_EXT = {".png", ".jpg", ".jpeg"}  # webp reached the model blank in testing
# browser histories browser_history can search (one per profile). Empty this list to keep browsing out of it.
HISTORY = [os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data\*\History"),
           os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\*\History")]
# Window titles blacked out in look_at_screen's screenshots (PowerShell -like patterns, e.g. an Edge profile you keep
# private: "* - Work - Microsoft*"). Set your own in local_settings.py.
PRIVATE_WINDOWS = []

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
    """Full-text search INSIDE documents, PDFs, notes and code on this PC. Returns best matching files with snippets."""
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
    """Find documents (PDF, Word, notes, text) by what they're about, when the exact words aren't known:
    "bill from the plumber" finds invoices, "trip ideas" finds travel notes. Use with search_content."""
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
    "machine" (Windows version, CPU, GPU, RAM, battery, uptime)."""
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
    return 'kind must be "disk", "apps", "processes" or "machine"'


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
    cmd = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + cmd  # keep non-English file names intact
    # encoded, so quotes inside cmd reach PowerShell untouched
    return subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", base64.b64encode(cmd.encode("utf-16-le")).decode()],
                          capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW).stdout.strip()


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
    kind = "Directory" if os.path.isdir(path) else "File"
    quoted = path.replace("'", "''")
    powershell(f"Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::Delete{kind}"
               f"('{quoted}', 'OnlyErrorDialogs', 'SendToRecycleBin')")
    return "couldn't delete it" if os.path.exists(path) else "sent to the Recycle Bin"


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
                                  open_item, open_app, move_item, create_folder, recycle_item]}
TYPES = {str: "string", int: "integer", float: "number"}


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
    if any(isinstance(v, str) and indexer.private(v) for v in args.values()):  # every tool, every path argument
        return ('refused. Tell the user exactly: "You\'ve put that folder off limits for me, so I can\'t look in it." '
                "Don't give any other reason and don't try another way in.")
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
You can also open files and apps, create folders, move or rename, and send things to the Recycle Bin, but only when the user asks
for that. Each action asks the user to allow it; if they don't, leave it.
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
