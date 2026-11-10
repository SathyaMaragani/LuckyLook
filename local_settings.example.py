# Copy this file to local_settings.py and edit it. local_settings.py stays on your PC (it's in .gitignore).
# Any setting at the top of indexer.py or agent.py can be overridden here.

# Folders LuckyLook must never index (not even file names), list, read or search. Environment variables work.
# Example: an Edge browser profile you keep private (find its folder name in edge://version, "Profile path").
PRIVATE = [r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Profile 3"]

# Window titles blacked out whenever LuckyLook looks at your screen (PowerShell -like patterns).
# Edge puts the profile name in its window titles: "Some page - Work - Microsoft Edge".
PRIVATE_WINDOWS = ["* - Work - Microsoft*"]

# Which drives to index (default: every fixed drive).
# ROOTS = ["C:\\", "D:\\"]

# Which local model to use (any Ollama model with tool calling; vision needed for screen and image questions).
# MODEL = "qwen3.5:9b"
