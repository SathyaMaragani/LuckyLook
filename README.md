# LuckyLook

<img src="assets/logo.png" width="96" alt="LuckyLook logo">

**A private AI for your Windows PC that knows what's on it.** Press **Ctrl+Alt+L** anywhere (or click the thin blue tab hanging from the top of your screen), ask a question, and it answers from your own computer:

<img src="assets/screenshot.png" width="640" alt="The LuckyLook popup: frosted glass over a colourful background, answering whether the PC is healthy with a table of drives, the steps it took, a file chip and a code block, and an info strip with the AI model, index, reminder, CPU and battery">

For longer conversations there's the **LuckyLook app**: your chats on the left, the answers in the middle, and *At a glance* on the right (the AI model, CPU, memory, battery, drives, the index and your reminders):

<img src="assets/screenshot-app.png" width="800" alt="The LuckyLook app window in frosted glass: a chat list, an answer with a drive table, and an At a glance panel with CPU, memory and battery rings, drive bars and index numbers">

<img src="assets/screenshot-tab.png" width="168" alt="The LuckyLook tab at the top of the screen, pulled down on hover">

- **Files and documents:** "where is my resume?", "what did I quote the client for the logo?", "what's eating my disk?". Searches inside documents, PDFs, Word, PowerPoint and Excel files (old `.doc`/`.xls`/`.ppt` too), notes, code and zips (the names of the files in them), and your pictures: while you're away from the PC, the model describes each photo, screenshot and scan and copies its text, so "the photo of my car" finds it. All by exact words *and by meaning*: "the bill from the plumber" finds the invoice even if it never says "bill".
- **Your PC:** free space, installed apps, what's using memory, battery, specs.
- **What you were doing:** recently opened files, and websites you visited (Edge and Chrome history).
- **What's on your screen:** "what does this error mean?" It looks at your screen. Also images and photos ("what does this receipt say?").
- **What you copied:** text, files, or a Win+Shift+S snip.
- **The internet, answered in the chat:** "what's the temperature in Hyderabad?", "will it rain tomorrow?", "who won the last cricket world cup?". It gets the weather from wttr.in, searches DuckDuckGo and reads the pages itself, then answers, without opening a browser. Without a city it guesses one from your connection; put `City: …` in About me so it uses yours.
- **Does things, if you allow it:** "open my resume", "start Blender", "close Chrome", "play Shape of You" (plays the top YouTube result in Edge), "make a Taxes 2026 folder", "write a Python script in it that adds up my invoices", "run it", "change the port in config.py to 8080", "move the invoices into it", "delete the old installer". Every action shows **Allow / Deny** first, with the exact command or change, and deleting only ever goes to the Recycle Bin.
- **Reminders:** "remind me at 5 pm to call the bank" pops up a Windows notification then.
- **PC controls:** "set the volume to 50%", "mute", "turn it up a bit", "screen brightness 80%" (laptop screens), "next song", "pause the music". These just happen, no Allow needed.
- **Your Telegram, read for you:** "any new Telegram messages?", "what did Yeshwanth say today?", "summarise the family group", "find the message with the hotel address", "download the PDF Ravi sent". Connect once from the tray menu (**Connect Telegram…**): an app id from my.telegram.org, then your phone number and the code Telegram sends you. Messages to Telegram then go straight through Telegram (no typing into the window). Downloads land in `Downloads\Telegram`; downloading and sending ask your **Allow**, and at most 10 messages an hour go through the API (`SENDS_PER_HOUR`), so it can never send in bulk.
- **Calls on WhatsApp:** "call Yeshwanth", "video call Yeshwanth". Like messages: the right chat, checked on screen, then your **Allow**; it presses WhatsApp's own call button by name. (Telegram doesn't let other programs press its call button.)
- **Messages on Telegram and WhatsApp:** "send Hi to Yeshwanth on WhatsApp" (without an app named, Telegram). It opens Telegram Desktop or WhatsApp Desktop, finds the chat, reads the screen to make sure it's the right person, shows you who and what, and sends only after your **Allow** (or a spoken "yes"). If the first match isn't that person, nothing is sent. List the people you message under `## People` in `me.md` (tray menu › About me…), so voice spells their names right.
- **Talk to it:** say **"Hey Lucky"** (or click 🎤) and ask anything, in English, Hindi or Telugu; it can do everything it does when you type. It answers out loud and on screen, or on screen only: the 🔊 / 💬 toggle next to the mic. When a spoken request needs your Allow, it asks "Shall I…?" and you can just say yes or no.
- **When something's wrong:** "why did my PC crash last night?" (shutdowns, blue screens, app crashes), "what starts with Windows?"
- **Remembers you:** "remember that I prefer short answers" goes into `memory.md`. Tell it about yourself in `me.md` (tray menu › About me…). Your conversation is still there after a restart; `/new` starts fresh.

Everything runs **on your computer** with [Ollama](https://ollama.com). No cloud, no account, nothing leaves the machine.

The popup is frosted glass in the style of macOS Spotlight, and it opens on the screen your mouse is on. While it works, a soft rainbow glow runs round its edge and each step shows as it happens ("Searching inside your files… 12 found"); answers stream in word by word. Files and folders in answers are chips: click to open, ⋯ or right-click to show in Explorer (programs only ever show in Explorer, a click never runs them). Code has a Copy button, and so does every answer. The strip at the bottom shows the AI model, the index, your next reminder, CPU, memory and battery.

Open the app from the tray menu (**Open LuckyLook app**), the Start menu (type *LuckyLook*; LuckyLook adds itself there when it starts), or ⤢ in the popup.

## Requirements

- Windows 11 (Windows 10 works, just without rounded corners). The windows are drawn by the web view built into Windows (WebView2, the same engine as Edge).
- [Python](https://www.python.org/downloads/) 3.10 or newer, and [Ollama](https://ollama.com/download). The installer sets these up for you if they're missing.
- Any PC with 4 GB of RAM or more. LuckyLook picks the AI model that fits your hardware (see `models.json`): a graphics card with 8 GB gets the recommended `qwen3.5:9b`, a bigger card gets a bigger model, and smaller cards or PCs without one get a smaller model that runs on the CPU.

## Install (easy way)

1. On this page, click **Code › Download ZIP**, then unzip it somewhere permanent (for example `Documents\LuckyLook`).
2. Double-click **`install.bat`**.

It checks for Python and Ollama and installs them with winget if they're missing. It then downloads the AI models (several GB, one time only), sets everything up, runs a self-check, adds LuckyLook to Windows startup and starts it. Don't want it to start with Windows? Run `install.bat -NoStartup`.

## Install (by hand)

In a terminal:

```
git clone https://github.com/SathyaMaragani/LuckyLook
cd LuckyLook
ollama pull qwen3.5:9b
ollama pull nomic-embed-text
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python test_lucky.py
.venv\Scripts\pythonw lucky.py
```

(`qwen3.5:9b` is for an 8 GB graphics card. With a different PC, pull the model `models.json` lists for it instead; `install.bat` does this for you.)

LuckyLook now runs in the background: press **Ctrl+Alt+L**. The first time, it indexes every fixed drive, and that can take an hour or more on a big disk. The strip at the bottom of the popup shows progress, and you can ask questions meanwhile. After that it refreshes every 6 hours (counted from the last time, so restarting doesn't redo it), and only re-reads files that changed. To pick up new files right away: tray menu › **Reindex now**. If you'd rather have changes show up within seconds, set `LIVE = True` in `local_settings.py`; that keeps a small background job running, which you may notice on battery. Search by meaning builds up in the background too, at about 20 documents a second with a graphics card.

Optional: copy `me.example.md` to `me.md` and tell it about yourself (your name, your work, where your projects live).

**Start with Windows:** run this in PowerShell from the LuckyLook folder:

```powershell
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$([Environment]::GetFolderPath('Startup'))\LuckyLook.lnk"); $s.TargetPath = "$PWD\.venv\Scripts\pythonw.exe"; $s.Arguments = "`"$PWD\lucky.py`""; $s.WorkingDirectory = "$PWD"; $s.IconLocation = "$PWD\assets\logo.ico"; $s.Save()
```

## Using it

| | |
|---|---|
| **Ctrl+Alt+L**, click the top tab, or click the tray icon | open or close the popup |
| tray menu › **Open LuckyLook app**, the Start menu, or ⤢ in the popup | the app window: chats and *At a glance* |
| right-click the tray icon | menu: open, open the app, show or hide the top tab, **Reindex now**, **About me…**, **Private folders and windows…**, **Change hotkey…**, restart, quit |
| **Enter** / **Esc** | ask / hide (in the app, **Shift+Enter** is a new line) |
| ■ while it answers | stop the answer |
| `/new` | start a fresh conversation |
| `/widget` | hide or show the top tab |
| `/quit` | close LuckyLook |

The top tab gets out of the way by itself during full-screen videos, games and slideshows.

## Privacy

- Nothing is sent anywhere. The model, the index (`index.db`) and your notes all stay in this folder.
- Telegram, once you connect it, is your own account through Telegram's official API: reading and downloading fetch your messages from Telegram itself; the reading and summarising happen on this PC. The login (`telegram.session`, `telegram.json`) stays in this folder and is never uploaded; anyone with a copy of it could use your Telegram, so don't share the folder. To disconnect, delete those two files (and end the session in Telegram › Settings › Devices).
- Voice runs on this PC too: Whisper understands you, Kokoro speaks (a natural female voice, `af_heart`), and Vosk listens for "Hey Lucky" (`voice-models/`, about 950 MB, downloaded once by `install.bat`). While "Hey Lucky" is on, the microphone stays open and Windows shows its mic icon; nothing is recorded or kept until it hears the phrase. Turn it off in the tray menu (**Listen for "Hey Lucky"**); the 🎤 button still works.
- It reads file *names* everywhere on your fixed drives, and the *text* of documents and code outside system folders. Secrets files (`.env`, SSH keys, `.pem`/`.key`/`.pfx`, KeePass databases) are never read. OneDrive files that live only in the cloud are not downloaded.
- It only looks at your screen, clipboard or browser history when you ask a question that needs them. LuckyLook's own windows never appear in screenshots or screen shares (that's also how their glass sees what's behind them).
- It never changes anything without asking: opening, starting and closing apps, opening websites, creating folders, writing and editing files, running commands, moving and deleting each wait for your **Allow**. It shows you what it will write, change or run first. Deleting goes to the Recycle Bin, writing over or editing a file sends the old version there too, and moving never overwrites. Commands that name a private folder are refused, but a command can reach anything you can, so read it before you allow it.
- Websites open in your Edge profile set by `EDGE_PROFILE` (default `Default`), never a private one. To play a song or video, LuckyLook itself looks up YouTube's top result for what you asked (after your Allow, without cookies or any account).
- What goes out to the internet: your questions' web searches (to DuckDuckGo), the city for weather (to wttr.in), the public pages it reads, YouTube searches, and Telegram if you connect it. Never your files. It can't read pages on your PC or home network.
- **Keep things out:** tray menu › **Private folders and windows…** (or copy `local_settings.example.py` to `local_settings.py`). List folders LuckyLook must never touch (for example a browser profile), and window titles to black out whenever it looks at your screen. To keep browsing history out entirely, add `HISTORY = []`. Then tray menu › Restart LuckyLook.

## Settings

Put overrides in `local_settings.py`: `HOTKEY`, `MODEL`, `ROOTS` (which drives to index), `REINDEX_HOURS`, `LIVE`, `EDGE_PROFILE`, `PRIVATE`, `PRIVATE_WINDOWS`, `HISTORY`, and for voice `WAKE_WORD`, `WHISPER_MODEL`, `VOICE` (a Kokoro voice: `af_heart`, `af_bella`, `af_sarah`, `bf_emma`, `bf_isabella`, `hf_alpha` for Hindi; or a [Piper voice](https://huggingface.co/rhasspy/piper-voices)), `SPEED`, `SILENCE_SECONDS`. Restart LuckyLook from the tray menu afterwards. Other constants are at the top of each file: `lucky.py` has the look and the re-index timings.

**Memory:** LuckyLook itself uses about 270 MB of RAM (its windows are drawn by Windows' web view), about 400 MB while the app window is open, and around 1% of one CPU core when idle. The AI model uses about 5.5 GB of graphics memory while answering, and Ollama unloads it after 5 minutes without questions. The installer also sets `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0`, which halve the memory the model needs for the conversation (512 → 272 MB at LuckyLook's 16k context); they apply from Ollama's next start.

**Which model:** `models.json` maps hardware to a model: by graphics card memory, or by RAM when there's no usable card. Edit it to change the choices for everyone; set `MODEL` in `local_settings.py` to force one on your own PC. To see what your PC gets: `.venv\Scripts\python -c "import agent; print(agent.specs(), agent.MODEL)"`. Any Ollama model with tool calling works. For screen and image questions, it also needs vision. `bench.py` compares models on questions about your PC: `.venv\Scripts\python bench.py qwen3.5:9b qwen3:8b --think`.

## Files

- `lucky.py`: the popup, the app window, the top tab, the hotkey and tray, and keeping the index fresh. Errors go to `lucky.log`.
- `voice.py`: the microphone, "Hey Lucky", speech to text and text to speech.
- `ui/`: how the windows look (`style.css`), the conversation (`chat.js`), and the popup, app and tab pages. Plain files, nothing loaded from the internet.
- `agent.py`: the model and its tools. Also usable from a terminal: `.venv\Scripts\python agent.py "where is my resume?"`
- `indexer.py`: builds `index.db`. Run it by hand for a folder: `.venv\Scripts\python indexer.py D:\Projects`
- `install.bat` / `install.ps1`: the one-click installer. Safe to run again: it skips what's already done.
- `models.json`: which AI model fits which hardware.
- `test_lucky.py`: self-check. `bench.py`: model comparison. `assets/`: the logo and screenshots.

## License

MIT. See [LICENSE](LICENSE).
