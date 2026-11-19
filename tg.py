"""LuckyLook's Telegram: your own account through Telegram's official API (Telethon), so it can list your chats,
read and search messages, download files and photos, and send messages.

Connect once: tray menu > Connect Telegram..., or:  .venv\\Scripts\\python tg.py login
That asks for an app id from my.telegram.org, then your phone number and the code Telegram sends you. It keeps a
session file (telegram.session) here: it lets LuckyLook use your Telegram, so it never leaves this PC.
"""
import asyncio
import datetime
import difflib
import json
import re
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
SESSION = HERE / "telegram"  # Telethon adds .session
KEYS = HERE / "telegram.json"  # the app id and hash from my.telegram.org
DOWNLOADS = Path.home() / "Downloads" / "Telegram"
MAX_MESSAGES = 200
SENDS_PER_HOUR = 10  # a safety net: never send in bulk through the API (bulk sending is what gets accounts flagged)

try:
    from local_settings import *  # noqa: F401,F403  your own settings, never uploaded
except ImportError:
    pass


class NotConnected(Exception):
    def __str__(self):
        return ("Telegram isn't connected yet. Tell the user: tray menu > Connect Telegram... (one time: an app id from "
                "my.telegram.org, then their phone number and the code Telegram sends).")


_loop = _client = None
_lock = threading.Lock()
_dialogs, _dialogs_at = [], 0.0


def run(coro):
    """Runs a coroutine on the Telegram client's own event loop (a background thread) and returns its result."""
    global _loop
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True).start()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=300)


def connected():
    return KEYS.exists() and SESSION.with_suffix(".session").exists()


async def client():
    global _client
    if _client is None:
        if not connected():
            raise NotConnected()
        from telethon import TelegramClient
        keys = json.loads(KEYS.read_text(encoding="utf-8"))
        c = TelegramClient(str(SESSION), keys["api_id"], keys["api_hash"])
        await c.connect()
        if not await c.is_user_authorized():
            await c.disconnect()
            raise NotConnected()
        _client = c
    return _client


async def dialogs():
    """All chats, newest first (cached for a minute: listing them is the slow part)."""
    global _dialogs, _dialogs_at
    if time.time() - _dialogs_at > 60:
        _dialogs = await (await client()).get_dialogs(limit=500)
        _dialogs_at = time.time()
    return _dialogs


def match(name, titles):
    """Indexes of the titles that are the chat asked for: every word of name close to a word in the title."""
    asked = re.findall(r"\w+", name.lower())
    out = []
    for i, t in enumerate(titles):
        words = re.findall(r"\w+", (t or "").lower())
        if asked and words and all(any(difflib.SequenceMatcher(None, a, w).ratio() >= 0.8 for w in words) for a in asked):
            out.append(i)
    return out


async def find(chat):
    """(the chat, its title) for a name, or a str saying why not (none, or which of several)."""
    if chat.strip().lower() in ("saved messages", "me", "myself"):
        return "me", "Saved Messages"
    ds = await dialogs()
    hits = match(chat, [d.name for d in ds])
    exact = [i for i in hits if ds[i].name.strip().lower() == chat.strip().lower()]
    hits = exact or hits
    if not hits:
        return f"no Telegram chat called {chat!r}"
    if len(hits) > 1:
        return f"several Telegram chats match {chat!r}, which one? " + ", ".join(ds[i].name for i in hits[:10])
    return ds[hits[0]].entity, ds[hits[0]].name


def media(m):
    """' [photo]', ' [document: report.pdf, 2.1 MB]'... or ''."""
    if not m.media or not m.file:
        return ""
    kind = "photo" if m.photo else "video" if m.video else "voice" if m.voice else "audio" if m.audio else \
        "sticker" if m.sticker else "document"
    name = f": {m.file.name}" if m.file.name else ""
    size = f", {m.file.size / 1e6:.1f} MB" if m.file.size else ""
    return f" [{kind}{name}{size}]"


def line(m, title=""):
    who = "me" if m.out else (getattr(m.sender, "first_name", None) or getattr(m.sender, "title", None) or title or "?")
    text = (m.message or "").replace("\n", " ")
    return f"[{m.id}] {m.date.astimezone():%Y-%m-%d %H:%M} {who}: {text[:600]}{media(m)}"


def when(since):
    """'2026-11-19' or '2026-11-19 10:00' -> an aware datetime (local time), or None."""
    if not since.strip():
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(since.strip(), fmt).astimezone()
        except ValueError:
            pass
    raise ValueError(f"since must look like 2026-11-19 or 2026-11-19 10:00, not {since!r}")


# ---------- what agent.py's tools call ----------

def chats(limit=20, unread_only=False):
    async def go():
        out = []
        for d in await dialogs():
            if unread_only and not d.unread_count:
                continue
            last = d.message
            preview = (last.message or "").replace("\n", " ")[:80] if last else ""
            out.append(f"{d.name} | {d.unread_count} unread | {last.date.astimezone():%Y-%m-%d %H:%M} | "
                       f"{preview}{media(last) if last else ''}" if last else f"{d.name} | {d.unread_count} unread")
            if len(out) >= limit:
                break
        return "\n".join(out) or ("no unread chats" if unread_only else "no chats")
    return run(go())


def read(chat, limit=30, since=""):
    async def go():
        found = await find(chat)
        if isinstance(found, str):
            return found
        entity, title = found
        start, out = when(since), []
        async for m in (await client()).iter_messages(entity, limit=min(MAX_MESSAGES, limit if not start else MAX_MESSAGES)):
            if start and m.date < start:
                break
            out.append(line(m, title))
            if len(out) >= limit:
                break
        return f"{title} (newest last):\n" + "\n".join(reversed(out)) if out else f"no messages in {title}" + (
            f" since {since}" if since else "")
    return run(go())


def search(query, chat="", limit=20):
    async def go():
        entity, title = None, ""
        if chat:
            found = await find(chat)
            if isinstance(found, str):
                return found
            entity, title = found
        out = []
        async for m in (await client()).iter_messages(entity, search=query, limit=min(limit, MAX_MESSAGES)):
            where = title or getattr(m.chat, "title", None) or getattr(m.chat, "first_name", None) or ""
            out.append(f"{where} | {line(m, where)}")
        return "\n".join(out) or f"no Telegram messages with {query!r}"
    return run(go())


def pick_media(chat, message_id=0, kind=""):
    """(the message with the file, the chat's title, a description) or a str saying why not."""
    async def go():
        found = await find(chat)
        if isinstance(found, str):
            return found
        entity, title = found
        c = await client()
        if message_id:
            m = await c.get_messages(entity, ids=int(message_id))
            if not m or not m.media:
                return f"message {message_id} in {title} has no file or photo"
            return m, title, media(m).strip(" []")
        async for m in c.iter_messages(entity, limit=MAX_MESSAGES):
            d = media(m)
            if d and d != " [sticker]" and (not kind or kind.lower() in d.lower()):
                return m, title, d.strip(" []")
        return f"no {kind or 'file or photo'} in the last {MAX_MESSAGES} messages of {title}"
    return run(go())


def download(m, title):
    """Saves a message's file or photo in Downloads\\Telegram\\<chat>; returns where."""
    folder = DOWNLOADS / (re.sub(r'[<>:"/\\|?*]', "_", title).strip(" .") or "chat")
    folder.mkdir(parents=True, exist_ok=True)

    async def go():
        return await (await client()).download_media(m, file=str(folder) + "\\")
    return run(go())


def find_chat(chat):
    """(the chat, its title), or a str saying why not."""
    return run(find(chat))


_sent = []  # when messages were sent through the API, for SENDS_PER_HOUR


def send_limit():
    """None if another message may go now, else why not (and when it may)."""
    now = time.time()
    _sent[:] = [t for t in _sent if now - t < 3600]
    if len(_sent) < SENDS_PER_HOUR:
        return None
    wait = int((_sent[0] + 3600 - now) / 60) + 1
    return (f"LuckyLook has already sent {SENDS_PER_HOUR} Telegram messages in the last hour, its limit to keep your "
            f"account safe from Telegram's spam checks; the next one can go in about {wait} minutes")


def send(entity, text):
    if send_limit():
        raise RuntimeError(send_limit())

    async def go():
        await (await client()).send_message(entity, text)
    run(go())
    _sent.append(time.time())


_me = ""


def me():
    """The connected account's first name (asked once)."""
    global _me
    if not _me:
        async def go():
            return (await (await client()).get_me()).first_name or "you"
        _me = run(go())
    return _me


def disconnect():
    """Logs LuckyLook out of Telegram (the session ends there too) and deletes the login files."""
    global _client, _me, _dialogs_at
    if connected():
        try:
            async def go():
                await (await client()).log_out()
            run(go())
        except Exception as e:  # offline: the files still go; end it in Telegram > Settings > Devices
            print(f"telegram log out: {e!r}", flush=True)
    _client, _me, _dialogs_at = None, "", 0.0
    for f in (SESSION.with_suffix(".session"), KEYS):
        f.unlink(missing_ok=True)


def login():
    """One time, in a console: the app id, then your phone number and code (and your password if you set one)."""
    print("Connect LuckyLook to your Telegram\n")
    print("1. Open https://my.telegram.org and log in with your phone number.")
    print("2. Click 'API development tools', fill in any app title and short name (e.g. LuckyLook), Create.")
    print("3. Copy the api_id and api_hash shown there.\n")
    old = json.loads(KEYS.read_text(encoding="utf-8")) if KEYS.exists() else {}
    hint = " (Enter: the one you gave before)" if old else ""
    api_id = int(input(f"api_id{hint}: ").strip() or old.get("api_id", 0))
    api_hash = input(f"api_hash{hint}: ").strip() or old.get("api_hash", "")
    KEYS.write_text(json.dumps({"api_id": api_id, "api_hash": api_hash}), encoding="utf-8")
    SESSION.with_suffix(".session").unlink(missing_ok=True)  # a login that didn't finish before: start clean
    print("\nNow your phone number with the country code (like +91 98765 43210), then the code Telegram sends you"
          " (in the Telegram app, not by SMS). If you use a cloud password, it asks for that too.\n")
    from telethon.sync import TelegramClient
    with TelegramClient(str(SESSION), api_id, api_hash) as c:  # start() asks for the phone, the code, the password
        c.start()
        me = c.get_me()
        print(f"\nConnected as {me.first_name}. LuckyLook can use Telegram now; you can close this window.")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["login"]:
        try:
            login()
        except (Exception, KeyboardInterrupt) as e:  # shown, not lost: the window would otherwise just close
            print(f"\nThat didn't work: {e}\nClose this window and try Connect again.")
        input("\nPress Enter to close this window.")
    else:
        print(__doc__)
