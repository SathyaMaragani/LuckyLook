# LuckyLook installer: checks Python and Ollama (installs them with winget if missing), downloads the model,
# sets up LuckyLook in this folder, adds it to Windows startup and starts it.
#   install.bat                   everything
#   install.bat -NoStartup        don't start LuckyLook with Windows
#   install.bat -NoLaunch         don't start it now
param([switch]$NoStartup, [switch]$NoLaunch)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`n$text" -ForegroundColor Red; exit 1 }
function Version($exe, [string[]]$pre) {
    try { & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null } catch {}
}
function WingetInstall($id, $name) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    Write-Host "Installing $name with winget..."
    winget install -e --id $id
    return $LASTEXITCODE -eq 0
}

Step "Python 3.10 or newer"
$py = $null
foreach ($c in "py -3", "python") {  # the py launcher first: "python" can be the Microsoft Store stand-in
    $exe, $pre = $c -split " "
    $pre = @($pre | Where-Object { $_ })
    $v = Version $exe $pre
    if ($v -and [version]$v -ge [version]"3.10") { $py, $pyPre = $exe, $pre; break }
}
if (-not $py) {
    if (WingetInstall "Python.Python.3.12" "Python") {
        Fail "Python is installed. Close this window and run install.bat again, so Windows picks it up."
    }
    Fail "Python not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run install.bat again."
}
Write-Host "found: $c (Python $v)"

Step "Ollama"
$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) { $ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
if (-not (Test-Path $ollama)) {
    if (-not (WingetInstall "Ollama.Ollama" "Ollama")) {
        Fail "Ollama not found. Install it from https://ollama.com/download, then run install.bat again."
    }
}
function OllamaUp { try { Invoke-RestMethod http://localhost:11434/api/tags -TimeoutSec 3 } catch { $null } }
if (-not (OllamaUp)) {
    Write-Host "starting Ollama..."
    Start-Process $ollama "serve" -WindowStyle Hidden
    foreach ($i in 1..30) { if (OllamaUp) { break }; Start-Sleep 1 }
    if (-not (OllamaUp)) { Fail "Ollama didn't start. Open the Ollama app once, then run install.bat again." }
}
Write-Host "running"
# less graphics memory for the conversation (16k words of context: 512 MB -> 272 MB); used from Ollama's next start
foreach ($kv in @{ OLLAMA_FLASH_ATTENTION = "1"; OLLAMA_KV_CACHE_TYPE = "q8_0" }.GetEnumerator()) {
    if (-not [Environment]::GetEnvironmentVariable($kv.Key, "User")) {
        [Environment]::SetEnvironmentVariable($kv.Key, $kv.Value, "User")
        Write-Host "set $($kv.Key)=$($kv.Value) (takes effect when Ollama next starts)"
    }
}

Step "LuckyLook's own Python environment"
if (-not (Test-Path .venv\Scripts\python.exe)) {
    & $py @pyPre -m venv .venv
    if ($LASTEXITCODE) { Fail "Couldn't create the Python environment (.venv)." }
}
.venv\Scripts\python -m pip install --disable-pip-version-check -q -r requirements.txt
if ($LASTEXITCODE) { Fail "Couldn't install the Python packages (needs internet)." }
Write-Host "ready"

Step "The AI models"
# models.json picks the chat model that fits this PC (MODEL in local_settings.py overrides it)
.venv\Scripts\python -c "import agent; v, r = agent.specs(); print(f'This PC: {v:.0f} GB graphics card memory, {r:.0f} GB RAM, so LuckyLook uses {agent.MODEL}')"
$have = (OllamaUp).models.name
# the chat model, and the small one that lets it search documents by meaning
foreach ($model in (.venv\Scripts\python -c "import agent, indexer; print(agent.MODEL); print(indexer.EMBED_MODEL)")) {
    if ($have -contains $model -or $have -contains "$model`:latest") {
        Write-Host "$model is already downloaded"
    } else {
        Write-Host "downloading $model (one time only; the chat model is several GB)..."
        & $ollama pull $model
        if ($LASTEXITCODE) { Fail "Couldn't download $model." }
    }
}

Step "Self-check"
if (-not (Test-Path me.md)) { Copy-Item me.example.md me.md }
.venv\Scripts\python test_lucky.py
if ($LASTEXITCODE) { Fail "The self-check failed (see above). Please open an issue on GitHub with this output." }

function Shortcut($folder, $arguments) {
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut("$([Environment]::GetFolderPath($folder))\LuckyLook.lnk")
    $s.TargetPath = "$PWD\.venv\Scripts\pythonw.exe"
    $s.Arguments = "`"$PWD\lucky.py`"$arguments"
    $s.WorkingDirectory = "$PWD"
    $s.IconLocation = "$PWD\assets\logo.ico"
    $s.Save()
}
Step "Start menu"
Shortcut "Programs" " --app"  # opens the LuckyLook app window (starting LuckyLook first if needed)
Write-Host "added LuckyLook to the Start menu"
if (-not $NoStartup) {
    Step "Start with Windows"
    Shortcut "Startup" ""
    Write-Host "added (remove it any time: Win+R, shell:startup, delete LuckyLook)"
}

if (-not $NoLaunch) {
    Step "Starting LuckyLook"
    $running = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | Where-Object CommandLine -like "*lucky.py*"
    if ($running) { Write-Host "already running" } else { Start-Process .venv\Scripts\pythonw.exe "`"$PWD\lucky.py`"" }
}

Write-Host "`nDone! Press Ctrl+Alt+L anywhere to ask LuckyLook something." -ForegroundColor Green
Write-Host "The first scan of your drives runs in the background and can take an hour or more; you can ask questions meanwhile."
Write-Host "Tell it about yourself in me.md, and keep folders private with local_settings.py (see README)."
