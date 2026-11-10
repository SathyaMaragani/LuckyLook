# LuckyLook

<img src="assets/logo.png" width="96" alt="LuckyLook logo">

**A private AI for your Windows PC that knows what's on it.** Press **Ctrl+Alt+L** anywhere (or click the thin blue tab hanging from the top of your screen), ask a question, and it answers from your own computer:

<img src="assets/screenshot.png" width="640" alt="The LuckyLook popup: frosted glass over a colourful background, answering how much free space each drive has">

<img src="assets/screenshot-tab.png" width="320" alt="The LuckyLook tab at the top of the screen, pulled down on hover">

- **Files and documents:** "where is my resume?", "what did I quote the client for the logo?", "what's eating my disk?". Searches inside documents, PDFs, Word files, notes and code.
- **Your PC:** free space, installed apps, what's using memory, battery, specs.
- **What you were doing:** recently opened files, and websites you visited (Edge and Chrome history).
- **What's on your screen:** "what does this error mean?" It looks at your screen. Also images and photos ("what does this receipt say?").
- **What you copied:** text, files, or a Win+Shift+S snip.
- **Remembers you:** "remember that I prefer short answers" goes into `memory.md`. Tell it about yourself in `me.md`.

Everything runs **on your computer** with [Ollama](https://ollama.com). No cloud, no account, nothing leaves the machine.

The popup is a frosted-glass bar in the style of macOS Spotlight. File paths in answers are clickable: click to open, right-click to show in Explorer.

## Requirements

- Windows 11 (Windows 10 works, just without rounded corners)
- [Python](https://www.python.org/downloads/) 3.10 or newer, and [Ollama](https://ollama.com/download). The installer sets these up for you if they're missing.
- A graphics card with 8 GB of memory is ideal. It also runs on the CPU, just slower.

## Install (easy way)

1. On this page, click **Code › Download ZIP**, then unzip it somewhere permanent (for example `Documents\LuckyLook`).
2. Double-click **`install.bat`**.

It checks for Python and Ollama and installs them with winget if they're missing. It then downloads the AI model (several GB, one time only), sets everything up, runs a self-check, adds LuckyLook to Windows startup and starts it. Don't want it to start with Windows? Run `install.bat -NoStartup`.

## Install (by hand)

In a terminal:

```
git clone https://github.com/SathyaMaragani/LuckyLook
cd LuckyLook
ollama pull qwen3.5:9b
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python test_lucky.py
.venv\Scripts\pythonw lucky.py
```

LuckyLook now runs in the background: press **Ctrl+Alt+L**. The first time, it indexes every fixed drive, and that can take an hour or more on a big disk. The status line at the bottom of the popup shows progress, and you can ask questions meanwhile. After that it re-indexes every 6 hours, and only changed files are read again.

Optional: copy `me.example.md` to `me.md` and tell it about yourself (your name, your work, where your projects live).

**Start with Windows:** run this in PowerShell from the LuckyLook folder:

```powershell
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$([Environment]::GetFolderPath('Startup'))\LuckyLook.lnk"); $s.TargetPath = "$PWD\.venv\Scripts\pythonw.exe"; $s.Arguments = "`"$PWD\lucky.py`""; $s.WorkingDirectory = "$PWD"; $s.IconLocation = "$PWD\assets\logo.ico"; $s.Save()
```

## Using it

| | |
|---|---|
| **Ctrl+Alt+L**, click the top tab, or click the tray icon | open or close the popup |
| right-click the tray icon | menu: open, show or hide the top tab, quit |
| **Enter** / **Esc** | ask / hide |
| `/new` | start a fresh conversation |
| `/widget` | hide or show the top tab |
| `/quit` | close LuckyLook |

The top tab gets out of the way by itself during full-screen videos, games and slideshows.

## Privacy

- Nothing is sent anywhere. The model, the index (`index.db`) and your notes all stay in this folder.
- It reads file *names* everywhere on your fixed drives, and the *text* of documents and code outside system folders. Secrets files (`.env`, SSH keys, `.pem`/`.key`/`.pfx`, KeePass databases) are never read. OneDrive files that live only in the cloud are not downloaded.
- It only looks at your screen, clipboard or browser history when you ask a question that needs them. The popup itself never appears in screenshots or screen shares.
- **Keep things out:** copy `local_settings.example.py` to `local_settings.py`. List folders LuckyLook must never touch (for example a browser profile), and window titles to black out whenever it looks at your screen. To keep browsing history out entirely, add `HISTORY = []`.

## Settings

Put overrides in `local_settings.py`: `MODEL`, `ROOTS` (which drives to index), `PRIVATE`, `PRIVATE_WINDOWS`, `HISTORY`. Other constants are at the top of each file: `lucky.py` has the hotkey, the look and the re-index interval.

**Other models:** any Ollama model with tool calling works. For screen and image questions, it also needs vision. `bench.py` compares models on questions about your PC: `.venv\Scripts\python bench.py qwen3.5:9b qwen3:8b --think`.

## Files

- `lucky.py`: the popup, hotkey and top tab. Errors go to `lucky.log`.
- `agent.py`: the model and its tools. Also usable from a terminal: `.venv\Scripts\python agent.py "where is my resume?"`
- `indexer.py`: builds `index.db`. Run it by hand for a folder: `.venv\Scripts\python indexer.py D:\Projects`
- `install.bat` / `install.ps1`: the one-click installer. Safe to run again: it skips what's already done.
- `test_lucky.py`: self-check. `bench.py`: model comparison. `assets/`: the logo and screenshots.

## License

MIT. See [LICENSE](LICENSE).
