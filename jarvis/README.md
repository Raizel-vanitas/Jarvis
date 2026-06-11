# JARVIS — GUI Edition

A voice-activated AI assistant with a dark interface, powered by Ollama (llama3.2).
Fully offline. No API keys needed.(Also if it says missing dependencies js install them plz)

---

#Models
You can use different AI models for smarter responses, by default llama3.2 is selected. personally i'd recommend qwen2.5-coder:7b which u can edit in the config folder.


# Python 3.14!

## Files in this package

| File | What it does |
|---|---|
| `jarvis_gui.py` | The main JARVIS app (GUI version) |
| `install.bat` | Installs all Python dependencies + pulls the AI model |
| `setup_startup.bat` | Makes JARVIS start automatically when you log in |

---

## Setup (do this once)

**Step 1 — Install Python 3.14**
Download from https://www.python.org/downloads/
> ✅ Tick **"Add Python to PATH"** during install

**Step 2 — Install Ollama**
Download from https://ollama.com and install it.

**Step 3 — Run the installer**
Double-click `install all.bat`. It will install all Python packages and pull the AI model.
(if doesnt work i will list dependencies to install below!)

**Step 4 — Start JARVIS**
Double-click `jarvis_gui.py`, or run:
```
python jarvis_gui.py
```

**Step 5 (optional) — Start on boot**
Double-click `setup_startup.bat` to make JARVIS launch automatically when you log in.
To remove it later: open Task Manager → Startup apps → disable JARVIS.

---

# Dependencies
requests
psutil
pyttsx3
SpeechRecognition
pyaudio
edge-tts
pygame
sounddevice
numpy
pygetwindow
plyer
pystray
pillow
pyautogui
pyperclip
pywin32

## Daily use

- **Voice:** say your wake word (default: *"Jarvis"*) then speak your command
- **Text:** type in the input box at the bottom and press Enter
- **Mic toggle:** click the 🎤 button to pause/resume voice listening
- **Settings:** click ⚙ Settings in the top bar to change your name, wake word, mic, etc.

### Example commands
```
open Spotify
what's on my desktop?
scan my Downloads for threats
what processes are running?
system status
kill notepad
create a file called todo.txt
search Google for Python tutorials
```

---

## Troubleshooting

**"Ollama not found" error on startup**
Run `ollama serve` in a terminal before launching JARVIS.

**No voice / microphone issues**
Open Settings (⚙) and pick a different microphone from the dropdown.

**pyaudio install failed**
Run in a terminal:
```
pip install pipwin
pipwin install pyaudio
```

---

## Sending to a friend

Just zip up the whole folder and send it. They need to:
1. Install Python 3.14 (with "Add to PATH" ticked)
2. Install Ollama from https://ollama.com
3. Run `install.bat`
4. Run `jarvis_gui.py`

JARVIS will run the first-time setup wizard automatically on their machine.
