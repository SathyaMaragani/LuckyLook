# Copy this file to local_settings.py (or use the tray menu: Private folders and windows…) and remove the # in
# front of the lines you want. local_settings.py stays on your PC (it's in .gitignore).
# Any setting at the top of indexer.py, agent.py or lucky.py can be overridden here. Restart LuckyLook
# afterwards (tray icon > Restart LuckyLook).

# Folders LuckyLook must never index (not even file names), list, read or search. Environment variables work.
# Example: an Edge browser profile you keep private (find its folder name in edge://version, "Profile path").
# PRIVATE = [r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Profile 3"]

# Window titles blacked out whenever LuckyLook looks at your screen (PowerShell -like patterns).
# Edge puts the profile name in its window titles: "Some page - Work - Microsoft Edge".
# PRIVATE_WINDOWS = ["* - Work - Microsoft*"]

# Which drives to index (default: every fixed drive).
# ROOTS = ["C:\\", "D:\\"]

# Which local model to use (any Ollama model with tool calling; vision needed for screen and image questions).
# MODEL = "qwen3.5:9b"

# The shortcut that opens LuckyLook (also settable from the tray menu: Change hotkey).
# HOTKEY = "Ctrl+Alt+L"

# The Edge profile LuckyLook opens websites in: its folder name from edge://version, "Profile path" (default "Default").
# EDGE_PROFILE = "Profile 1"
