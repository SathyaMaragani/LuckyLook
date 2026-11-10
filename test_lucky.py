"""Self-check: python test_lucky.py"""
import datetime
import json
import msvcrt
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.error
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
