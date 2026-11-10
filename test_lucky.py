"""Self-check: python test_lucky.py"""
import datetime
import json
import msvcrt
import os
import shutil
import sqlite3
import tempfile
import time
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

json.dumps(agent.SCHEMAS)
assert list(lucky.find_paths("see `C:\\My Docs\\cv.pdf` and D:\\x\\y.txt.")) == [
    ("C:\\My Docs\\cv.pdf", 5), ("D:\\x\\y.txt", 28)]
print("ok")
