"""Self-check: python test_lucky.py"""
import contextlib
import datetime
import json
import msvcrt
import os
import queue
import shutil
import sqlite3
import tempfile
import threading
import time
import types
import urllib.error
import zipfile
from contextlib import closing
from pathlib import Path

import agent
import indexer
import lucky

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    indexer.DB = tmp / "test.db"
    docs = tmp / "docs"
    docs.mkdir()
    note = docs / "trip notes.md"
    note.write_text("Packing list for the zebra safari, budget 4242", encoding="utf-8")
    (docs / ".env").write_text("API_KEY=zebrasecret")
    (tmp / "node_modules").mkdir()
    (tmp / "node_modules" / "junk.js").write_text("zebra")
    quiet = lambda m: None  # noqa: E731

    indexer.index([str(tmp)], log=quiet)
    assert str(note) in agent.search_content("zebra safari")
    assert agent.search_content("zebrasecret") == "no matches"  # secrets never indexed
    assert "junk.js" not in agent.find_files(name="junk")  # node_modules pruned
    assert str(note) in agent.find_files(name="trip", ext="md", under=str(tmp).lower())
    assert "4242" in agent.read_file(str(note))
    assert agent.read_file(str(docs / ".env")).startswith("refused")
    assert "trip notes.md" in agent.list_dir(str(docs))
    assert agent.find_folders("OC").splitlines() == [str(docs)]  # partial, case-insensitive name match
    assert "node_modules" not in agent.find_folders("modules")

    note.unlink()
    (tmp / "old").mkdir()
    indexer.index([str(tmp)], log=quiet)
    assert str(tmp / "old") in agent.find_folders("old")
    (tmp / "old").rmdir()
    indexer.index([str(tmp)], log=quiet)
    assert "no matching" in agent.find_folders("old")
    assert "0 matching" in agent.find_files(name="trip")
    assert agent.search_content("zebra") == "no matches"

    with open(indexer.DB.with_suffix(".lock"), "a") as held:  # pretend another index run is going
        msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            indexer.index([str(tmp)], log=quiet)
            raise AssertionError("two index runs at once")
        except RuntimeError:
            pass
        msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)

    # search by meaning: a fake embedding (one dimension per topic word) instead of the real model
    topics = ["cat", "money", "trip"]
    indexer.embed = lambda texts: [[float(w in t.lower()) / max(1, sum(x in t.lower() for x in topics)) ** 0.5
                                    for w in topics] for t in texts]
    (docs / "kittens.md").write_text("all about the cat", encoding="utf-8")
    (docs / "invoice.txt").write_text("money owed: 300", encoding="utf-8")
    indexer.index([str(tmp)], log=quiet)
    while indexer.embed_some(1):  # one at a time, to check it carries on until all are done
        pass
    assert agent.search_meaning("money", limit=1).startswith(str(docs / "invoice.txt"))
    assert agent.search_meaning("cat", limit=1).startswith(str(docs / "kittens.md"))
    (docs / "invoice.txt").unlink()
    indexer.update({str(docs / "invoice.txt")}, log=quiet)
    assert "invoice" not in agent.search_meaning("money")  # its vector went with it
    real_embed = indexer.embed

    def picky(texts):  # Ollama refusing one odd document must not block the others
        if any("odd" in t for t in texts):
            raise urllib.error.HTTPError("", 400, "Bad Request", None, None)
        return real_embed(texts)
    indexer.embed = picky
    (docs / "odd.md").write_text("odd one", encoding="utf-8")
    (docs / "trip.md").write_text("trip plans", encoding="utf-8")
    indexer.index([str(tmp)], log=quiet)
    assert indexer.embed_some() == 2 and indexer.embed_some() == 0  # both handled, nothing left to retry forever
    assert agent.search_meaning("trip", limit=1).startswith(str(docs / "trip.md"))

    # PowerPoint, Excel, old Office, rtf, zips
    with zipfile.ZipFile(docs / "deck.pptx", "w") as z:
        z.writestr("ppt/slides/slide10.xml", "<p:sld><a:p><a:r><a:t>closing walrus</a:t></a:r></a:p></p:sld>")
        z.writestr("ppt/slides/slide2.xml", "<a:p><a:t>opening walrus</a:t></a:p>")
    with zipfile.ZipFile(docs / "sums.xlsx", "w") as z:
        z.writestr("xl/sharedStrings.xml", "<sst><si><t>Rent</t></si><si><r><t>Pen</t></r><r><t>guin</t></r></si></sst>")
        z.writestr("xl/worksheets/sheet1.xml", '<worksheet><cols><col min="1"/></cols><sheetData><row r="1"><c r="A1" '
                   't="s"><v>0</v></c><c r="B1"><f>SUM(1,2)</f><v>1250</v></c></row><row r="2"><c r="A2" t="s"><v>1</v>'
                   '</c><c r="B2" t="inlineStr"><is><t>wombat</t></is></c><c r="C2"/></row></sheetData></worksheet>')
    with zipfile.ZipFile(docs / "backup.zip", "w") as z:
        z.writestr("2024/tax report.pdf", "x")
    (docs / "old.doc").write_bytes(b"\xd0\xcf\x00\x01" + "hello narwhal".encode("utf-16-le") + b"\xff")
    (docs / "letter.rtf").write_text(r"{\rtf1\ansi{\fonttbl{\f0 Arial;}}\f0 caf\'e9 \b bold\b0\par next \u8364?}")
    assert indexer.extract(str(docs / "deck.pptx")).split() == ["opening", "walrus", "closing", "walrus"]
    assert indexer.extract(str(docs / "sums.xlsx")) == "Rent\t1250\nPenguin\twombat\t"
    assert "café bold\nnext €" in indexer.extract(str(docs / "letter.rtf"))
    indexer.index([str(tmp)], log=quiet)
    for word, name in [("walrus", "deck.pptx"), ("wombat", "sums.xlsx"), ("narwhal", "old.doc"), ("tax report", "backup.zip")]:
        assert str(docs / name) in agent.search_content(word), word

    # pictures get described by the model (faked here), only where text is read, never twice
    (docs / "beach.jpg").write_bytes(b"\xff\xd8" + b"0" * 60_000)
    (docs / "icon.png").write_bytes(b"0" * 1000)  # too small: an icon
    (tmp / "AppData").mkdir()
    (tmp / "AppData" / "art.png").write_bytes(b"0" * 60_000)  # app folder: names only
    indexer.index([str(tmp)], log=quiet)
    looked = []
    real_live, indexer.live = indexer.live, lambda p: real_live(p.replace(str(tmp), "D:\\t"))  # tmp is in AppData itself

    def down(path, prompt):
        raise urllib.error.URLError("refused")
    try:
        indexer.describe_some(down)
        raise AssertionError("Ollama down must be retried later, not skipped")
    except urllib.error.URLError:
        pass
    while indexer.describe_some(lambda path, prompt: looked.append(path) or "A photo of a red kayak on a beach."):
        pass
    indexer.live = real_live
    assert looked == [str(docs / "beach.jpg")]
    assert str(docs / "beach.jpg") in agent.search_content("kayak") and "art.png" not in agent.search_content("art")
    while indexer.embed_some():
        pass
    with closing(indexer.connect()) as con:  # the description got a meaning vector; the skipped picture didn't
        assert [r[0] for r in con.execute("select f.name from vectors v join files f on f.id = v.id where f.ext "
                                          "in ('.png', '.jpg')")] == ["beach.jpg"]

    # full passes: every REINDEX_HOURS counted from the last one (a restart doesn't redo it), and from Reindex now
    assert indexer.last_full() == 0  # passes of some folders don't count
    real_roots, indexer.ROOTS = indexer.ROOTS, [str(tmp)]
    lucky.idle, lucky.LIVE_SECONDS = (lambda: False), 0.1  # left so: the loops below run until the test ends

    def start():
        p = types.SimpleNamespace(q=queue.Queue(), reindex=threading.Event())
        threading.Thread(target=lucky.Popup.index_loop, args=(p,), daemon=True).start()
        return p

    def passed(p, seconds):
        end = time.time() + seconds
        while time.time() < end:
            with contextlib.suppress(queue.Empty):
                if p.q.get(timeout=0.1)[1].startswith("index ready"):
                    return True
        return False
    p = start()
    assert passed(p, 30) and not passed(p, 1)  # never done: a pass now, then none for hours
    p.reindex.set()
    assert passed(p, 30) and time.time() - indexer.last_full() < 30  # Reindex now
    assert not passed(start(), 1)  # restarted minutes later: no pass
    indexer.ROOTS = real_roots

    # live updates: new, edited, renamed folder, deleted
    (tmp / "live").mkdir()
    (tmp / "live" / "plan.md").write_text("ostrich budget 77", encoding="utf-8")
    indexer.update({str(tmp / "live")}, log=quiet)
    assert str(tmp / "live" / "plan.md") in agent.search_content("ostrich")
    time.sleep(0.05)
    (tmp / "live" / "plan.md").write_text("emu budget 88", encoding="utf-8")
    indexer.update({str(tmp / "live" / "plan.md")}, log=quiet)
    assert agent.search_content("ostrich") == "no matches" and "plan.md" in agent.search_content("emu")
    (tmp / "live").rename(tmp / "moved")  # Windows reports only the folder's old and new name
    indexer.update({str(tmp / "live"), str(tmp / "moved")}, log=quiet)
    assert str(tmp / "moved" / "plan.md") in agent.find_files(name="plan") and "\\live\\" not in agent.find_files(name="plan")
    (tmp / "moved" / "plan.md").unlink()
    indexer.update({str(tmp / "moved" / "plan.md")}, log=quiet)
    assert "0 matching" in agent.find_files(name="plan") and agent.search_content("emu") == "no matches"
    assert not indexer.live("C:\\Users\\x\\AppData\\Local\\cache.bin") and not indexer.live(str(indexer.DB) + "-wal")
    assert indexer.live("C:\\Users\\x\\Documents\\notes.md")

    hist = tmp / "History"  # a made-up Edge/Chrome history
    with closing(sqlite3.connect(hist)) as con:
        noon = datetime.datetime.combine(datetime.date.today(), datetime.time(12)).timestamp()
        us = lambda days_ago: (noon - days_ago * 86400 + 11644473600) * 1e6  # noqa: E731  browsers count µs from 1601
        con.execute("create table urls (id, url, title, hidden)")
        con.execute("create table visits (url, visit_time)")
        con.executemany("insert into urls values (?, ?, ?, ?)", [
            (1, "https://blender.org/addons", "Best Blender Add-ons", 0),
            (2, "https://old.example", "Blender news", 0),
            (3, "https://ads.example/blender", "", 1)])  # hidden = redirects and frames
        con.executemany("insert into visits values (?, ?)", [(1, us(0)), (1, us(1)), (2, us(30)), (3, us(0))])
        con.commit()
    agent.HISTORY = [str(hist)]
    out = agent.browser_history("blender add")  # every word must match, in the title or the address
    assert "Add-ons" in out and "old.example" not in out
    assert "old.example" in agent.browser_history("blender old")
    assert agent.browser_history("blender").count("\n") == 1  # each page once, never the hidden one
    yesterday = str(datetime.date.today() - datetime.timedelta(days=1))
    assert agent.browser_history(day=yesterday).startswith(yesterday + " 12:00  Best")  # seen again today, still listed
    assert agent.browser_history("zebra") == "nothing found"

    priv = tmp / "Profile 6"  # stands in for a private folder, like a browser profile
    priv.mkdir()
    (priv / "notes.md").write_text("zebra bookmark", encoding="utf-8")
    shutil.copy(hist, priv / "History")
    indexer.PRIVATE = [os.path.normcase(os.path.realpath(priv))]
    indexer.index([str(tmp)], log=quiet)
    assert "0 matching" in agent.find_files(name="notes") and agent.search_content("bookmark") == "no matches"
    assert "no matching" in agent.find_folders("Profile")  # not even the names
    assert agent.run_tool("read_file", {"path": str(priv / "notes.md")}).startswith("refused")
    assert agent.run_tool("list_dir", {"path": str(priv).upper() + "\\..\\Profile 6"}).startswith("refused")
    agent.HISTORY = [str(priv / "History")]
    assert agent.browser_history() == "nothing found"

    # actions ask first, never overwrite, and the private folder stays off limits
    a = tmp / "a.txt"
    a.write_text("x")
    agent.CONFIRM = lambda text: False
    assert agent.move_item(str(a), str(docs)) == agent.NO and a.exists()
    asked = []
    agent.CONFIRM = lambda text: asked.append(text) or True
    assert agent.move_item(str(a), str(docs)).startswith("moved") and (docs / "a.txt").exists() and not a.exists()
    assert asked == [f"move {a} to {docs / 'a.txt'}"]
    (tmp / "b.txt").write_text("y")
    assert "already exists" in agent.move_item(str(tmp / "b.txt"), str(docs / "a.txt")) and len(asked) == 1
    assert agent.run_tool("recycle_item", {"path": str(priv / "notes.md")}).startswith("refused") and len(asked) == 1
    agent.CONFIRM = lambda text: False
    assert agent.recycle_item(str(docs / "a.txt")) == agent.NO and (docs / "a.txt").exists()
    assert agent.create_folder(str(tmp / "Taxes" / "2026")) == agent.NO and not (tmp / "Taxes").exists()
    agent.CONFIRM = lambda text: asked.append(text) or True
    assert agent.create_folder(str(tmp / "Taxes" / "2026")).startswith("created") and (tmp / "Taxes" / "2026").is_dir()
    assert asked[-1] == f"create the folder {tmp / 'Taxes' / '2026'}"
    assert "already exists" in agent.create_folder(str(tmp / "Taxes"))
    assert agent.move_item(str(tmp / "Taxes"), str(tmp / "Tax papers")).startswith("moved")  # renaming a folder
    assert (tmp / "Tax papers" / "2026").is_dir() and not (tmp / "Taxes").exists()
    assert agent.run_tool("create_folder", {"path": str(priv / "new")}).startswith("refused")  # private stays private
    # write_file: shows the text first, creates folders, an old file goes to the Recycle Bin (here: stubbed)
    code = tmp / "Code" / "hello.py"
    agent.CONFIRM = lambda text: False
    assert agent.run_tool("write_file", {"path": str(code), "text": "print('hi')"}) == agent.NO and not code.exists()
    agent.CONFIRM = lambda text: asked.append(text) or True
    assert agent.run_tool("write_file", {"path": str(code), "text": "print('hi')"}) == f"created {code} (1 lines)"
    assert asked[-1] == f"create {code} with:\n\nprint('hi')\n" and code.read_text() == "print('hi')\n"
    real_bin, agent.to_recycle_bin = agent.to_recycle_bin, lambda p: os.remove(p) or False
    long = "\n".join(f"x = {i}" for i in range(40))
    assert agent.write_file(str(code), long).startswith("replaced") and code.read_text() == long + "\n"
    assert "(the old one goes to the Recycle Bin)" in asked[-1] and asked[-1].endswith("x = 29\n… 10 more lines\n")
    agent.to_recycle_bin = lambda p: True  # couldn't recycle: the old file stays as it was
    assert agent.write_file(str(code), "new").startswith("couldn't") and code.read_text() == long + "\n"
    agent.to_recycle_bin = real_bin
    assert agent.run_tool("write_file", {"path": str(priv / "x.py"), "text": "1"}).startswith("refused")
    # edit_file: exactly one match, line endings and the old version kept (Recycle Bin stubbed), Deny changes nothing
    conf = tmp / "conf.py"
    conf.write_bytes(b"host = 'a'\r\nport = 80\r\nport2 = 80\r\n")
    agent.to_recycle_bin = lambda p: shutil.copy(p, tmp / "conf.old") and False
    assert "2 times" in agent.edit_file(str(conf), "= 80", "= 8080") and not (tmp / "conf.old").exists()
    agent.CONFIRM = lambda text: False
    assert agent.edit_file(str(conf), "port = 80", "port = 8080") == agent.NO
    agent.CONFIRM = lambda text: asked.append(text) or True
    assert agent.run_tool("edit_file", {"path": str(conf), "old": "port = 80\n", "new": "port = 8080\n"}).startswith("changed")
    assert conf.read_bytes() == b"host = 'a'\r\nport = 8080\r\nport2 = 80\r\n" and (tmp / "conf.old").exists()
    assert asked[-1].endswith(":\n\n− port = 80\n+ port = 8080\n")
    agent.to_recycle_bin = real_bin
    # run_command: shows the command, returns output and exit code, stops slow ones, keeps out of private folders
    assert agent.run_command("Write-Output (6*7); exit 3") == "exit code 3\n42" and asked[-1] == "run this command:\n\nWrite-Output (6*7); exit 3\n"
    assert agent.run_command("Get-Item C:\\no-such-thing").startswith("exit code 1\nGet-Item : Cannot find path")
    agent.RUN_SECONDS = 1
    assert "stopped" in agent.run_command("Start-Sleep 5")
    agent.RUN_SECONDS = 120
    n = len(asked)
    assert agent.run_command(f'Get-ChildItem "{priv}"').startswith("refused") and len(asked) == n
    assert agent.run_command("dir $env:LOCALAPPDATA\\x\\profile 6").startswith("refused")
    # close_app: matches by name, asks, never LuckyLook itself
    sleeper = __import__("subprocess").Popen(["ping", "-n", "30", "127.0.0.1"], creationflags=agent.NO_WINDOW)
    assert (("PING.EXE", sleeper.pid) in agent.processes("ping") or ("ping.exe", sleeper.pid) in agent.processes("ping"))
    assert os.getpid() not in [p for _, p in agent.processes("python")]
    assert agent.close_app("ping", force=True) == "closed PING.EXE" and sleeper.wait(5) is not None
    assert asked[-1] == "force-close PING.EXE" and "nothing called" in agent.close_app("no-such-app-xyz")
    # open_website: YouTube search, plain addresses, only http(s), never a private Edge profile
    opened = []
    real_start, agent.os.startfile = os.startfile, lambda *a, **k: opened.append(k["arguments"])
    assert agent.open_website("youtube", "Shape of You Ed Sheeran") == \
        "opened https://www.youtube.com/results?search_query=Shape+of+You+Ed+Sheeran"
    assert opened[-1] == '--profile-directory="Default" "https://www.youtube.com/results?search_query=Shape+of+You+Ed+Sheeran"'
    assert agent.open_website("github.com/python") == "opened https://github.com/python"
    assert agent.open_website("", "weather") == "opened https://www.bing.com/search?q=weather"
    assert agent.open_website("imdb.com", "Dune") == "opened https://www.bing.com/search?q=Dune+site%3Aimdb.com"
    assert agent.open_website("localhost:3000") == "opened https://localhost:3000"
    assert agent.open_website("javascript:alert(1)").startswith("only") and agent.open_website("file:///C:/x").startswith("only")
    real_private, indexer.private = indexer.private, lambda p: p.endswith("Profile 6")
    agent.EDGE_PROFILE = "Profile 6"
    assert agent.open_website("youtube.com").startswith("refused") and len(opened) == 5
    agent.EDGE_PROFILE, indexer.private, agent.os.startfile = "Default", real_private, real_start
    # remind_me: times in the past refused; due ones come out once, late ones say so
    agent.REMINDERS = tmp / "reminders.json"
    assert "already passed" in agent.remind_me("2020-01-01 09:00", "x")
    soon = datetime.datetime.now() + datetime.timedelta(minutes=5)
    assert agent.remind_me(f"{soon:%Y-%m-%d %H:%M}", "call the bank").startswith("set for")
    assert agent.due_reminders() == [] and agent.due_reminders(soon) == ["call the bank"] and agent.due_reminders(soon) == []
    agent.remind_me(f"{soon:%Y-%m-%d %H:%M}", "stretch")
    assert agent.due_reminders(soon + datetime.timedelta(hours=1)) == [f"stretch (was due {soon:%Y-%m-%d %H:%M})"]
    assert "Shutdowns and blue screens:" in agent.system_info("crashes") and agent.system_info("startup") is not None

json.dumps(agent.SCHEMAS)
# the model fits the PC (models.json): (graphics card GB, RAM GB)
assert [agent.pick_model(pc) for pc in [(25.8, 64), (8.5, 32), (6.4, 16), (4.2, 16), (0.5, 32), (2, 12), (0, 4)]] == [
    "qwen3.5:27b", "qwen3.5:9b", "qwen3.5:4b", "qwen3.5:2b", "qwen3.5:4b", "qwen3.5:2b", "qwen3.5:0.8b"]
vram, ram = agent.specs()
assert ram > 1 and vram >= 0
assert list(lucky.find_paths("see `C:\\My Docs\\cv.pdf` and D:\\x\\y.txt.")) == [
    ("C:\\My Docs\\cv.pdf", 5), ("D:\\x\\y.txt", 28)]
real_live, indexer.live = indexer.live, lambda path: True  # LuckyLook's folder may be anywhere, even in AppData
with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as watched:
    w = indexer.Watcher([watched])
    time.sleep(0.3)
    Path(watched, "fresh.txt").write_text("hi", encoding="utf-8")
    for _ in range(40):  # Windows reports it within milliseconds; allow 2 s
        if str(Path(watched, "fresh.txt")) in (w.paths or ()):
            break
        time.sleep(0.05)
    assert str(Path(watched, "fresh.txt")) in w.take(), "the Watcher didn't see a new file"
indexer.live = real_live

root = lucky.tk.Tk()  # answers show markdown as styles, not symbols
t = lucky.tk.Text(root)
t.insert("end", "› q\n## Drives\n- **C:** `78 GB` free\n* 2 ** 3 = 8\n")
lucky.render_markdown(t, "2.0")
assert t.get("1.0", "end-1c") == "› q\nDrives\n• C: 78 GB free\n• 2 ** 3 = 8\n", t.get("1.0", "end-1c")
assert t.get(*t.tag_ranges("code")) == "78 GB" and [t.get(a, b) for a, b in zip(*[iter(t.tag_ranges("bold"))] * 2)] == [
    "Drives", "C:"]
root.destroy()
print("ok")
