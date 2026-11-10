"""Keeps index.db up to date: every file's name/size/date, plus searchable text of documents and code.

Run:  python indexer.py                 (all drives in ROOTS)
      python indexer.py D:\\Projects     (just these folders)
"""
import ctypes
import html
import logging
import msvcrt
import os
import re
import sqlite3
import stat
import sys
import time
import zipfile
from pathlib import Path

try:
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
except ImportError:
    PdfReader = None

DB = Path(__file__).with_name("index.db")
# every fixed (not USB or network) drive: C:, D:, ...
ROOTS = [d for d in (f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ") if ctypes.windll.kernel32.GetDriveTypeW(d) == 3]

# Never walked at all (noise, or huge and useless to ask about).
PRUNE = {"$recycle.bin", "system volume information", "node_modules", ".git", "__pycache__", ".venv", "venv",
         "site-packages", ".cache", ".gradle", ".m2", ".nuget"}
PRUNE_PATHS = {"c:\\windows"}
# Folders off limits to LuckyLook: never indexed (not even file names), listed, read or searched.
# Set your own in local_settings.py (see local_settings.example.py).
PRIVATE = []
# Walked for file names, but file contents are not read.
NAMES_ONLY = {"program files", "program files (x86)", "programdata", "appdata"}

try:
    from local_settings import *  # noqa: F401,F403  your own settings, never uploaded
except ImportError:
    pass
PRIVATE = [os.path.normcase(os.path.realpath(os.path.expandvars(p))) for p in PRIVATE]

TEXT_EXT = set(""".txt .md .markdown .rst .csv .tsv .json .yaml .yml .toml .ini .cfg .conf .xml .html .htm .css
 .scss .js .jsx .ts .tsx .mjs .cjs .py .ipynb .java .kt .c .h .cpp .hpp .cs .go .rs .rb .php .swift .sh .ps1 .bat
 .cmd .sql .r .lua .dart .vue .svelte .tex .srt .gd .gradle .sln .csproj""".split())
DOC_EXT = {".docx"} | ({".pdf"} if PdfReader else set())
MAX_TEXT_BYTES = 2_000_000
MAX_DOC_BYTES = 30_000_000
MAX_CHARS = 200_000
SECRET = re.compile(r"^(\.env.*|id_rsa.*|id_ed25519.*|.*\.(pem|key|pfx|p12|kdbx|ppk))$", re.I)

LINK_TAGS = {0xA0000003, 0xA000000C}  # junction, symlink: skip to avoid loops
CLOUD_ATTRS = 0x1000 | 0x40000 | 0x400000  # offline / OneDrive placeholders: reading would download them


def connect():
    con = sqlite3.connect(DB, timeout=60)
    con.execute("pragma journal_mode=wal")
    con.executescript("""
        create table if not exists files(id integer primary key, path text unique, name text, ext text,
                                         size int, mtime real, seen int);
        create index if not exists files_size on files(size);
        create index if not exists files_mtime on files(mtime);
        create table if not exists folders(path text primary key, name text, seen int);
        create virtual table if not exists content using fts5(name, body, tokenize='porter unicode61 remove_diacritics 2');
    """)
    return con


def private(path):
    """True for anything inside a PRIVATE folder, however the path is written (case, slashes, .., short names, links)."""
    try:
        p = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return True  # can't tell, so no
    return any(p == f or p.startswith(f + os.sep) for f in PRIVATE)


def readable(name, size):
    ext = os.path.splitext(name)[1].lower()
    if SECRET.match(name):
        return False
    if ext in TEXT_EXT:
        return size <= MAX_TEXT_BYTES
    return ext in DOC_EXT and size <= MAX_DOC_BYTES


def extract(path, limit=MAX_CHARS):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        parts, n = [], 0
        for page in PdfReader(path).pages:
            parts.append(page.extract_text() or "")
            n += len(parts[-1])
            if n > limit:
                break
        # pypdf can return half an emoji (an unpaired surrogate), which SQLite refuses to store
        return "\n".join(parts)[:limit].encode("utf-8", "ignore").decode()
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        return html.unescape(re.sub(r"<[^>]+>", "", xml.replace("</w:p>", "\n")))[:limit]
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read(limit).replace("\x00", "")


def walk(root):
    """Yields (path, name, size, mtime, may_read_text) for every regular file under root.
    Folders are yielded too, with size None."""
    stack = [(root, True)]
    while stack:
        folder, text_ok = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
                e.path.encode()  # same problem in a file name: skip it rather than crash the whole run
            except (OSError, UnicodeEncodeError):
                continue
            if stat.S_ISDIR(st.st_mode):
                low = e.name.lower()
                full = e.path.lower()
                if low in PRUNE or full in PRUNE_PATHS or full in PRIVATE or st.st_reparse_tag in LINK_TAGS:
                    continue
                stack.append((e.path, text_ok and low not in NAMES_ONLY))
                yield e.path, e.name, None, None, False
            elif stat.S_ISREG(st.st_mode):
                yield e.path, e.name, st.st_size, st.st_mtime, text_ok and not st.st_file_attributes & CLOUD_ATTRS


def index(roots=ROOTS, log=print):
    """Updates index.db for these folders. Two runs at once (popup + indexer.py by hand) would undo each other's work,
    so a second one raises instead."""
    with open(DB.with_suffix(".lock"), "a") as lock:
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("another index run is already going") from None
        try:
            _index(roots, log)
        finally:
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _index(roots, log):
    con = connect()
    run = time.time_ns()
    seen = changed = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for path, name, size, mtime, text_ok in walk(root):
            if size is None:
                con.execute("insert into folders(path, name, seen) values (?,?,?) "
                            "on conflict(path) do update set seen=excluded.seen", (path, name, run))
                continue
            seen += 1
            if seen % 5000 == 0:
                con.commit()
                log(f"indexing… {seen:,} files")
            row = con.execute("select id, mtime from files where path=?", (path,)).fetchone()
            if row and row[1] == mtime:
                con.execute("update files set seen=? where id=?", (run, row[0]))
                continue
            changed += 1
            ext = os.path.splitext(name)[1].lower()
            if row:
                fid = row[0]
                con.execute("update files set name=?, ext=?, size=?, mtime=?, seen=? where id=?",
                            (name, ext, size, mtime, run, fid))
                con.execute("delete from content where rowid=?", (fid,))
            else:
                fid = con.execute("insert into files(path, name, ext, size, mtime, seen) values (?,?,?,?,?,?)",
                                  (path, name, ext, size, mtime, run)).lastrowid
            if text_ok and readable(name, size):
                try:
                    text = extract(path)
                except Exception:  # corrupt pdf/docx, locked file, etc.
                    text = ""
                if text.strip():
                    con.execute("insert into content(rowid, name, body) values (?,?,?)", (fid, name, text))
        # forget files under this root that no longer exist
        prefix = root if root.endswith(os.sep) else root + os.sep
        where = "seen<>? and path>=? and path<?"
        args = (run, prefix, prefix + "\uffff")
        con.execute(f"delete from content where rowid in (select id from files where {where})", args)
        con.execute(f"delete from files where {where}", args)
        con.execute(f"delete from folders where {where}", args)
    con.commit()
    con.close()
    log(f"index ready · {seen:,} files ({changed:,} new or changed)")


if __name__ == "__main__":
    index(sys.argv[1:] or ROOTS)
