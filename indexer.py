"""Keeps index.db up to date: every file's name/size/date, plus searchable text of documents and code.
Full passes (index) plus live updates in between (Watcher + update), which the popup runs.

Run:  python indexer.py                 (all drives in ROOTS)
      python indexer.py D:\\Projects     (just these folders)
"""
import array
import contextlib
import ctypes
import html
import json
import logging
import msvcrt
import os
import re
import sqlite3
import stat
import struct
import sys
import threading
import time
import urllib.request
import zipfile
from ctypes import wintypes
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
# Search by meaning: documents (not code or data) get an embedding from this local Ollama model.
EMBED_MODEL = "nomic-embed-text"
MEANING_EXT = {".pdf", ".docx", ".md", ".markdown", ".txt", ".rst"}
OLLAMA_EMBED = "http://localhost:11434/api/embed"
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
        create table if not exists vectors(id integer primary key, vec blob);  -- meaning of a document (embed_some)
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


@contextlib.contextmanager
def exclusive():
    """Only one writer at a time, across processes: two runs at once (popup + indexer.py by hand) would undo each
    other's work, so a second one raises instead."""
    with open(DB.with_suffix(".lock"), "a") as lock:
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("another index run is already going") from None
        try:
            yield
        finally:
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def index(roots=ROOTS, log=print):
    """Full pass: updates index.db for these folders."""
    with exclusive():
        _index(roots, log)


def add_folder(con, path, name, run):
    con.execute("insert into folders(path, name, seen) values (?,?,?) on conflict(path) do update set seen=excluded.seen",
                (path, name, run))


def add_file(con, path, name, size, mtime, text_ok, run):
    """Adds or refreshes one file, reading its text only if it's new or changed. Returns True if it was."""
    row = con.execute("select id, mtime from files where path=?", (path,)).fetchone()
    if row and row[1] == mtime:
        con.execute("update files set seen=? where id=?", (run, row[0]))
        return False
    ext = os.path.splitext(name)[1].lower()
    if row:
        fid = row[0]
        con.execute("update files set name=?, ext=?, size=?, mtime=?, seen=? where id=?", (name, ext, size, mtime, run, fid))
        con.execute("delete from content where rowid=?", (fid,))
        con.execute("delete from vectors where id=?", (fid,))
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
    return True


def _index(roots, log):
    con = connect()
    run = time.time_ns()
    seen = changed = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for path, name, size, mtime, text_ok in walk(root):
            if size is None:
                add_folder(con, path, name, run)
                continue
            seen += 1
            if seen % 5000 == 0:
                con.commit()
                log(f"indexing… {seen:,} files")
            changed += add_file(con, path, name, size, mtime, text_ok, run)
        # forget files under this root that no longer exist
        prefix = root if root.endswith(os.sep) else root + os.sep
        where = "seen<>? and path>=? and path<?"
        args = (run, prefix, prefix + "\uffff")
        con.execute(f"delete from content where rowid in (select id from files where {where})", args)
        con.execute(f"delete from vectors where id in (select id from files where {where})", args)
        con.execute(f"delete from files where {where}", args)
        con.execute(f"delete from folders where {where}", args)
    con.commit()
    con.close()
    log(f"index ready · {seen:,} files ({changed:,} new or changed)")


# ---------- live updates between full passes ----------

def live(path):
    """Is a change at this path worth indexing right away? Not in pruned or private folders, not in app and system
    folders (they churn all day: the full pass covers them), and not LuckyLook's own index files."""
    low = path.lower()
    if low.startswith(str(DB.with_suffix("")).lower() + "."):  # index.db, -wal, .lock: writing them is a change too
        return False
    if any(low == p or low.startswith(p + "\\") for p in (*PRUNE_PATHS, *PRIVATE)):
        return False
    return not any(part in PRUNE or part in NAMES_ONLY for part in low.split("\\")[1:])


def update(paths, log=print):
    """Applies changes the Watcher saw: (re)indexes each path that exists (a folder with everything in it, for moves
    and renames) and forgets each one that's gone, with everything that was under it."""
    with exclusive():
        con = connect()
        run = time.time_ns()
        for path in sorted(paths):
            try:
                st = os.stat(path, follow_symlinks=False)
            except OSError:  # gone (deleted, or the old name of a rename)
                args = (path, path + "\\", path + "]")  # "]" sorts right after "\": the range is everything under path
                where = "path = ? or (path >= ? and path < ?)"
                con.execute(f"delete from content where rowid in (select id from files where {where})", args)
                con.execute(f"delete from vectors where id in (select id from files where {where})", args)
                con.execute(f"delete from files where {where}", args)
                con.execute(f"delete from folders where {where}", args)
                continue
            name = os.path.basename(path)
            if st.st_reparse_tag in LINK_TAGS:
                continue
            if stat.S_ISDIR(st.st_mode):
                add_folder(con, path, name, run)
                for p, n, size, mtime, text_ok in walk(path):
                    if size is None:
                        add_folder(con, p, n, run)
                    else:
                        add_file(con, p, n, size, mtime, text_ok, run)
            elif stat.S_ISREG(st.st_mode):
                add_file(con, path, name, st.st_size, st.st_mtime, not st.st_file_attributes & CLOUD_ATTRS, run)
        con.commit()
        con.close()
    log(f"index updated · {len(paths):,} changes")


def embed(texts):
    """Embeddings (unit-length lists of floats) for these texts, from EMBED_MODEL."""
    body = json.dumps({"model": EMBED_MODEL, "input": texts, "truncate": True}).encode()
    with urllib.request.urlopen(urllib.request.Request(OLLAMA_EMBED, body, {"Content-Type": "application/json"}),
                                timeout=600) as r:
        return json.load(r)["embeddings"]


def embed_some(n=256):
    """Gives up to n documents without one a meaning vector (name + start of the text). Returns how many it did:
    0 means all done. ~20 documents a second on an 8 GB graphics card."""
    with exclusive():
        con = connect()
        exts = ",".join("?" * len(MEANING_EXT))
        rows = con.execute(f"select f.id, f.name, substr(c.body, 1, 1500) from files f join content c on c.rowid = f.id "
                           f"left join vectors v on v.id = f.id where v.id is null and f.ext in ({exts}) limit ?",
                           (*MEANING_EXT, n)).fetchall()
        if rows:
            vecs = embed([f"search_document: {name}\n{text}" for _, name, text in rows])  # nomic's prefix for documents
            con.executemany("insert or replace into vectors(id, vec) values (?, ?)",
                            [(fid, array.array("f", v).tobytes()) for (fid, _, _), v in zip(rows, vecs)])
            con.commit()
        con.close()
    return len(rows)


class Watcher:
    """Collects the paths Windows reports changing under these folders (ReadDirectoryChangesW, a thread per folder).
    take() hands over what changed since the last call, or None when Windows had to drop some (a burst too big for
    its buffer): then only a full pass can catch up."""

    def __init__(self, roots):
        self.lock = threading.Lock()
        self.paths, self.dropped = set(), False
        for root in roots:
            threading.Thread(target=self.watch, args=(root,), daemon=True).start()

    def watch(self, root):
        k, H = ctypes.windll.kernel32, ctypes.c_void_p
        k.CreateFileW.restype = H
        k.ReadDirectoryChangesW.argtypes = [H, H, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD), H, H]
        # list-directory access, share everything, open existing, directories need BACKUP_SEMANTICS
        handle = k.CreateFileW(root, 0x1, 0x7, None, 3, 0x02000000, None)
        if handle in (None, H(-1).value):
            return
        buf = ctypes.create_string_buffer(1 << 20)
        got = wintypes.DWORD()
        # whole subtree; names of files and folders, sizes, write times
        while k.ReadDirectoryChangesW(handle, buf, len(buf), True, 0x1 | 0x2 | 0x8 | 0x10, ctypes.byref(got), None, None):
            with self.lock:
                if not got.value:
                    self.dropped = True
                    continue
                offset = 0
                while True:  # FILE_NOTIFY_INFORMATION records: next offset, action, name length, name
                    step, _, size = struct.unpack_from("<III", buf, offset)
                    path = os.path.join(root, buf.raw[offset + 12:offset + 12 + size].decode("utf-16-le"))
                    if live(path):
                        self.paths.add(path)
                    if not step:
                        break
                    offset += step

    def take(self):
        with self.lock:
            paths, dropped = self.paths, self.dropped
            self.paths, self.dropped = set(), False
        return None if dropped else paths

    def give_back(self, paths):
        """For changes that couldn't be applied yet (indexer.py running by hand): try again next time."""
        with self.lock:
            self.paths |= paths


if __name__ == "__main__":
    index(sys.argv[1:] or ROOTS)
