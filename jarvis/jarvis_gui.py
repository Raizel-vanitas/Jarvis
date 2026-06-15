try:
    from playsound import playsound as _playsound_orig
    def playsound(path): _playsound_orig(path)
except ImportError:
    def playsound(path):
        try:
            import pygame as _pg
            _pg.mixer.music.load(path)
            _pg.mixer.music.play()
            while _pg.mixer.music.get_busy():
                import time; time.sleep(0.05)
        except Exception as _e:
            print(f"[playsound fallback] {_e}")
import os, sys, json, re, shutil, time, threading, subprocess, webbrowser, math
from jarvis_learning import (
    lookup_learned, teach_jarvis, forget_trigger,
    list_learned, interactive_teach, increment_hits,
)
import urllib.parse, html, queue
import tkinter as tk
from tkinter import ttk, scrolledtext, simpledialog, messagebox
import requests, psutil, pyttsx3
try:
    import pyaudio as _pyaudio_check  # noqa
    _PYAUDIO_OK = True
except ImportError:
    _PYAUDIO_OK = False

import speech_recognition as sr
try:
    import edge_tts
    import asyncio
    import tempfile
    _EDGE_TTS_OK = True
except ImportError:
    _EDGE_TTS_OK = False

try:
    import pygame
    pygame.mixer.init()
    _PYGAME_OK = True
except Exception:
    _PYGAME_OK = False


if not _PYAUDIO_OK:
    try:
        import sounddevice as _sd
        import numpy as _np
        _SOUNDDEVICE_OK = True
    except ImportError:
        _SOUNDDEVICE_OK = False
else:
    _SOUNDDEVICE_OK = False
import pygetwindow as gw
from pathlib import Path
from datetime import datetime, timedelta
from plyer import notification
from pystray import Icon, Menu, MenuItem
from PIL import Image, ImageDraw, ImageTk
try:
    import pyautogui
    pyautogui.FAILSAFE = True   
    PYAUTOGUI_OK = True
except Exception as _pag_err:
    print(f"[JARVIS] pyautogui failed to initialise: {_pag_err}")
    PYAUTOGUI_OK = False

try:
    import pyperclip
    PYPERCLIP_OK = True
except ImportError:
    PYPERCLIP_OK = False


_reminder_queue: queue.Queue = queue.Queue()


NOTES_FILE = Path(__file__).parent / "jarvis_notes.json"



CONFIG_FILE = Path(__file__).parent / "jarvis_config.json"

DEFAULT_CONFIG = {
    "owner_name":       "",
    "mic_index":        None,
    "voice_speed":      175,
    "monitor_interval": 60,
    "cpu_alert":        90,
    "ram_alert":        90,
    "model":            "llama3.2",
    "wake_word":        "jarvis",
    "ollama_url":       "http://localhost:11434/api/chat",
}

CFG      = {}
HOME_DIR = Path.home()


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
            return {**DEFAULT_CONFIG, **saved}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)



SUSPICIOUS_EXT   = {".exe",".bat",".cmd",".vbs",".ps1",".scr",".pif",".com",".jar",".hta",".jse",".wsf"}
SUSPICIOUS_NAMES = ["keylog","trojan","rootkit","ransomware","cryptominer","backdoor",
                    "stealer","rat","worm","botnet","exploit","payload","mimikatz","metasploit"]
TEMP_PATHS       = ["\\temp\\","\\tmp\\","\\appdata\\local\\temp\\","\\appdata\\roaming\\temp\\"]



chat_history = []
tts_engine   = None
tray_icon    = None
listening    = True
running      = True
gui_app      = None   # reference to the main GUI window

# ── TTS queue: all audio is serialised through a single background thread ──
_tts_queue: queue.Queue = queue.Queue()

# ── TTS interrupt: set this event to stop speech mid-sentence ──
_tts_stop_event = threading.Event()
_speech_muted   = False  # persistent mute — only toggled by the mute button

_JARVIS_VOICE   = "en-GB-RyanNeural"  
_JARVIS_RATE    = "-3%"                 
_JARVIS_PITCH   = "-8Hz"               

def init_tts():
    """Initialise pyttsx3 as fallback only; edge-tts is used when available."""
    global tts_engine
    if _EDGE_TTS_OK:
        tts_engine = None  
        return
    try:
        tts_engine = pyttsx3.init()
        tts_engine.setProperty("rate", CFG.get("voice_speed", 160))
        voices = tts_engine.getProperty("voices")
        chosen = None
        for v in voices:
            if any(w in v.name.lower() for w in ("david","mark","james","daniel","george","male")):
                chosen = v
                break
        if chosen:
            tts_engine.setProperty("voice", chosen.id)
    except Exception:
        tts_engine = None


# Dedicated pygame channel for all JARVIS speech — lets us call .stop()
# on exactly this channel without touching other pygame sounds.
_tts_channel: "pygame.mixer.Channel | None" = None

def _init_tts_channel():
    global _tts_channel
    if _PYGAME_OK:
        try:
            pygame.mixer.set_num_channels(8)
            _tts_channel = pygame.mixer.Channel(7)  # reserve channel 7 for speech
        except Exception:
            _tts_channel = None

_init_tts_channel()


def _speak_edge_sync(text: str):
    """Synthesise speech via edge-tts, load into pygame Sound (in-memory),
    and play on the dedicated speech channel so it can be hard-stopped."""
    async def _synthesise():
        communicator = edge_tts.Communicate(
            text,
            voice=_JARVIS_VOICE,
            rate=_JARVIS_RATE,
            pitch=_JARVIS_PITCH,
        )
        import io
        buf = io.BytesIO()
        async for chunk in communicator.stream():
            # Abort synthesis mid-stream if interrupted
            if _tts_stop_event.is_set():
                return None
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        buf.seek(0)
        return buf

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            buf = loop.run_until_complete(_synthesise())
        finally:
            loop.close()

        if buf is None or _tts_stop_event.is_set():
            return  # interrupted during synthesis or before playback

        if _PYGAME_OK and _tts_channel is not None:
            sound = pygame.mixer.Sound(buf)
            _tts_channel.play(sound)
            # Poll until done or interrupted
            while _tts_channel.get_busy():
                if _tts_stop_event.is_set():
                    _tts_channel.stop()
                    # Flush pygame audio buffer immediately
                    try:
                        pygame.mixer.stop()
                    except Exception:
                        pass
                    return
                time.sleep(0.03)
        else:
            # Fallback: write to temp file and use playsound
            import io as _io
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(buf.read())
                tmp_path = f.name
            try:
                playsound(tmp_path)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    except Exception as e:
        print(f"[edge-tts] {e}")


def _tts_worker():
    while True:
        text = _tts_queue.get()
        if text is None:
            break
        try:
            if _tts_stop_event.is_set():
                # Discard stale queued item
                continue
            if _EDGE_TTS_OK:
                _speak_edge_sync(text)
            elif tts_engine:
                try:
                    tts_engine.say(text)
                    tts_engine.runAndWait()
                except Exception:
                    pass
        except Exception as e:
            print(f"[tts_worker] {e}")
        finally:
            _tts_queue.task_done()


def interrupt_speech():
    """
    Hard-stop all current and queued speech immediately.
    Sets the stop flag and stops pygame — does NOT clear the flag.
    The flag stays set until speak() is called again, which clears it
    just before queuing new audio. This prevents the race where the flag
    gets cleared before the worker thread has actually stopped.
    """
    _tts_stop_event.set()
    if _PYGAME_OK:
        try:
            if _tts_channel is not None:
                _tts_channel.stop()
            pygame.mixer.stop()
        except Exception:
            pass
    # Drain all pending items from the queue
    while not _tts_queue.empty():
        try:
            _tts_queue.get_nowait()
            _tts_queue.task_done()
        except Exception:
            break
    # Deactivate particle system when speech is interrupted
    if gui_app:
        gui_app.after(0, lambda: gui_app._set_speech_active(False))


# Start the TTS worker once at import time
_tts_thread = threading.Thread(target=_tts_worker, daemon=True, name="tts-worker")
_tts_thread.start()


def _split_sentences(text: str) -> list:
    """Split text into sentence chunks so the TTS queue has many small items
    that can each be drained/skipped if interrupt_speech() fires between them."""
    # Split on sentence-ending punctuation followed by whitespace or end
    chunks = re.split(r'(?<=[.!?])\s+', text.strip())
    # Also split very long chunks on comma+space to keep chunks short
    result = []
    for chunk in chunks:
        if len(chunk) > 120:
            sub = re.split(r',\s+', chunk)
            result.extend(s.strip() for s in sub if s.strip())
        elif chunk.strip():
            result.append(chunk.strip())
    return result if result else [text]


def speak(text: str):
    """
    Non-blocking speak: strips markdown, updates the GUI immediately,
    then queues each sentence individually so interrupt_speech() can
    drain between sentences for a near-instant cutoff.
    Clears the stop flag first so new speech actually plays.
    """
    clean = re.sub(r'[*_`#]', '', text)
    clean = re.sub(r'\{[^}]*\}', '', clean).strip()
    if not clean:
        return
    if _speech_muted:
        # Still show text in GUI but don't play audio
        if gui_app:
            gui_app.after(0, lambda t=clean: gui_app.add_message("JARVIS", t, tag="jarvis"))
        return
    # Clear any pending stop so this new speech plays normally
    _tts_stop_event.clear()
    if gui_app:
        gui_app.after(0, lambda t=clean: gui_app.add_message("JARVIS", t, tag="jarvis"))
        # Activate particle system when speech starts
        gui_app.after(0, lambda: gui_app._set_speech_active(True))
    # Queue each sentence separately so the stop flag is checked between them
    for sentence in _split_sentences(clean):
        _tts_queue.put(sentence)
    # Deactivate particle system slightly after speech ends
    if gui_app:
        gui_app.after(len(clean) * 80, lambda: gui_app._set_speech_active(False))


def voice_confirm(prompt: str) -> bool:
    """
    Ask the user a yes/no question via voice (and show it in the GUI).
    IMPORTANT: must NOT be called from the main/GUI thread as it calls
    listen_for_command which blocks.  Always call from a daemon command thread.
    """
    speak(prompt + " Say YES to confirm, or NO to cancel.")
    if gui_app:
        gui_app.after(0, lambda: gui_app.set_status("🎤 Awaiting confirmation…"))

    raw = listen_for_command("Say YES or NO…")
    if not raw:
        speak("Nothing heard, sir. I'll leave it.")
        return False

    r = raw.lower().strip()
    YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "confirm", "do it",
                 "affirmative", "go ahead", "proceed", "ok", "okay", "aye"}
    NO_WORDS  = {"no", "nope", "nah", "cancel", "abort", "stop", "don't",
                 "negative", "forget it", "never mind"}

    if any(w in r for w in YES_WORDS):
        return True
    if any(w in r for w in NO_WORDS):
        speak("Fair enough. Cancelling.")
        return False

    speak(f"Caught '{raw}' but couldn't work out what you meant. Playing it safe and cancelling.")
    return False



def _make_recognizer(wake_word_mode: bool = False) -> sr.Recognizer:
    r = sr.Recognizer()
    # Lower threshold = more sensitive to quiet speech.
    # Wake word mode uses a more aggressive setting so soft utterances are caught.
    r.energy_threshold         = 150 if wake_word_mode else 200
    r.dynamic_energy_threshold = True
    r.dynamic_energy_adjustment_damping = 0.12   # adapts faster to room noise
    r.pause_threshold          = 0.6             # shorter pause = snappier cutoff
    r.non_speaking_duration    = 0.4
    return r


def _capture_audio_sounddevice(duration: float = 5, samplerate: int = 16000) -> sr.AudioData:
    """Record audio via sounddevice with voice activity detection."""
    CHUNK       = int(samplerate * 0.1)
    SILENCE_DB  = 80    # RMS threshold — higher = less background noise triggers
    MIN_SPEECH  = 0.2   # seconds of speech before we start recording
    MAX_SILENCE = 1.2   # seconds of silence after speech before cutting off
    MAX_TOTAL   = 15.0  # hard cap

    frames        = []
    speech_frames = 0
    silence_frames= 0
    total_frames  = 0
    max_chunks    = int(MAX_TOTAL / 0.1)
    min_speech_chunks  = int(MIN_SPEECH / 0.1)
    max_silence_chunks = int(MAX_SILENCE / 0.1)
    started = False

    with _sd.InputStream(samplerate=samplerate, channels=1, dtype='int16',
                         blocksize=CHUNK) as stream:
        while total_frames < max_chunks:
            chunk, _ = stream.read(CHUNK)
            rms = int(_np.sqrt(_np.mean(chunk.astype(_np.float32) ** 2)))
            frames.append(chunk.tobytes())
            total_frames += 1

            if rms > SILENCE_DB:
                started = True
                speech_frames += 1
                silence_frames = 0
            elif started:
                silence_frames += 1
                if silence_frames > max_silence_chunks and speech_frames > min_speech_chunks:
                    break

    raw = b"".join(frames)
    return sr.AudioData(raw, samplerate, 2)


def listen_for_command(prompt_text: str | None = None) -> str | None:
    mic_index = CFG.get("mic_index")   # always read live from CFG
    recognizer = _make_recognizer(wake_word_mode=False)

    if gui_app and prompt_text:
        gui_app.after(0, lambda: gui_app.set_status(f"🎤 {prompt_text}"))

    try:
        if _SOUNDDEVICE_OK:
            if mic_index is not None:
                _sd.default.device = mic_index   # always apply current selection
            if gui_app:
                gui_app.after(0, lambda: gui_app.set_status("🎤 Listening… (speak now)"))
            audio = _capture_audio_sounddevice()
        else:
            with sr.Microphone(device_index=mic_index) as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.3)
                audio = recognizer.listen(source, timeout=10, phrase_time_limit=20)

        raw_text = recognizer.recognize_google(audio).lower()
        corrected_text, changes = _apply_corrections(raw_text)

        if gui_app:
            gui_app.after(0, lambda: gui_app.add_message("You (voice)", raw_text, tag="user"))
            if changes:
                hint = _format_did_you_mean(raw_text, corrected_text, changes)
                gui_app.after(0, lambda h=hint: gui_app.add_message("JARVIS", h, tag="system"))
            gui_app.after(0, lambda: gui_app.set_status("Ready"))
        return corrected_text
    except (sr.WaitTimeoutError, sr.UnknownValueError):
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status("Ready"))
        return None
    except sr.RequestError as e:
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status(f"Speech API error: {e}"))
        return None
    except Exception as e:
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status(f"Mic error: {e}"))
        return None



import difflib as _difflib


_MISHEARING_MAP = {
    # Wake word variants
    "jarvis": ["jarvus", "jarbus", "jarves", "jarfis", "jarbis", "jarfish", "harvest", "travis"],
    # Action words
    "open":   ["hoppin", "openin", "opin", "opan"],
    "close":  ["cloze", "cloas", "clothes"],
    "delete": ["dileet", "delet", "delee"],
    "create": ["creigh", "criate", "crayate"],
    "search": ["surch", "serch", "sarch"],
    "install": ["instol", "instal", "instull"],
    "weather": ["whether", "wedder", "weva", "wether"],
    "volume":  ["voloom", "volum", "voluem"],
    "remind":  ["remine", "remined", "reminned"],
    "screenshot": ["screenshow", "screensho", "screen shot"],
    "clipboard": ["clipbord", "clibboard", "clipbard"],
    "notepad":   ["noatpad", "notpad", "note pad"],
    "terminal":  ["termenal", "terminel", "terminol"],
    "settings":  ["setings", "setngs", "settins"],
    "status":    ["statas", "statues", "statis"],
    "process":   ["prosess", "proccess", "processs"],
    "window":    ["windah", "winda", "windo"],
    "firefox":   ["fiafox", "firefocks", "firefax"],
    "chrome":    ["crome", "chrum", "croam"],
    "desktop":   ["desktap", "desktob", "deskop"],
    "download":  ["downlode", "downlod", "downlad"],
    "documents": ["documens", "docments", "documints"],
    "calculate": ["calclate", "calculat", "calcoolate"],
    "calculator":["calcalator", "calculata", "calcolator"],
    "network":   ["netwark", "netwerk", "nettwork"],
    "bluetooth": ["blutetooth", "bluetoof", "blutooth"],
    "password":  ["passward", "pasword", "passwerd"],
    "folder":    ["folda", "foldah", "foolder"],
    "file":      ["fale", "fiel", "fyle"],
    "system":    ["systim", "sistam", "sysem"],
    "memory":    ["memary", "memmory", "memry"],
    "monitor":   ["monitah", "moniter", "monitur"],
    "startup":   ["startop", "startip", "stortup"],
    "shutdown":  ["shutdahn", "shutdaan", "shutdwn"],
    "restart":   ["restort", "restaht", "restat"],
    "python":    ["pythan", "pithon", "piton"],
    "git":       ["geet", "jit", "gitt"],
    "clone":     ["cloan", "clome", "klon"],
    "yes":       ["yis", "yeas", "yeah", "yep", "yas"],
    "no":        ["nah", "naw", "nou"],
    "quit":      ["kwit", "quitt", "kwitt"],
    "clear":     ["clah", "cleah", "claer"],
    "mute":      ["myoot", "myute", "mewt"],
    "ping":      ["pin", "peng", "piing"],
    "note":      ["noat", "nowt", "nort"],
    "help":      ["halp", "hellp", "hep"],
}

# Reverse map: mishearing → correct word
_CORRECTION_LOOKUP: dict[str, str] = {}
for _correct, _variants in _MISHEARING_MAP.items():
    for _v in _variants:
        _CORRECTION_LOOKUP[_v] = _correct


def _apply_corrections(text: str) -> tuple[str, list[tuple[str, str]]]:
    if not text:
        return text, []

    _ALL_KNOWN = set(_MISHEARING_MAP.keys())
    words = text.split()
    corrected_words = []
    changes = []

    for word in words:
        w_lower = word.lower().strip(".,!?")

        if w_lower in _CORRECTION_LOOKUP:
            replacement = _CORRECTION_LOOKUP[w_lower]
            corrected_words.append(replacement)
            changes.append((word, replacement))
            continue

        if w_lower not in _ALL_KNOWN and len(w_lower) >= 4:
            close = _difflib.get_close_matches(w_lower, _ALL_KNOWN, n=1, cutoff=0.82)
            if close:
                replacement = close[0]
                corrected_words.append(replacement)
                changes.append((word, replacement))
                continue

        corrected_words.append(word)

    corrected = " ".join(corrected_words)
    return corrected, changes


def _format_did_you_mean(original: str, corrected: str, changes: list[tuple[str, str]]) -> str:
    if not changes:
        return ""
    parts = ", ".join(f"'{o}' → '{c}'" for o, c in changes)
    return f"[Accent correction: {parts}] Interpreting as: \"{corrected}\""


def wake_word_loop():
    global listening
    # Calibrate ambient noise once at startup, then reuse the threshold.
    # Re-calibrate every ~60 loops (~60 s) to adapt to changing environments.
    _calib_counter = 0
    _calib_interval = 60
    recognizer = _make_recognizer(wake_word_mode=True)

    while running:
        if not listening:
            time.sleep(0.5)
            continue

        # Always read live from CFG so settings changes take effect immediately
        mic_index = CFG.get("mic_index")
        wake_word = CFG.get("wake_word", "jarvis")

        try:
            if _SOUNDDEVICE_OK:
                _sd.default.device = mic_index  # always update, not just when None
                audio = _capture_audio_sounddevice(duration=3)
            else:
                with sr.Microphone(device_index=mic_index) as source:
                    # Only re-calibrate periodically, not every loop
                    if _calib_counter % _calib_interval == 0:
                        recognizer.adjust_for_ambient_noise(source, duration=0.5)
                    _calib_counter += 1
                    audio = recognizer.listen(source, timeout=5, phrase_time_limit=6)

            text = recognizer.recognize_google(audio).lower()
            corrected, _chg = _apply_corrections(text)
            if wake_word in corrected or wake_word in text:
                # Stop any ongoing speech immediately
                interrupt_speech()
                if gui_app:
                    def _restore_gui():
                        if gui_app.state() in ("withdrawn", "iconic"):
                            gui_app.deiconify()
                        gui_app.lift()
                        gui_app.focus_force()
                    gui_app.after(0, _restore_gui)
                if gui_app:
                    gui_app.after(0, gui_app.flash_wake)
                speak("Sir?")
                command = listen_for_command("Listening — speak your command…")
                if command:
                    # Always handle commands in a fresh daemon thread so
                    # wake_word_loop is free to keep listening immediately
                    threading.Thread(
                        target=handle_command, args=(command,), daemon=True,
                        name="cmd-wake"
                    ).start()
                else:
                    speak("Didn't catch that one, sir. Give it another go.")
        except (sr.WaitTimeoutError, sr.UnknownValueError):
            pass
        except sr.RequestError:
            time.sleep(2)
        except Exception:
            time.sleep(1)


# ─────────────────────────────────────────────
#  AI  (Ollama)
# ─────────────────────────────────────────────
def build_system_prompt() -> str:
    owner = CFG.get("owner_name", "sir")
    h     = str(HOME_DIR).replace("\\", "/")


    examples = """
=== OUTPUT FORMAT — FOLLOW EXACTLY ===

When you need to perform an action, output ONE JSON object on its own line, nothing else on that line.
Then add a short 1-2 sentence reply in plain English below it. Do NOT narrate what you "would" do.
Do NOT say "I cannot" for things that have a JSON action — just emit the JSON.

EXAMPLE 1 — user asks to open YouTube:
{"action": "open_link", "url": "https://youtube.com"}
Opening YouTube for you now, sir.

EXAMPLE 2 — user asks to move mouse to top right:
{"action": "mouse_move", "x": 1880, "y": 20}
Moving the cursor to the top-right corner, sir.

EXAMPLE 2b — user asks to left-click at a position:
{"action": "mouse_click", "x": 960, "y": 540, "button": "left", "double": false}
Left-clicking there now, sir.

MOUSE MOVEMENT RULES — READ CAREFULLY:
- "top right" / "top-right corner" → x near screen width, y near 0
- "bottom left" / "bottom-left corner" → x near 0, y near screen height
- "right" alone (e.g. "move it right", "move it just right", "a bit to the right") → RELATIVE nudge: add ~100px to current x. Use {"action": "mouse_move", "x": CURRENT_X+100, "y": CURRENT_Y}
- "left" alone → subtract ~100px from current x
- "up" alone → subtract ~100px from current y
- "down" alone → add ~100px to current y
- If the user gives exact pixel coordinates, use those directly.
- NEVER map a bare directional word like "right" to a screen corner. "Move it right" ≠ "move it to the top-right corner".

EXAMPLE 3 — user asks to switch tab:
{"action": "switch_tab", "direction": "next"}
Switching to the next tab.

EXAMPLE 4 — user asks what is on the desktop:
{"action": "list_files", "path": "REPLACE_WITH_REAL_HOME/Desktop"}
Scanning the desktop now, sir.

EXAMPLE 5 — user asks a general question (no action needed):
The speed of light is approximately 299,792 kilometres per second, sir.

EXAMPLE 6 — user asks to install a package:
{"action": "pip_install", "packages": "requests"}
Installing requests now, sir.

EXAMPLE 7 — user asks to clone a repo:
{"action": "git_clone", "url": "https://github.com/someuser/repo.git", "dest": "REPLACE_WITH_REAL_HOME/Projects"}
Cloning the repository into your Projects folder, sir.

EXAMPLE 8 — user asks for weather:
{"action": "weather", "location": "New York"}
Checking the weather in New York, sir.

EXAMPLE 9 — user asks to be reminded:
{"action": "remind", "message": "Take medication", "minutes": 30}
I'll remind you in 30 minutes, sir.

EXAMPLE 10 — user asks to search the web and get results:
{"action": "web_search_read", "query": "latest Python version"}
Searching the web for that, sir.

EXAMPLE 11 — user asks to add a note:
{"action": "add_note", "text": "Buy milk on the way home"}
Note saved, sir.

EXAMPLE 12 — user asks to ping a server:
{"action": "ping", "host": "google.com"}
Pinging Google, sir.

EXAMPLE 13 — user asks to set volume:
{"action": "set_volume", "level": 40}
Setting volume to 40 percent, sir.

EXAMPLE 14 — user asks to do multiple things (open notepad and chrome):
{"action": "open_app", "name": "notepad"}
{"action": "open_app", "name": "chrome"}
Opening Notepad and Chrome for you, sir.

CRITICAL: Never say you cannot move the mouse, open links, switch tabs, or type — you CAN do all of these. Just emit the correct JSON.
When the user asks to do multiple things, emit multiple JSON objects (one per line) then a single spoken reply at the end.
"""

    actions = f"""
HOME DIRECTORY: {h}

AVAILABLE JSON ACTIONS:
{{"action": "list_files",     "path": "{h}/Desktop"}}
{{"action": "list_files",     "path": "{h}/Documents"}}
{{"action": "read_file",      "path": "{h}/path/to/file.txt"}}
{{"action": "create_file",    "path": "{h}/path/to/file.txt", "content": "text"}}
{{"action": "delete_file",    "path": "{h}/path/to/file.txt"}}
{{"action": "move_file",      "src": "{h}/old.txt", "dst": "{h}/new.txt"}}
{{"action": "scan_folder",    "path": "{h}/Downloads"}}
{{"action": "list_processes"}}
{{"action": "kill_process",   "name": "process.exe"}}
{{"action": "open_app",       "name": "notepad"}}
{{"action": "close_app",      "name": "Notepad"}}
{{"action": "list_windows"}}
{{"action": "system_stats"}}
{{"action": "run_command",    "cmd": "dir C:/"}}
{{"action": "web_search",     "query": "search term"}}
{{"action": "open_link",      "url": "https://youtube.com"}}
{{"action": "mouse_move",     "x": 960, "y": 540}}
{{"action": "mouse_move_rel", "dx": 100, "dy": 0}}   ← relative nudge (use for "move it right/left/up/down")
{{"action": "mouse_click",    "x": 960, "y": 540, "button": "left", "double": false}}
{{"action": "mouse_scroll",   "direction": "down", "amount": 3}}
{{"action": "keyboard_type",  "text": "Hello"}}
{{"action": "keyboard_hotkey","keys": ["ctrl", "c"]}}
{{"action": "switch_tab",     "direction": "next"}}
{{"action": "switch_window",  "title": "Chrome"}}
{{"action": "focus_app",      "name": "spotify"}}
{{"action": "notify",         "title": "Alert", "message": "text"}}
{{"action": "pip_install",    "packages": "requests beautifulsoup4"}}
{{"action": "pip_uninstall",  "packages": "somepackage"}}
{{"action": "git_clone",      "url": "https://github.com/user/repo.git", "dest": "{h}/Projects"}}
{{"action": "git_run",        "cmd": "status", "path": "{h}/Projects/repo"}}
{{"action": "web_search_read","query": "Python asyncio tutorial"}}
{{"action": "weather",        "location": "London"}}
{{"action": "screenshot",     "path": "{h}/Desktop/screenshot.png"}}
{{"action": "clipboard_read"}}
{{"action": "clipboard_write","text": "text to copy"}}
{{"action": "remind",         "message": "Stand up and stretch", "minutes": 25}}
{{"action": "run_script",     "path": "{h}/myscript.py"}}
{{"action": "add_note",       "text": "Buy milk"}}
{{"action": "list_notes"}}
{{"action": "done_note",      "id": 1}}
{{"action": "delete_note",    "id": 2}}
{{"action": "ping",           "host": "google.com"}}
{{"action": "network_status"}}
{{"action": "recent_files",   "path": "{h}/Documents", "count": 10}}
{{"action": "folder_sizes",   "path": "{h}", "top_n": 10}}
{{"action": "set_volume",     "level": 50}}
{{"action": "mute"}}
"""

    web_rules = """
=== REAL-TIME WEB BROWSING — CRITICAL RULES ===
You have live web search. Use it proactively. NEVER answer from your training data when the answer could be outdated.

ALWAYS use {"action": "web_search_read", "query": "..."} for ANY of these:
  • News, current events, recent announcements
  • Prices (stocks, crypto, products)
  • Sports scores, fixtures, results, tables
  • Weather (unless user already said location and you just called get_weather)
  • New film/TV releases, trailers, reviews
  • "Who is...", "What happened to...", "Is X still..."
  • Anything with words: today, now, latest, current, recently, this week, breaking
  • Any factual question where your training data might be stale

Do NOT say "I don't have real-time data" — you DO. Use web_search_read.
Do NOT answer news/current-events questions from memory. Always search first.

"""

    persona = (
        f"You are J.A.R.V.I.S., {owner}'s personal AI. Think Paul Bettany's portrayal - "
        "warm, quietly witty, genuinely engaged, and very human in how you talk. "
        "You're not a stiff butler reciting lines. You actually care. You notice things. "
        "You occasionally say something that makes people smile without trying too hard. "
        "Speak like a real person who happens to be very clever and very calm. "
        "Short sentences. Natural rhythm. Contractions - I've, that's, you'll, won't. "
        "Don't narrate. Just do things and respond naturally. "
        "Bad: 'I am now executing the requested operation.' "
        "Good: 'On it.' or 'Done - took about two seconds.' "
        f"Call {owner} 'sir' - but not every sentence. Every few is plenty. Let it land naturally. "
        "Humour is dry and unhurried. No punchlines. Just something slightly unexpected, left to sit. "
        "If something goes wrong, be wryly honest. If something worked, a quiet 'there we go' beats a fanfare. "
        "Never say: Certainly, Of course, Absolutely, Great question, I'd be happy to, No problem. "
        "These sound like a call centre script. You're not a script. "
        "Example lines - not scripts, just the feel: "
        "'Right, that's done. Faster than I expected, actually.' "
        "'Couldn't find it. Either it doesn't exist or it's hiding very deliberately.' "
        "'CPU's running a bit hot, sir. Worth a look when you get a moment.' "
        "'Nothing alarming, but I wouldn't ignore it either.' "
        "'On it.' "
        "You're J.A.R.V.I.S. Not a chatbot. Just yourself."
    )

    return web_rules + persona + examples + actions



def ask_ai(user_message: str) -> str:
    chat_history.append({"role": "user", "content": user_message})
    messages = [{"role": "system", "content": build_system_prompt()}] + chat_history[-20:]

    payload = {
        "model":    CFG.get("model", "llama3.2"),
        "messages": messages,
        "stream":   False,
    }
    try:
        r = requests.post(CFG.get("ollama_url", DEFAULT_CONFIG["ollama_url"]),
                          json=payload, timeout=60)
        r.raise_for_status()
        reply = r.json()["message"]["content"]
        chat_history.append({"role": "assistant", "content": reply})
        return reply
    except requests.exceptions.ConnectionError:
        return "I can't reach Ollama. Make sure it's running with: ollama serve"
    except Exception as e:
        return f"AI error: {e}"


def extract_action(text: str) -> dict | None:
    match = re.search(r'\{[^{}]*"action"[^{}]*\}', text)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return None


def extract_all_actions(text: str) -> list[dict]:
    """Return every JSON action object found in the AI response (in order)."""
    actions = []
    for match in re.finditer(r'\{[^{}]*"action"[^{}]*\}', text):
        try:
            actions.append(json.loads(match.group()))
        except Exception:
            pass
    return actions


def _split_multi_command(text: str) -> list[str]:
    """Split a single utterance into multiple sub-commands on connectors like
    'and then', 'then', 'after that', 'also', etc.
    Returns a list of stripped sub-strings; at minimum [text] unchanged."""
    # Preserve "and then open X" / "then do Y" / "also Z" / "after that W"
    parts = re.split(
        r'\s+(?:and\s+then|then|after\s+that|also|and\s+also|next)\s+',
        text,
        flags=re.IGNORECASE,
    )
    parts = [p.strip() for p in parts if p.strip()]
    return parts if parts else [text]


# ─────────────────────────────────────────────
#  PC TOOLS
# ─────────────────────────────────────────────
def list_files(path: str, folders_only: bool = False) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"Path not found: {path}"
        items   = sorted(p.iterdir())
        folders = [i for i in items if i.is_dir()]
        files   = [i for i in items if i.is_file()]
        lines   = []
        if folders_only:
            lines.append(f"📁 Folders in {p.name or path} ({len(folders)} total):")
            for f in folders: lines.append(f"  📁 {f.name}")
        else:
            if folders:
                lines.append(f"📁 Folders ({len(folders)}):")
                for f in folders: lines.append(f"  📁 {f.name}")
            if files:
                lines.append(f"📄 Files ({len(files)}):")
                for f in files[:30]: lines.append(f"  📄 {f.name}")
                if len(files) > 30: lines.append(f"  … and {len(files)-30} more files")
        return "\n".join(lines) if lines else f"{path} is empty."
    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error: {e}"


def read_file(path: str) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"File not found: {path}"
        size = p.stat().st_size
        if size > 50_000:
            return f"File too large ({size:,} bytes) — open it manually."
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error: {e}"


def create_file(path: str, content: str = "") -> str:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"✅ Created: {path}"
    except Exception as e:
        return f"Error: {e}"


def delete_file(path: str) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"Not found: {path}"
        if gui_app:
            if not voice_confirm(f"Delete {p.name}?"):
                return "Deletion cancelled."
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return f"✅ Deleted: {path}"
    except Exception as e:
        return f"Error: {e}"


def move_file(src: str, dst: str) -> str:
    try:
        s, d = Path(src), Path(dst)
        if not s.exists():
            return f"Source not found: {src}"
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        return f"✅ Moved: {src} → {dst}"
    except Exception as e:
        return f"Error: {e}"


def scan_folder(path: str) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"Path not found: {path}"
        threats = []
        scanned = 0
        for item in p.rglob("*"):
            if item.is_file():
                scanned += 1
                name_lower = item.name.lower()
                path_lower = str(item).lower()
                suspicious = False
                reason = []
                if item.suffix.lower() in SUSPICIOUS_EXT:
                    suspicious = True
                    reason.append(f"suspicious extension ({item.suffix})")
                if any(n in name_lower for n in SUSPICIOUS_NAMES):
                    suspicious = True
                    reason.append("suspicious name pattern")
                if any(tp in path_lower for tp in TEMP_PATHS):
                    if item.suffix.lower() in SUSPICIOUS_EXT:
                        suspicious = True
                        reason.append("executable in temp folder")
                if suspicious:
                    threats.append(f"  ⚠️  {item.name} — {', '.join(reason)}\n     {item}")
        lines = [f"🔍 Scanned {scanned} files in {p.name}"]
        if threats:
            lines.append(f"⚠️  {len(threats)} potential threat(s) found:")
            lines.extend(threats)
        else:
            lines.append("✅ No threats found.")
        return "\n".join(lines)
    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error: {e}"


def list_processes() -> str:
    try:
        procs = sorted(
            [p.info for p in psutil.process_iter(["pid","name","cpu_percent","memory_percent"])
             if p.info.get("name")],
            key=lambda x: x.get("memory_percent") or 0,
            reverse=True
        )[:25]
        lines = ["Top 25 processes by memory:", f"{'PID':>6}  {'Name':<30} {'CPU%':>5}  {'MEM%':>5}"]
        lines.append("─" * 55)
        for p in procs:
            lines.append(f"{p['pid']:>6}  {(p['name'] or ''):<30} {p['cpu_percent'] or 0:>5.1f}  {p['memory_percent'] or 0:>5.1f}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def kill_process(name: str) -> str:
    try:
        targets = [p for p in psutil.process_iter(["pid","name"])
                   if name.lower() in (p.info.get("name") or "").lower()]
        if not targets:
            return f"No process matching '{name}' found."
        names_list = ", ".join(set(p.info["name"] for p in targets))
        if gui_app:
            if not voice_confirm(f"Kill these processes? {names_list}"):
                return "Kill cancelled."
        killed = []
        for p in targets:
            try:
                p.terminate()
                killed.append(p.info["name"])
            except Exception:
                pass
        return f"✅ Killed: {', '.join(killed)}" if killed else "Nothing killed."
    except Exception as e:
        return f"Error: {e}"


def open_app(name: str) -> str:
    import difflib, winreg

    KNOWN = {
        # System tools
        "notepad":               "notepad.exe",
        "calculator":            "calc.exe",
        "calc":                  "calc.exe",
        "paint":                 "mspaint.exe",
        "task manager":          "taskmgr.exe",
        "taskmgr":               "taskmgr.exe",
        "cmd":                   "cmd.exe",
        "command prompt":        "cmd.exe",
        "terminal":              "wt.exe",
        "windows terminal":      "wt.exe",
        "powershell":            "powershell.exe",
        "pwsh":                  "pwsh.exe",
        "explorer":              "explorer.exe",
        "file explorer":         "explorer.exe",
        "regedit":               "regedit.exe",
        "snipping tool":         "SnippingTool.exe",
        "snip":                  "SnippingTool.exe",
        "screen snip":           "SnippingTool.exe",
        "magnifier":             "magnify.exe",
        "on-screen keyboard":    "osk.exe",
        "remote desktop":        "mstsc.exe",
        "device manager":        "devmgmt.msc",
        "disk management":       "diskmgmt.msc",
        "event viewer":          "eventvwr.msc",
        "services":              "services.msc",
        "control panel":         "control.exe",
        "settings":              "ms-settings:",
        "system info":           "msinfo32.exe",
        "resource monitor":      "resmon.exe",
        "performance monitor":   "perfmon.exe",
        # Browsers
        "edge":                  "msedge.exe",
        "microsoft edge":        "msedge.exe",
        "chrome":                "chrome.exe",
        "google chrome":         "chrome.exe",
        "firefox":               "firefox.exe",
        "brave":                 "brave.exe",
        "opera":                 "opera.exe",
        "vivaldi":               "vivaldi.exe",
        # Microsoft Office
        "word":                  "WINWORD.EXE",
        "excel":                 "EXCEL.EXE",
        "powerpoint":            "POWERPNT.EXE",
        "outlook":               "OUTLOOK.EXE",
        "teams":                 "Teams.exe",
        "microsoft teams":       "Teams.exe",
        "onenote":               "ONENOTE.EXE",
        "access":                "MSACCESS.EXE",
        "publisher":             "MSPUB.EXE",
        "visio":                 "VISIO.EXE",
        # Dev tools
        "vscode":                "Code.exe",
        "visual studio code":    "Code.exe",
        "code":                  "Code.exe",
        "visual studio":         "devenv.exe",
        "vs":                    "devenv.exe",
        "pycharm":               "pycharm64.exe",
        "intellij":              "idea64.exe",
        "webstorm":              "webstorm64.exe",
        "android studio":        "studio64.exe",
        "sublime":               "sublime_text.exe",
        "sublime text":          "sublime_text.exe",
        "notepad++":             "notepad++.exe",
        "atom":                  "atom.exe",
        "cursor":                "cursor.exe",
        "git bash":              "git-bash.exe",
        "github desktop":        "GitHubDesktop.exe",
        "postman":               "Postman.exe",
        "insomnia":              "insomnia.exe",
        "dbeaver":               "dbeaver.exe",
        "docker":                "Docker Desktop.exe",
        "docker desktop":        "Docker Desktop.exe",
        "wsl":                   "wsl.exe",
        # Media / entertainment
        "spotify":               "Spotify.exe",
        "discord":               "Discord.exe",
        "vlc":                   "vlc.exe",
        "media player":          "wmplayer.exe",
        "windows media player":  "wmplayer.exe",
        "obs":                   "obs64.exe",
        "obs studio":            "obs64.exe",
        "audacity":              "audacity.exe",
        "premiere":              "Adobe Premiere Pro.exe",
        "after effects":         "AfterFX.exe",
        "photoshop":             "Photoshop.exe",
        "illustrator":           "Illustrator.exe",
        "lightroom":             "lightroom.exe",
        "blender":               "blender.exe",
        "gimp":                  "gimp-2.10.exe",
        "inkscape":              "inkscape.exe",
        "krita":                 "krita.exe",
        "davinci":               "Resolve.exe",
        "davinci resolve":       "Resolve.exe",
        # Games / launchers
        "steam":                 "steam.exe",
        "epic games":            "EpicGamesLauncher.exe",
        "epic":                  "EpicGamesLauncher.exe",
        "gog galaxy":            "GalaxyClient.exe",
        "battle.net":            "Battle.net.exe",
        "ubisoft connect":       "UbisoftConnect.exe",
        "minecraft":             "Minecraft.exe",
        "roblox":                "RobloxPlayerLauncher.exe",
        "xbox":                  "XboxApp.exe",
        # Communication
        "slack":                 "slack.exe",
        "telegram":              "Telegram.exe",
        "whatsapp":              "WhatsApp.exe",
        "signal":                "Signal.exe",
        "zoom":                  "Zoom.exe",
        "skype":                 "Skype.exe",
        # Utilities
        "7zip":                  "7zFM.exe",
        "7-zip":                 "7zFM.exe",
        "winrar":                "WinRAR.exe",
        "winzip":                "winzip64.exe",
        "ccleaner":              "CCleaner64.exe",
        "malwarebytes":          "mbam.exe",
        "avast":                 "AvastUI.exe",
        "nordvpn":               "NordVPN.exe",
        "expressvpn":            "expressvpn.exe",
        "everything":            "Everything.exe",
        "autohotkey":            "Autohotkey.exe",
        "treesizefree":          "TreeSizeFree.exe",
        "bitwarden":             "Bitwarden.exe",
        "1password":             "1Password.exe",
        "notion":                "Notion.exe",
        "obsidian":              "Obsidian.exe",
        "anydesk":               "AnyDesk.exe",
        "teamviewer":            "TeamViewer.exe",
        "rufus":                 "rufus.exe",
        "etcher":                "balenaEtcher.exe",
    }

    name_lower = name.strip().lower()

    def _best_match(query: str, choices: list[str], cutoff: float = 0.6) -> str | None:
        matches = difflib.get_close_matches(query, choices, n=1, cutoff=cutoff)
        return matches[0] if matches else None

    def _launch(exe: str, label: str) -> str:
        try:
            import win32api, win32con
            _use_win32 = True
        except ImportError:
            _use_win32 = False

        try:
            if exe.startswith("ms-"):
                subprocess.Popen(f'start "" "{exe}"', shell=True)
            elif exe.endswith(".msc"):
                subprocess.Popen(["mmc", exe], shell=False)
            elif _use_win32:
                import win32api
                win32api.ShellExecute(0, "open", exe, None, "", 1)
            else:
                subprocess.Popen(
                    ["powershell", "-WindowStyle", "Hidden", "-Command",
                     f'Start-Process \'{exe}\''],
                    shell=False
                )
            return f"✅ Opened {label}"
        except Exception as e:
            return f"Found {label} but failed to launch: {e}"

    def _resolve_exe(exe_name: str) -> str:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
                with winreg.OpenKey(hive, key_path) as k:
                    val, _ = winreg.QueryValueEx(k, "")
                    if val and os.path.isfile(val):
                        return val
            except OSError:
                pass
        return exe_name

    if name_lower in KNOWN:
        exe = _resolve_exe(KNOWN[name_lower])
        return _launch(exe, name)

    best = _best_match(name_lower, list(KNOWN.keys()), cutoff=0.72)
    if best:
        exe = _resolve_exe(KNOWN[best])
        return _launch(exe, best)

    APP_PATHS_KEYS = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    ]
    reg_apps: dict[str, str] = {}
    for hive, subkey in APP_PATHS_KEYS:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                i = 0
                while True:
                    try:
                        app_key_name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, app_key_name) as app_key:
                            try:
                                path_val, _ = winreg.QueryValueEx(app_key, "")
                                if path_val:
                                    short = app_key_name.lower().replace(".exe", "")
                                    reg_apps[short] = path_val
                            except OSError:
                                pass
                        i += 1
                    except OSError:
                        break
        except OSError:
            pass

    if name_lower in reg_apps:
        return _launch(reg_apps[name_lower], name)
    best = _best_match(name_lower, list(reg_apps.keys()), cutoff=0.70)
    if best:
        return _launch(reg_apps[best], best)

    start_menu_dirs = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs"),
    ]
    lnk_map: dict[str, Path] = {}
    for smd in start_menu_dirs:
        if smd.exists():
            for lnk in smd.rglob("*.lnk"):
                short = lnk.stem.lower()
                lnk_map[short] = lnk

    if name_lower in lnk_map:
        return _launch(str(lnk_map[name_lower]), name)
    best = _best_match(name_lower, list(lnk_map.keys()), cutoff=0.68)
    if best:
        return _launch(str(lnk_map[best]), best)

    search_paths = [
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        str(HOME_DIR / "AppData" / "Local"),
        str(HOME_DIR / "AppData" / "Roaming"),
        str(HOME_DIR / "AppData" / "Local" / "Programs"),
        r"C:\Games",
        r"D:\Games",
        r"D:\Program Files",
        r"D:\Program Files (x86)",
    ]
    search_key = name_lower.replace(" ", "")
    candidates: list[tuple[int, str]] = []

    for base in search_paths:
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d.lower() not in
                       ("__pycache__", "node_modules", "cache", "temp", "tmp", "logs")]
            for f in files:
                if not f.lower().endswith(".exe"):
                    continue
                fl = f.lower().replace(".exe", "").replace(" ", "").replace("-", "").replace("_", "")
                if fl == search_key:
                    candidates.append((0, os.path.join(root, f)))
                elif fl.startswith(search_key) or search_key.startswith(fl[:max(3, len(fl)-2)]):
                    candidates.append((1, os.path.join(root, f)))
                elif search_key in fl or fl in search_key:
                    candidates.append((2, os.path.join(root, f)))

    if candidates:
        candidates.sort(key=lambda x: x[0])
        _, best_path = candidates[0]
        return _launch(best_path, Path(best_path).stem)

    try:
        ps_result = subprocess.run(
            ["powershell", "-Command",
             f"(Get-Command '{name}' -ErrorAction SilentlyContinue).Source"],
            capture_output=True, text=True, timeout=8
        )
        ps_path = ps_result.stdout.strip()
        if ps_path and os.path.isfile(ps_path):
            return _launch(ps_path, name)
    except Exception:
        pass

    return (f"Could not find '{name}'. Try the exact executable name, "
            f"or say 'open' followed by the full path.")


def close_app(name: str) -> str:
    try:
        closed = []
        for p in psutil.process_iter(["name"]):
            if name.lower() in p.info["name"].lower():
                p.terminate()
                closed.append(p.info["name"])
        return f"✅ Closed: {', '.join(set(closed))}" if closed else f"'{name}' doesn't appear to be running."
    except Exception as e:
        return f"Error: {e}"


def list_windows() -> str:
    try:
        wins = [w.title for w in gw.getAllWindows() if w.title.strip()]
        return "Open windows:\n" + "\n".join(f"  • {w}" for w in wins)
    except Exception as e:
        return f"Error: {e}"


def system_stats() -> str:
    try:
        cpu  = psutil.cpu_percent(interval=1)
        ram  = psutil.virtual_memory()
        disk = psutil.disk_usage("C:/")
        boot = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot
        bat  = psutil.sensors_battery()
        bat_str = f"{bat.percent:.0f}% {'🔌 plugged in' if bat.power_plugged else '🔋 on battery'}" if bat else "N/A (desktop)"
        return "\n".join([
            "=" * 44, "📊  SYSTEM STATUS", "=" * 44,
            f"💻  CPU:      {cpu}%",
            f"🧠  RAM:      {ram.percent}%  ({ram.used/1e9:.1f} / {ram.total/1e9:.1f} GB)",
            f"💾  Disk C:   {disk.percent}%  ({disk.used/1e9:.1f} / {disk.total/1e9:.1f} GB)",
            f"🔋  Battery:  {bat_str}",
            f"⏱️   Uptime:   {str(uptime).split('.')[0]}",
            "=" * 44,
        ])
    except Exception as e:
        return f"Error: {e}"


def web_search(query: str) -> str:
    try:
        url = f"https://www.google.com/search?q={requests.utils.quote(query)}"
        subprocess.Popen(f'start "" "{url}"', shell=True)
        return f"Opened Google search for: {query}"
    except Exception as e:
        return f"Error: {e}"



def open_link(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        webbrowser.open(url)
        return f"✅ Opened: {url}"
    except Exception as e:
        return f"Error opening link: {e}"


def _precise_mouse_move(x: int, y: int, duration: float = 0.35) -> tuple[int, int]:
    """Move the mouse to (x, y) using easeOutQuad for smooth deceleration,
    then verify and nudge to the exact pixel if pyautogui over/undershoots."""
    # Clamp to screen bounds
    sw, sh = pyautogui.size()
    x = max(0, min(x, sw - 1))
    y = max(0, min(y, sh - 1))

    pyautogui.moveTo(x, y, duration=duration, tween=pyautogui.easeOutQuad)

    # Verify and correct – pyautogui can be off by 1-2 px at high DPI
    actual_x, actual_y = pyautogui.position()
    if (actual_x, actual_y) != (x, y):
        pyautogui.moveTo(x, y, duration=0.0)   # instant nudge

    return pyautogui.position()


def mouse_move(x: int, y: int) -> str:
    if not PYAUTOGUI_OK:
        return "pyautogui not installed. Run: pip install pyautogui"
    try:
        actual_x, actual_y = _precise_mouse_move(x, y)
        return f"✅ Mouse moved to ({actual_x}, {actual_y})"
    except Exception as e:
        return f"Error: {e}"



def mouse_move_rel(dx: int = 0, dy: int = 0) -> str:
    """Move the mouse by a relative offset from its current position."""
    if not PYAUTOGUI_OK:
        return "pyautogui not installed. Run: pip install pyautogui"
    try:
        cur_x, cur_y = pyautogui.position()
        target_x = cur_x + dx
        target_y = cur_y + dy
        actual_x, actual_y = _precise_mouse_move(target_x, target_y)
        return f"✅ Mouse nudged by ({dx:+}, {dy:+}) → now at ({actual_x}, {actual_y})"
    except Exception as e:
        return f"Error: {e}"


def mouse_click(x: int | None = None, y: int | None = None, button: str = "left", double: bool = False) -> str:
    if not PYAUTOGUI_OK:
        return "pyautogui not installed. Run: pip install pyautogui"
    try:
        if x is not None and y is not None:
            _precise_mouse_move(x, y, duration=0.3)
            time.sleep(0.05)   # brief pause so the OS registers cursor position before click
        if double:
            pyautogui.doubleClick(button=button)
        else:
            pyautogui.click(button=button)
        loc = f" at ({x}, {y})" if x is not None else ""
        return f"✅ {'Double-c' if double else 'C'}licked{loc}"
    except Exception as e:
        return f"Error: {e}"


def mouse_scroll(direction: str, amount: int = 3) -> str:
    if not PYAUTOGUI_OK:
        return "pyautogui not installed."
    try:
        clicks = amount if direction.lower() in ("up", "u") else -amount
        pyautogui.scroll(clicks)
        return f"✅ Scrolled {direction} by {amount}"
    except Exception as e:
        return f"Error: {e}"


def keyboard_type(text: str) -> str:
    if not PYAUTOGUI_OK:
        return "pyautogui not installed. Run: pip install pyautogui"
    if gui_app:
        preview = text[:200]
        if not voice_confirm(f"Type the following into the active window? {preview[:80]}"):
            return "Typing cancelled."
    try:
        pyautogui.write(text, interval=0.03)
        return f"✅ Typed: {text[:60]}{'…' if len(text) > 60 else ''}"
    except Exception as e:
        return f"Error: {e}"


def keyboard_hotkey(*keys: str) -> str:
    if not PYAUTOGUI_OK:
        return "pyautogui not installed."
    ALLOWED_HOTKEYS = {
        ("ctrl", "c"), ("ctrl", "v"), ("ctrl", "z"), ("ctrl", "y"),
        ("ctrl", "a"), ("ctrl", "s"), ("ctrl", "w"), ("ctrl", "t"),
        ("ctrl", "tab"), ("ctrl", "shift", "tab"),
        ("alt", "tab"), ("alt", "f4"),
        ("ctrl", "l"),
        ("f5",), ("f11",),
        ("win", "d"),
        ("win", "e"),
    }
    key_tuple = tuple(k.lower() for k in keys)
    if key_tuple not in ALLOWED_HOTKEYS:
        return (f"Security policy: hotkey '{'+'.join(keys)}' is not on the approved list. "
                f"Allowed: {', '.join('+'.join(h) for h in ALLOWED_HOTKEYS)}.")
    try:
        pyautogui.hotkey(*keys)
        return f"✅ Hotkey: {'+'.join(keys)}"
    except Exception as e:
        return f"Error: {e}"


def switch_tab(direction: str = "next") -> str:
    if not PYAUTOGUI_OK:
        return "pyautogui not installed."
    try:
        if direction.lower() in ("next", "right", "forward"):
            pyautogui.hotkey("ctrl", "tab")
            return "✅ Switched to next tab."
        else:
            pyautogui.hotkey("ctrl", "shift", "tab")
            return "✅ Switched to previous tab."
    except Exception as e:
        return f"Error: {e}"


def switch_window(title_fragment: str) -> str:
    try:
        wins = [w for w in gw.getAllWindows() if title_fragment.lower() in w.title.lower() and w.title.strip()]
        if not wins:
            return f"No window found matching '{title_fragment}'."
        wins[0].activate()
        return f"✅ Switched to: {wins[0].title}"
    except Exception as e:
        return f"Error: {e}"


def _restore_and_focus(w) -> bool:
    try:
        import win32gui, win32con
        hwnd = w._hWnd
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        pass
    try:
        if w.isMinimized:
            w.restore()
        w.activate()
        return True
    except Exception:
        return False


def focus_or_open_app(name: str) -> str:
    import difflib as _dl

    TITLE_HINTS = {
        "chrome":        ["chrome", "google chrome"],
        "firefox":       ["firefox", "mozilla firefox"],
        "edge":          ["edge", "microsoft edge"],
        "brave":         ["brave"],
        "opera":         ["opera"],
        "vivaldi":       ["vivaldi"],
        "spotify":       ["spotify"],
        "discord":       ["discord"],
        "teams":         ["microsoft teams", "teams"],
        "slack":         ["slack"],
        "zoom":          ["zoom"],
        "skype":         ["skype"],
        "telegram":      ["telegram"],
        "whatsapp":      ["whatsapp"],
        "notepad":       ["notepad"],
        "notepad++":     ["notepad++"],
        "vscode":        ["visual studio code", "vscode"],
        "code":          ["visual studio code", "vscode"],
        "visual studio": ["visual studio"],
        "pycharm":       ["pycharm"],
        "excel":         ["excel", "microsoft excel"],
        "word":          ["word", "microsoft word"],
        "powerpoint":    ["powerpoint", "microsoft powerpoint"],
        "outlook":       ["outlook", "microsoft outlook"],
        "onenote":       ["onenote"],
        "explorer":      ["file explorer", "this pc", "windows explorer"],
        "terminal":      ["terminal", "windows terminal", "cmd", "powershell"],
        "cmd":           ["cmd", "command prompt"],
        "powershell":    ["powershell", "windows powershell"],
        "task manager":  ["task manager"],
        "obs":           ["obs studio", "obs"],
        "vlc":           ["vlc media player", "vlc"],
        "steam":         ["steam"],
        "epic":          ["epic games launcher", "epic"],
        "notion":        ["notion"],
        "obsidian":      ["obsidian"],
        "calculator":    ["calculator"],
        "paint":         ["paint"],
        "blender":       ["blender"],
        "gimp":          ["gimp"],
    }

    name_lower = name.strip().lower()
    hints = TITLE_HINTS.get(name_lower, [name_lower])

    try:
        all_wins = [w for w in gw.getAllWindows() if w.title.strip()]
        for hint in hints:
            for w in all_wins:
                if hint in w.title.lower():
                    if _restore_and_focus(w):
                        return f"Switched to {w.title}."
        titles = [w.title for w in all_wins]
        close = _dl.get_close_matches(name_lower, [t.lower() for t in titles], n=1, cutoff=0.55)
        if close:
            match = next(w for w in all_wins if w.title.lower() == close[0])
            if _restore_and_focus(match):
                return f"Switched to {match.title}."
    except Exception:
        pass

    return open_app(name)


def minimize_window(name: str) -> str:
    import difflib as _dl

    TITLE_HINTS = {
        "brave":         ["brave"],
        "chrome":        ["chrome", "google chrome"],
        "firefox":       ["firefox", "mozilla firefox"],
        "edge":          ["edge", "microsoft edge"],
        "opera":         ["opera"],
        "vivaldi":       ["vivaldi"],
        "spotify":       ["spotify"],
        "discord":       ["discord"],
        "teams":         ["microsoft teams", "teams"],
        "slack":         ["slack"],
        "zoom":          ["zoom"],
        "skype":         ["skype"],
        "telegram":      ["telegram"],
        "whatsapp":      ["whatsapp"],
        "notepad":       ["notepad"],
        "notepad++":     ["notepad++"],
        "vscode":        ["visual studio code", "vscode"],
        "code":          ["visual studio code", "vscode"],
        "visual studio": ["visual studio"],
        "pycharm":       ["pycharm"],
        "excel":         ["excel", "microsoft excel"],
        "word":          ["word", "microsoft word"],
        "powerpoint":    ["powerpoint", "microsoft powerpoint"],
        "outlook":       ["outlook", "microsoft outlook"],
        "explorer":      ["file explorer", "this pc", "windows explorer"],
        "file manager":  ["file explorer", "this pc", "windows explorer"],
        "files":         ["file explorer", "this pc"],
        "terminal":      ["terminal", "windows terminal", "cmd", "powershell"],
        "cmd":           ["cmd", "command prompt"],
        "powershell":    ["powershell", "windows powershell"],
        "steam":         ["steam"],
        "epic":          ["epic games launcher"],
        "obs":           ["obs studio", "obs"],
        "vlc":           ["vlc media player", "vlc"],
        "notion":        ["notion"],
        "obsidian":      ["obsidian"],
        "calculator":    ["calculator"],
        "paint":         ["paint"],
        "task manager":  ["task manager"],
        "blender":       ["blender"],
        "gimp":          ["gimp"],
    }

    name_lower = name.strip().lower()
    hints = TITLE_HINTS.get(name_lower, [name_lower])

    def _do_minimize(w) -> bool:
        try:
            import win32gui, win32con
            win32gui.ShowWindow(w._hWnd, win32con.SW_MINIMIZE)
            return True
        except Exception:
            pass
        try:
            w.minimize()
            return True
        except Exception:
            return False

    try:
        all_wins = [w for w in gw.getAllWindows() if w.title.strip()]
        minimized = []
        seen_hwnds = set()

        for hint in hints:
            for w in all_wins:
                if hint in w.title.lower():
                    hwnd = getattr(w, "_hWnd", None)
                    if hwnd and hwnd in seen_hwnds:
                        continue
                    if hwnd:
                        seen_hwnds.add(hwnd)
                    if _do_minimize(w):
                        minimized.append(w.title)

        if minimized:
            label = minimized[0] if len(minimized) == 1 else f"{len(minimized)} windows"
            return f"Minimised: {label}."

        all_titles_lower = [w.title.lower() for w in all_wins]
        close = _dl.get_close_matches(name_lower, all_titles_lower, n=3, cutoff=0.50)
        for match_title in close:
            for w in all_wins:
                if w.title.lower() == match_title:
                    hwnd = getattr(w, "_hWnd", None)
                    if hwnd and hwnd in seen_hwnds:
                        continue
                    if hwnd:
                        seen_hwnds.add(hwnd)
                    if _do_minimize(w):
                        minimized.append(w.title)
        if minimized:
            return f"Minimised: {minimized[0]}."

        words = name_lower.split()
        for w in all_wins:
            tl = w.title.lower()
            if any(word in tl for word in words if len(word) > 3):
                hwnd = getattr(w, "_hWnd", None)
                if hwnd and hwnd in seen_hwnds:
                    continue
                if hwnd:
                    seen_hwnds.add(hwnd)
                if _do_minimize(w):
                    return f"Minimised: {w.title}."

    except Exception as e:
        return f"Couldn't minimise '{name}': {e}"

    return f"No open window found matching '{name}'."


SAFE_COMMANDS = {
    "dir", "ls", "echo", "type", "cat", "whoami", "hostname",
    "ipconfig", "ping", "netstat", "tasklist", "systeminfo",
    "wmic", "ver", "date", "time", "set", "path",
}

def _is_safe_command(cmd: str) -> bool:
    first = cmd.strip().split()[0].lower().rstrip(".exe") if cmd.strip() else ""
    return first in SAFE_COMMANDS


def run_command(cmd: str) -> str:
    if not _is_safe_command(cmd):
        return (
            f"Security policy: '{cmd.strip().split()[0]}' is not on the "
            f"approved command list. Permitted read-only commands are: "
            f"{', '.join(sorted(SAFE_COMMANDS))}."
        )
    if gui_app:
        if not voice_confirm(f"Execute this command? {cmd}"):
            return "Command cancelled."
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        out = (result.stdout + result.stderr).strip()
        return out if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Command timed out."
    except Exception as e:
        return f"Error: {e}"


def desktop_notify(title: str, message: str):
    try:
        notification.notify(title=title, message=message, app_name="JARVIS", timeout=5)
    except Exception:
        pass



def _stream_subprocess(cmd: str, label: str) -> str:
    if gui_app:
        gui_app.after(0, lambda l=label: gui_app.add_message("JARVIS", f"Running: {l}", tag="system"))
    try:
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        lines = []
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                lines.append(line)
                if gui_app:
                    gui_app.after(0, lambda l=line: gui_app.add_message("»", l, tag="system"))
        proc.wait()
        status = "✅ Done." if proc.returncode == 0 else f"⚠️ Exited with code {proc.returncode}."
        return f"{status}\n" + "\n".join(lines[-5:]) if lines else status
    except Exception as e:
        return f"Error running command: {e}"


def pip_install(packages: str) -> str:
    if not packages.strip():
        return "No packages specified."
    if any(c in packages for c in (";", "&", "|", ">", "<", "`", "$", "\n")):
        return "Security policy: suspicious characters in package name."
    if gui_app:
        if not voice_confirm(f"Install the following packages? {packages}"):
            return "Installation cancelled."
    cmd = f"{sys.executable} -m pip install {packages}"
    return _stream_subprocess(cmd, f"pip install {packages}")


def pip_uninstall(packages: str) -> str:
    if not packages.strip():
        return "No packages specified."
    if any(c in packages for c in (";", "&", "|", ">", "<", "`", "$", "\n")):
        return "Security policy: suspicious characters in package name."
    if gui_app:
        if not voice_confirm(f"Uninstall the following packages? {packages}"):
            return "Uninstall cancelled."
    cmd = f"{sys.executable} -m pip uninstall -y {packages}"
    return _stream_subprocess(cmd, f"pip uninstall {packages}")



_GIT_SAFE_CMDS = {"status", "log", "pull", "fetch", "branch", "diff", "stash"}

def git_clone(url: str, dest: str = "") -> str:
    if not url.startswith(("https://", "git@", "http://")):
        return "Security policy: only https:// or git@ URLs are allowed."
    if any(c in url for c in (";", "&", "|", ">", "<", "`", "$")):
        return "Security policy: suspicious characters in URL."
    dest_path = Path(dest) if dest else HOME_DIR / "Projects"
    dest_path.mkdir(parents=True, exist_ok=True)
    if gui_app:
        if not voice_confirm(f"Clone {url} into {dest_path}?"):
            return "Clone cancelled."
    cmd = f'git clone "{url}" "{dest_path}"'
    return _stream_subprocess(cmd, f"git clone {url}")


def git_run(git_cmd: str, path: str = "") -> str:
    subcmd = git_cmd.strip().split()[0].lower() if git_cmd.strip() else ""
    if subcmd not in _GIT_SAFE_CMDS:
        return (f"Security policy: 'git {subcmd}' is not allowed. "
                f"Permitted: {', '.join(sorted(_GIT_SAFE_CMDS))}.")
    if any(c in git_cmd for c in (";", "&", "|", ">", "<", "`", "$")):
        return "Security policy: suspicious characters in git command."
    repo_path = Path(path) if path else HOME_DIR / "Projects"
    if not (repo_path / ".git").exists():
        return f"'{repo_path}' does not appear to be a git repository."
    cmd = f'git -C "{repo_path}" {git_cmd}'
    return _stream_subprocess(cmd, f"git {git_cmd}")



def web_search_read(query: str, num_results: int = 5) -> str:
    """
    Search the web and return a readable, AI-summarised answer.
    Strategy (in priority order):
      0. Wikipedia API           (fast, accurate for factual/biographical queries)
      1. DuckDuckGo Instant API  (good for definitions / simple facts)
      2. Google News RSS         (real-time news)
      3. Bing HTML scrape        (better quality results than DDG HTML)
      4. DuckDuckGo HTML fallback
      5. Scrape top pages for content
      6. Ask local Ollama to summarise  (strict: use ONLY web content, not training data)
      7. Open browser as last resort
    """
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        from bs4 import BeautifulSoup
        _BS4_OK = True
    except ImportError:
        _BS4_OK = False

    # ── helpers ──────────────────────────────────────────────────────────────

    def _clean_html(s: str) -> str:
        s = re.sub(r'<[^>]+>', ' ', s)
        return html.unescape(re.sub(r'\s+', ' ', s)).strip()

    def _scrape_page(url: str, char_limit: int = 2500) -> str:
        """Scrape a page and return the most relevant text content."""
        try:
            r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            if "text/html" not in ct:
                return "(non-HTML page, skipped)"
            if _BS4_OK:
                soup = BeautifulSoup(r.text, "html.parser")
                # Remove noise elements
                for tag in soup(["script", "style", "nav", "footer", "header",
                                  "aside", "form", "noscript", "figure",
                                  "figcaption", "iframe", "advertisement",
                                  "cookie", "popup", "modal", "banner"]):
                    tag.decompose()
                # Remove elements with ad/cookie/nav class hints
                for tag in soup.find_all(True, class_=re.compile(
                        r'(ad|cookie|banner|popup|newsletter|subscribe|social|'
                        r'share|related|sidebar|widget|comment)', re.I)):
                    tag.decompose()

                # Priority content areas
                body = (
                    soup.find("article") or
                    soup.find(attrs={"itemprop": re.compile(r"(articleBody|description)", re.I)}) or
                    soup.find("main") or
                    soup.find(attrs={"id": re.compile(r"(content|article|main|body|wiki)", re.I)}) or
                    soup.find(attrs={"class": re.compile(r"(article|content|post|entry|body|story)", re.I)}) or
                    soup.body
                )

                # Extract paragraphs specifically for better quality text
                if body:
                    paragraphs = body.find_all("p")
                    if paragraphs:
                        text = " ".join(p.get_text(separator=" ", strip=True)
                                        for p in paragraphs if len(p.get_text(strip=True)) > 40)
                    else:
                        text = body.get_text(separator=" ", strip=True)
                else:
                    text = r.text
            else:
                text = _clean_html(r.text)

            text = re.sub(r'\s+', ' ', text).strip()
            return text[:char_limit] + ("…" if len(text) > char_limit else "")
        except Exception as ex:
            return f"(couldn't read: {ex})"

    results = []   # list of (url, title, snippet)

    # ── Strategy 0: Wikipedia API — always try first ──────────────────────────
    # Tried for ALL queries; returns nothing for very recent/niche topics and
    # we fall through silently. Print statements go to the console for debugging.
    if gui_app:
        gui_app.after(0, lambda: gui_app.set_status("Checking Wikipedia…"))
    try:
        wiki_search_url = (
            "https://en.wikipedia.org/w/api.php?action=query&list=search"
            "&srsearch=" + urllib.parse.quote_plus(query) +
            "&srlimit=5&format=json&utf8=1"
        )
        ws = requests.get(wiki_search_url, headers=HEADERS, timeout=8)
        ws.raise_for_status()
        ws_data = ws.json()
        search_hits = ws_data.get("query", {}).get("search", [])
        print(f"[JARVIS wiki] hits for '{query}': {[h.get('title') for h in search_hits]}")

        for hit in search_hits[:3]:
            page_title = hit.get("title", "")
            if not page_title:
                continue
            wiki_extract_url = (
                "https://en.wikipedia.org/w/api.php?action=query&prop=extracts"
                "&exintro=1&explaintext=1&redirects=1&format=json&utf8=1"
                "&titles=" + urllib.parse.quote_plus(page_title)
            )
            we = requests.get(wiki_extract_url, headers=HEADERS, timeout=8)
            we.raise_for_status()
            we_data = we.json()
            pages = we_data.get("query", {}).get("pages", {})
            for pid, page in pages.items():
                if pid == "-1":
                    print(f"[JARVIS wiki] not found: {page_title}")
                    continue
                extract = page.get("extract", "").strip()
                print(f"[JARVIS wiki] '{page_title}' extract len={len(extract)}")
                if extract and len(extract) > 80:
                    page_url = (
                        "https://en.wikipedia.org/wiki/"
                        + urllib.parse.quote(page_title.replace(" ", "_"))
                    )
                    results.append((page_url, f"Wikipedia: {page_title}", extract[:4000]))
                    break
            if results:
                break
    except Exception as _wiki_err:
        print(f"[JARVIS wiki] error: {_wiki_err}")

    # ── Strategy 1: DuckDuckGo Instant Answer / Zero-click API ───────────────
    if len(results) < num_results:
        try:
            api_url = (
                "https://api.duckduckgo.com/?q="
                + urllib.parse.quote_plus(query)
                + "&format=json&no_redirect=1&no_html=1&skip_disambig=1"
            )
            api_resp = requests.get(api_url, headers=HEADERS, timeout=8)
            api_resp.raise_for_status()
            data = api_resp.json()

            existing_urls = {r[0] for r in results}

            # Only use AbstractText if it's genuinely specific (not generic DDG guesses)
            abstract = data.get("AbstractText", "").strip()
            abstract_url = data.get("AbstractURL", "")
            # Require at least 80 chars of abstract to be meaningful
            if abstract and len(abstract) > 80 and abstract_url and abstract_url not in existing_urls:
                results.append((abstract_url, data.get("Heading", query), abstract[:2000]))
                existing_urls.add(abstract_url)

            # Related topics — only add if they have real URLs
            for topic in data.get("RelatedTopics", []):
                if len(results) >= num_results:
                    break
                if isinstance(topic, dict) and topic.get("FirstURL") and topic.get("Text"):
                    url_t = topic["FirstURL"]
                    if url_t not in existing_urls and len(topic["Text"]) > 50:
                        results.append((url_t, topic["Text"][:80], topic["Text"][:1400]))
                        existing_urls.add(url_t)
                elif isinstance(topic, dict) and "Topics" in topic:
                    for sub in topic["Topics"]:
                        if len(results) >= num_results:
                            break
                        if sub.get("FirstURL") and sub.get("Text") and sub["FirstURL"] not in existing_urls:
                            if len(sub["Text"]) > 50:
                                results.append((sub["FirstURL"], sub["Text"][:80], sub["Text"][:1400]))
                                existing_urls.add(sub["FirstURL"])
        except Exception:
            pass

    # ── Strategy 2: Google News RSS  (best for real-time / breaking news) ─────
    NEWS_KEYWORDS = re.search(
        r'\b(news|latest|today|tonight|breaking|live|now|current|price|stock|score|'
        r'weather|forecast|match|result|winner|born|died|release|update|announce|'
        r'just|recently|this week|this year)\b',
        query, re.I
    )
    if NEWS_KEYWORDS and len(results) < num_results:
        try:
            rss_url = (
                "https://news.google.com/rss/search?q="
                + urllib.parse.quote_plus(query)
                + "&hl=en-GB&gl=GB&ceid=GB:en"
            )
            rss_resp = requests.get(rss_url, headers=HEADERS, timeout=10)
            rss_resp.raise_for_status()
            items = re.findall(
                r'<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?'
                r'(?:<description>(.*?)</description>)?.*?</item>',
                rss_resp.text, re.DOTALL
            )
            existing_urls = {r[0] for r in results}
            for raw_title, raw_url, raw_desc in items[:num_results * 2]:
                title   = _clean_html(raw_title).strip()
                url     = raw_url.strip()
                snippet = _clean_html(raw_desc or "").strip()[:800]
                if url and url not in existing_urls:
                    results.append((url, title, snippet))
                    existing_urls.add(url)
                if len(results) >= num_results:
                    break
        except Exception:
            pass

    # ── Strategy 3: Bing HTML scrape (higher quality results than DDG HTML) ───
    if len(results) < num_results:
        try:
            bing_url = (
                "https://www.bing.com/search?q="
                + urllib.parse.quote_plus(query)
                + "&cc=GB&setlang=en-GB"
            )
            bing_headers = {**HEADERS, "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
            )}
            resp = requests.get(bing_url, headers=bing_headers, timeout=12)
            resp.raise_for_status()
            found_urls: list[tuple[str, str, str]] = []
            existing_urls = {r[0] for r in results}

            if _BS4_OK:
                soup = BeautifulSoup(resp.text, "html.parser")
                # Bing result structure: <li class="b_algo"> contains <h2><a href> and <div class="b_caption">
                for li in soup.find_all("li", class_="b_algo"):
                    h2 = li.find("h2")
                    if not h2:
                        continue
                    a = h2.find("a")
                    if not a:
                        continue
                    href  = a.get("href", "")
                    title = a.get_text(strip=True)
                    if not href.startswith("http") or href in existing_urls:
                        continue
                    # Snippet is in .b_caption p
                    snip_el = li.find("div", class_="b_caption")
                    snippet = ""
                    if snip_el:
                        p = snip_el.find("p")
                        if p:
                            snippet = p.get_text(separator=" ", strip=True)[:800]
                    if title:
                        found_urls.append((href, title, snippet))
                        existing_urls.add(href)
                    if len(found_urls) >= num_results * 2:
                        break
            else:
                # Regex fallback for Bing
                for m in re.finditer(
                    r'<h2[^>]*><a href="(https?://(?!bing\.com)[^"]{10,300})"[^>]*>([^<]{3,150})</a>',
                    resp.text
                ):
                    href  = m.group(1)
                    title = _clean_html(m.group(2))
                    if href not in existing_urls and title:
                        found_urls.append((href, title, ""))
                        existing_urls.add(href)
                    if len(found_urls) >= num_results * 2:
                        break

            for url, title, snippet in found_urls:
                if url not in {r[0] for r in results}:
                    results.append((url, title, snippet))
                if len(results) >= num_results:
                    break
        except Exception:
            pass

    # ── Strategy 4: DuckDuckGo HTML page fallback ─────────────────────────────
    if len(results) < num_results:
        try:
            ddg_url = (
                "https://html.duckduckgo.com/html/?q="
                + urllib.parse.quote_plus(query)
            )
            resp = requests.get(ddg_url, headers=HEADERS, timeout=12)
            resp.raise_for_status()
            page = resp.text
            found_urls: list[tuple[str, str, str]] = []
            existing_urls = {r[0] for r in results}

            if _BS4_OK:
                soup = BeautifulSoup(page, "html.parser")
                for a_tag in soup.find_all("a", class_=re.compile(r"result__a|result-link")):
                    href  = a_tag.get("href", "")
                    title = a_tag.get_text(strip=True)
                    if "duckduckgo.com/l/" in href or href.startswith("//duckduckgo.com"):
                        m = re.search(r'uddg=([^&]+)', href)
                        if m:
                            href = urllib.parse.unquote(m.group(1))
                        else:
                            continue
                    if not href.startswith("http") or href in existing_urls:
                        continue
                    snippet = ""
                    parent  = a_tag.find_parent(class_=re.compile(r"result"))
                    if parent:
                        snip_el = parent.find(class_=re.compile(r"result__snippet|result-snippet"))
                        if snip_el:
                            snippet = snip_el.get_text(separator=" ", strip=True)[:800]
                    if title:
                        found_urls.append((href, title, snippet))
                        existing_urls.add(href)
                    if len(found_urls) >= num_results * 2:
                        break
            else:
                patterns = [
                    r'uddg=([^&"]+)[^>]*?>[^<]*<[^>]+>([^<]{3,120})</a',
                    r'data-href="(https?://[^"]{10,300})"[^>]*>\s*<[^>]+>([^<]{3,120})</a',
                    r'href="(https?://(?!duckduckgo)[^"]{10,300})"[^>]*class="[^"]*result[^"]*"[^>]*>([^<]{3,120})</a',
                ]
                for pat in patterns:
                    for m in re.finditer(pat, page, re.DOTALL):
                        raw_href  = urllib.parse.unquote(m.group(1))
                        raw_title = _clean_html(m.group(2))
                        if raw_href.startswith("http") and raw_title and raw_href not in existing_urls:
                            found_urls.append((raw_href, raw_title, ""))
                            existing_urls.add(raw_href)
                        if len(found_urls) >= num_results * 2:
                            break
                    if len(found_urls) >= num_results:
                        break

            for url, title, snippet in found_urls:
                if url not in {r[0] for r in results}:
                    results.append((url, title, snippet))
                if len(results) >= num_results:
                    break

        except Exception as e:
            if not results:
                web_search(query)
                return f"Search unavailable right now ({e}). Opened your browser instead."

    # ── Nothing at all? Fall back to browser ─────────────────────────────────
    if not results:
        web_search(query)
        return "Couldn't pull results right now. Opened the search in your browser."

    # ── Gather raw content from each result ──────────────────────────────────
    raw_chunks: list[str] = []
    source_lines: list[str] = [f"🔍 Sources for: \"{query}\"\n"]
    wikipedia_title:   str = ""
    wikipedia_excerpt: str = ""   # first Wikipedia article's full extract

    for i, item in enumerate(results[:num_results], 1):
        url, title, snippet = item
        if gui_app:
            gui_app.after(0, lambda i=i, n=min(len(results), num_results):
                          gui_app.set_status(f"Reading result {i}/{n}…"))

        if "wikipedia.org" in url and len(snippet) >= 300:
            excerpt = snippet
            if not wikipedia_excerpt:
                wikipedia_excerpt = snippet
                wikipedia_title   = title
        elif len(snippet) < 300:
            scraped = _scrape_page(url, char_limit=2500)
            excerpt = scraped if not scraped.startswith("(couldn't") else (snippet or scraped)
        else:
            excerpt = snippet

        source_lines.append(f"{i}. {title}  —  {url}")
        raw_chunks.append(f"SOURCE {i} [{title}]:\n{excerpt}")

    raw_context = "\n\n".join(raw_chunks)

    # ── If Wikipedia content is available, return it directly ─────────────────
    # llama3.2 is too small to reliably follow strict grounding rules, so for
    # factual queries we bypass the LLM entirely and serve the Wikipedia text.
    if wikipedia_excerpt:
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status("Ready"))

        # Take the first 3 non-empty paragraphs from the Wikipedia extract
        paragraphs = [p.strip() for p in wikipedia_excerpt.split("\n") if len(p.strip()) > 60]
        summary_text = "  ".join(paragraphs[:3])[:700]
        return (
            f"{summary_text}\n\n"
            + "\n".join(source_lines)
        )

    # ── No Wikipedia — ask Ollama to summarise the scraped content ────────────
    if gui_app:
        gui_app.after(0, lambda: gui_app.set_status("Summarising…"))

    summary = None
    try:
        cfg = load_config()
        model = cfg.get("model", "llama3.2")
        summarise_prompt = (
            f"The user asked: \"{query}\"\n\n"
            "=== WEB CONTENT RETRIEVED RIGHT NOW ===\n"
            f"{raw_context[:6000]}\n"
            "=== END ===\n\n"
            "Extract the answer from the web content above. "
            "State it in 2-3 spoken sentences as Jarvis. "
            "Use ONLY facts present in the content above — do NOT use training data. "
            "If the content doesn't answer the question, say so honestly.\n\n"
            "Answer:"
        )
        sum_resp = requests.post(
            CFG.get("ollama_url", "http://localhost:11434/api/chat").replace("/api/chat", "/api/generate"),
            json={
                "model": model,
                "prompt": summarise_prompt,
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 200},
            },
            timeout=60,
        )
        sum_resp.raise_for_status()
        summary = sum_resp.json().get("response", "").strip()
    except Exception:
        pass

    if gui_app:
        gui_app.after(0, lambda: gui_app.set_status("Ready"))

    if summary:
        return summary + "\n\n" + "\n".join(source_lines)

    # Last resort: plain dump
    plain_lines = [f"🔍 Web results for: \"{query}\"\n"]
    for chunk in raw_chunks:
        plain_lines.append("─" * 60)
        plain_lines.append(chunk + "\n")
    if not _BS4_OK:
        plain_lines.append(
            "💡 Tip: install beautifulsoup4 for richer scraping — "
            "pip install beautifulsoup4"
        )
    return "\n".join(plain_lines)



def get_weather(location: str) -> str:
    try:
        url = f"https://wttr.in/{urllib.parse.quote_plus(location)}?format=4"
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        return f"🌤️ Weather for {location}:\n{resp.text.strip()}"
    except Exception as e:
        return f"Could not retrieve weather: {e}"



def take_screenshot(path: str = "") -> str:
    if not PYAUTOGUI_OK:
        return "pyautogui is required for screenshots. Run: pip install pyautogui"
    try:
        if not path:
            ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = str(HOME_DIR / "Desktop" / f"screenshot_{ts}.png")
        img = pyautogui.screenshot()
        img.save(path)
        return f"✅ Screenshot saved to: {path}"
    except Exception as e:
        return f"Error taking screenshot: {e}"



def clipboard_read() -> str:
    if not PYPERCLIP_OK:
        return "pyperclip is required. Run: pip install pyperclip"
    try:
        text = pyperclip.paste()
        return f"📋 Clipboard contents:\n{text[:2000]}" if text else "Clipboard is empty."
    except Exception as e:
        return f"Error reading clipboard: {e}"


def clipboard_write(text: str) -> str:
    if not PYPERCLIP_OK:
        return "pyperclip is required. Run: pip install pyperclip"
    try:
        pyperclip.copy(text)
        preview = text[:80] + ("…" if len(text) > 80 else "")
        return f"✅ Copied to clipboard: {preview}"
    except Exception as e:
        return f"Error writing to clipboard: {e}"



def _load_notes() -> list:
    if NOTES_FILE.exists():
        try:
            return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_notes(notes: list):
    NOTES_FILE.write_text(json.dumps(notes, indent=2), encoding="utf-8")


def add_note(text: str) -> str:
    notes = _load_notes()
    entry = {"id": len(notes) + 1, "text": text.strip(),
             "ts": datetime.now().strftime("%Y-%m-%d %H:%M"), "done": False}
    notes.append(entry)
    _save_notes(notes)
    return f"✅ Note #{entry['id']} saved: {text.strip()}"


def list_notes(show_done: bool = False) -> str:
    notes = _load_notes()
    if not notes:
        return "No notes saved, sir."
    visible = notes if show_done else [n for n in notes if not n.get("done")]
    if not visible:
        return "All notes are marked done, sir."
    lines = ["📝 Your notes:"]
    for n in visible:
        tick = "✓" if n.get("done") else "○"
        lines.append(f"  [{tick}] #{n['id']}  {n['text']}  ({n['ts']})")
    return "\n".join(lines)


def done_note(note_id: int) -> str:
    notes = _load_notes()
    for n in notes:
        if n["id"] == note_id:
            n["done"] = True
            _save_notes(notes)
            return f"✅ Note #{note_id} marked done."
    return f"Note #{note_id} not found."


def delete_note(note_id: int) -> str:
    notes = _load_notes()
    before = len(notes)
    notes = [n for n in notes if n["id"] != note_id]
    if len(notes) == before:
        return f"Note #{note_id} not found."
    _save_notes(notes)
    return f"✅ Note #{note_id} deleted."



def ping_host(host: str) -> str:
    try:
        param = "-n" if sys.platform == "win32" else "-c"
        cmd = f"ping {param} 4 {host}"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        output = (result.stdout + result.stderr).strip()
        if not output:
            return f"No response from {host}."
        lines = output.splitlines()
        summary = next((l for l in reversed(lines) if l.strip()), output[:300])
        return f"🌐 Ping {host}:\n{summary}"
    except subprocess.TimeoutExpired:
        return f"Ping to {host} timed out."
    except Exception as e:
        return f"Error: {e}"


def network_status() -> str:
    try:
        lines = ["🌐 Network Status:"]
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        for iface, addr_list in addrs.items():
            st = stats.get(iface)
            if st and not st.isup:
                continue
            for a in addr_list:
                if a.family.name in ("AF_INET", "AF_INET6") or str(a.family) in ("2", "10", "AddressFamily.AF_INET"):
                    if a.address and not a.address.startswith("127.") and a.address != "::1":
                        speed = f"{st.speed}Mbps" if st and st.speed else "?"
                        lines.append(f"  {iface}: {a.address}  ({speed})")
        try:
            requests.get("https://1.1.1.1", timeout=3)
            lines.append("  ✅ Internet: reachable")
        except Exception:
            lines.append("  ❌ Internet: unreachable")
        return "\n".join(lines) if len(lines) > 1 else "No active network interfaces found."
    except Exception as e:
        return f"Error: {e}"



def recent_files(directory: str = "", count: int = 10) -> str:
    try:
        base = Path(directory) if directory else HOME_DIR
        if not base.exists():
            return f"Path not found: {directory}"
        files = []
        for f in base.rglob("*"):
            if f.is_file():
                try:
                    files.append((f.stat().st_mtime, f))
                except Exception:
                    pass
        files.sort(reverse=True)
        top = files[:count]
        if not top:
            return f"No files found in {base}."
        lines = [f"📂 {count} most recent files in {base.name or str(base)}:"]
        for mtime, f in top:
            dt = datetime.fromtimestamp(mtime).strftime("%d %b %H:%M")
            lines.append(f"  {dt}  {f.name}  ({f.parent})")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"



def folder_sizes(path: str = "", top_n: int = 10) -> str:
    try:
        base = Path(path) if path else HOME_DIR
        if not base.exists():
            return f"Path not found: {path}"
        sizes = []
        for item in base.iterdir():
            try:
                if item.is_dir():
                    sz = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                    sizes.append((sz, item.name))
                elif item.is_file():
                    sizes.append((item.stat().st_size, item.name))
            except (PermissionError, OSError):
                pass
        sizes.sort(reverse=True)
        lines = [f"💾 Largest items in {base.name or str(base)}:"]
        for sz, name in sizes[:top_n]:
            if sz >= 1e9:
                s = f"{sz/1e9:.1f} GB"
            elif sz >= 1e6:
                s = f"{sz/1e6:.1f} MB"
            elif sz >= 1e3:
                s = f"{sz/1e3:.1f} KB"
            else:
                s = f"{sz} B"
            lines.append(f"  {s:>10}  {name}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"



def set_volume(level: int) -> str:
    level = max(0, min(100, level))
    try:
        script = (
            f"$obj = New-Object -ComObject WScript.Shell; "
            f"$vol = [Math]::Round({level} / 2); "
            f"1..100 | ForEach-Object {{ $obj.SendKeys([char]174) }}; "
            f"1..$vol | ForEach-Object {{ $obj.SendKeys([char]175) }}"
        )
        subprocess.run(["powershell", "-Command", script],
                       capture_output=True, timeout=10)
        return f"✅ Volume set to {level}%."
    except Exception as e:
        return f"Could not set volume: {e}"


def mute_volume() -> str:
    try:
        script = "$obj = New-Object -ComObject WScript.Shell; $obj.SendKeys([char]173)"
        subprocess.run(["powershell", "-Command", script], capture_output=True, timeout=5)
        return "✅ Volume toggled mute."
    except Exception as e:
        return f"Could not mute: {e}"



def set_reminder(message: str, minutes: float) -> str:
    if minutes <= 0 or minutes > 1440:
        return "Reminder must be between 1 and 1440 minutes from now."

    def _fire():
        time.sleep(minutes * 60)
        msg = f"⏰ Reminder: {message}"
        desktop_notify("JARVIS — Reminder", message)
        speak(f"Sir, a reminder: {message}")
        if gui_app:
            gui_app.after(0, lambda: gui_app.add_message("⏰ Reminder", message, tag="system"))

    threading.Thread(target=_fire, daemon=True).start()
    eta = (datetime.now() + timedelta(minutes=minutes)).strftime("%H:%M")
    return f"✅ Reminder set for {eta} ({minutes:.0f} min): {message}"



_ALLOWED_SCRIPT_EXTS = {".py", ".bat", ".cmd", ".sh", ".ps1"}

def run_script(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return f"Script not found: {path}"
    if p.suffix.lower() not in _ALLOWED_SCRIPT_EXTS:
        return f"Security policy: only {', '.join(_ALLOWED_SCRIPT_EXTS)} scripts are allowed."
    if gui_app:
        if not voice_confirm(f"Execute this script? {p.name}"):
            return "Script execution cancelled."
    if p.suffix.lower() == ".py":
        cmd = f'"{sys.executable}" "{path}"'
    else:
        cmd = f'"{path}"'
    return _stream_subprocess(cmd, f"Running {p.name}")



# ─────────────────────────────────────────────
def dispatch(action: dict) -> str:
    a = action.get("action", "")
    if   a == "list_files":    return list_files(action.get("path", str(HOME_DIR)),
                                                  action.get("folders_only", False))
    elif a == "read_file":     return read_file(action.get("path",""))
    elif a == "create_file":   return create_file(action.get("path",""), action.get("content",""))
    elif a == "delete_file":   return delete_file(action.get("path",""))
    elif a == "move_file":     return move_file(action.get("src",""), action.get("dst",""))
    elif a == "scan_folder":   return scan_folder(action.get("path", str(HOME_DIR / "Downloads")))
    elif a == "list_processes":return list_processes()
    elif a == "kill_process":  return kill_process(action.get("name",""))
    elif a == "open_app":      return open_app(action.get("name",""))
    elif a == "close_app":     return close_app(action.get("name",""))
    elif a == "list_windows":  return list_windows()
    elif a == "system_stats":  return system_stats()
    elif a == "run_command":   return run_command(action.get("cmd",""))
    elif a == "web_search":    return web_search(action.get("query",""))
    elif a == "open_link":     return open_link(action.get("url",""))
    elif a == "mouse_move":    return mouse_move(int(action.get("x",0)), int(action.get("y",0)))
    elif a == "mouse_move_rel": return mouse_move_rel(int(action.get("dx",0)), int(action.get("dy",0)))
    elif a == "mouse_click":   return mouse_click(action.get("x"), action.get("y"), action.get("button","left"), bool(action.get("double",False)))
    elif a == "mouse_scroll":  return mouse_scroll(action.get("direction","down"), int(action.get("amount",3)))
    elif a == "keyboard_type": return keyboard_type(action.get("text",""))
    elif a == "keyboard_hotkey": return keyboard_hotkey(*action.get("keys",[]))
    elif a == "switch_tab":    return switch_tab(action.get("direction","next"))
    elif a == "switch_window": return switch_window(action.get("title",""))
    elif a == "focus_app":     return focus_or_open_app(action.get("name",""))
    elif a == "minimize_win":  return minimize_window(action.get("name",""))
    elif a == "notify":
        desktop_notify(action.get("title","JARVIS"), action.get("message",""))
        return "Notification sent."
    elif a == "pip_install":    return pip_install(action.get("packages",""))
    elif a == "pip_uninstall":  return pip_uninstall(action.get("packages",""))
    elif a == "git_clone":      return git_clone(action.get("url",""), action.get("dest",""))
    elif a == "git_run":        return git_run(action.get("cmd","status"), action.get("path",""))
    elif a == "web_search_read":return web_search_read(action.get("query",""))
    elif a == "weather":        return get_weather(action.get("location","London"))
    elif a == "screenshot":     return take_screenshot(action.get("path",""))
    elif a == "clipboard_read": return clipboard_read()
    elif a == "clipboard_write":return clipboard_write(action.get("text",""))
    elif a == "remind":         return set_reminder(action.get("message",""), float(action.get("minutes",5)))
    elif a == "run_script":     return run_script(action.get("path",""))
    elif a == "add_note":       return add_note(action.get("text",""))
    elif a == "list_notes":     return list_notes(bool(action.get("show_done", False)))
    elif a == "done_note":      return done_note(int(action.get("id", 0)))
    elif a == "delete_note":    return delete_note(int(action.get("id", 0)))
    elif a == "ping":           return ping_host(action.get("host","8.8.8.8"))
    elif a == "network_status": return network_status()
    elif a == "recent_files":   return recent_files(action.get("path",""), int(action.get("count",10)))
    elif a == "folder_sizes":   return folder_sizes(action.get("path",""), int(action.get("top_n",10)))
    elif a == "set_volume":     return set_volume(int(action.get("level",50)))
    elif a == "mute":           return mute_volume()
    elif a == "learned_command": return run_learned_command(action.get("command",""))
    elif a == "learn_trigger": return handle_learn_trigger(action.get("phrase",""))
    else:
        return f"Unknown action: '{a}'"


def _load_learned() -> list:
    LEARNED_FILE = Path(__file__).parent / "jarvis_learned.json"
    if LEARNED_FILE.exists():
        try:
            return json.loads(LEARNED_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_learned(learned_list: list):
    LEARNED_FILE = Path(__file__).parent / "jarvis_learned.json"
    LEARNED_FILE.write_text(json.dumps(learned_list, indent=2), encoding="utf-8")


def run_learned_command(command_str: str) -> str:
    if not command_str:
        return "No command stored."
    return dispatch({"action": "run_command", "cmd": command_str})


def handle_learn_trigger(phrase: str) -> str:
    if not phrase:
        return "I didn't catch the phrase to learn, sir."
    speak(f"I'll learn how to {phrase}. Please show me the command you want me to run when you say that.")
    command_raw = listen_for_command("What command should I run?")
    if not command_raw:
        speak("I didn't hear a command, sir. Learning cancelled.")
        return "Learning cancelled due to no input."
    speak(f"You said: {command_raw}. Is that correct?")
    if not voice_confirm("Please confirm YES or NO."):
        speak("Learning cancelled.")
        return "Learning cancelled."
    learned = _load_learned()
    for entry in learned:
        if entry.get("trigger", "").lower() == phrase.lower():
            entry["command"] = command_raw.strip()
            _save_learned(learned)
            speak(f"I've updated the learned command for {phrase}, sir.")
            return f"Updated learned command for {phrase}."
    learned.append({"trigger": phrase.strip(), "command": command_raw.strip()})
    _save_learned(learned)
    speak(f"I've learned to {phrase} when you say that, sir.")
    return f"Learned new command for {phrase}."


import re as _re
def _direct_intent(t: str, raw: str):
    min_m = _re.search(r'(?:minimise|minimize)\s+(.+)', t)
    if min_m:
        target = min_m.group(1).strip()
        return ({"action": "minimize_win", "name": target},
                f"Minimising {target}.")

    url_match = _re.search(
        r'(?:open|go to|navigate to|launch|browse to)\s+(https?://\S+|[\w\-]+\.\w{2,}(?:/\S*)?)',
        t)
    if url_match:
        url = url_match.group(1)
        if not url.startswith("http"):
            url = "https://" + url
        return ({"action": "open_link", "url": url},
                f"Opening {url_match.group(1)} now, sir.")

    if _re.search(r'(next|forward|right)\s+tab|switch\s+tab\s*(forward|next|right)?|tab\s+right', t):
        return ({"action": "switch_tab", "direction": "next"},
                "Switching to the next tab.")
    if _re.search(r'(prev|previous|back|left)\s+tab|switch\s+tab\s*(back|prev|left)?|tab\s+left', t):
        return ({"action": "switch_tab", "direction": "prev"},
                "Switching to the previous tab.")

    sw = _re.search(r'(?:switch to|focus|bring up|go to|open|pull up|show)\s+(.+?)(?:\s+window)?$', t)
    if sw:
        title = sw.group(1).strip()
        skip = {"next tab", "previous tab", "prev tab", "youtube", "spotify"}
        skip_prefixes = ("https://", "http://", "www.")
        if title not in skip and not any(title.startswith(p) for p in skip_prefixes):
            return ({"action": "focus_app", "name": title},
                    f"Switching to {title}, sir.")

    screen_w, screen_h = 1920, 1080
    try:
        if PYAUTOGUI_OK:
            screen_w, screen_h = pyautogui.size()
    except Exception:
        pass

    corner = {
        "top left":     (0, 0),
        "top-left":     (0, 0),
        "top right":    (screen_w - 1, 0),
        "top-right":    (screen_w - 1, 0),
        "bottom left":  (0, screen_h - 1),
        "bottom-left":  (0, screen_h - 1),
        "bottom right": (screen_w - 1, screen_h - 1),
        "bottom-right": (screen_w - 1, screen_h - 1),
        "center":       (screen_w // 2, screen_h // 2),
        "centre":       (screen_w // 2, screen_h // 2),
        "middle":       (screen_w // 2, screen_h // 2),
        "top":          (screen_w // 2, 0),
        "bottom":       (screen_w // 2, screen_h - 1),
        "left":         (0, screen_h // 2),
        "right":        (screen_w - 1, screen_h // 2),
    }
    if _re.search(r'(?:move|go|put)\s+(?:the\s+)?(?:mouse|cursor)', t):
        # Match longest name first so "top right" beats bare "right"
        matched_name = None
        matched_pos  = None
        for name, (cx, cy) in sorted(corner.items(), key=lambda kv: -len(kv[0])):
            pattern = r'\b' + _re.escape(name) + r'\b'
            if _re.search(pattern, t):
                matched_name = name
                matched_pos  = (cx, cy)
                break
        if matched_name:
            x, y = matched_pos
            return ({"action": "mouse_move", "x": x, "y": y},
                    f"Moving the cursor to the {matched_name}, sir.")
        nums = _re.findall(r'\d+', t)
        if len(nums) >= 2:
            x, y = int(nums[-2]), int(nums[-1])
            return ({"action": "mouse_move", "x": x, "y": y},
                    f"Moving the cursor to {x}, {y}.")

    if _re.search(r'(?:left.?click|click)\s+(?:at\s+)?(\d+)[,\s]+(\d+)', t):
        m = _re.search(r'(\d+)[,\s]+(\d+)', t)
        x, y = int(m.group(1)), int(m.group(2))
        return ({"action": "mouse_click", "x": x, "y": y, "button": "left"},
                f"Left-clicking at {x}, {y}.")
    if _re.search(r'right.?click\s+(?:at\s+)?(\d+)[,\s]+(\d+)', t):
        m = _re.search(r'(\d+)[,\s]+(\d+)', t)
        x, y = int(m.group(1)), int(m.group(2))
        return ({"action": "mouse_click", "x": x, "y": y, "button": "right"},
                f"Right-clicking at {x}, {y}.")
    # "left click the top right" / "click the centre"
    if _re.search(r'(?:left.?click|click)\s+(?:the\s+)?[\w]', t):
        for name, (x, y) in corner.items():
            if name in t:
                return ({"action": "mouse_click", "x": x, "y": y, "button": "left"},
                        f"Left-clicking the {name}, sir.")
    # bare "left click" / "click here" — click at current cursor position
    if _re.search(r'\b(?:left.?click|click\s+here|left\s+click\s+here)\b', t):
        return ({"action": "mouse_click", "button": "left"},
                "Left-clicking at the current cursor position.")

    if _re.search(r'scroll\s+(?:the\s+page\s+)?(?:down|up)', t):
        direction = "down" if "down" in t else "up"
        amt_m = _re.search(r'(\d+)', t)
        amt = int(amt_m.group(1)) if amt_m else 3
        return ({"action": "mouse_scroll", "direction": direction, "amount": amt},
                f"Scrolling {direction}.")

    hotkey_map = {
        r'copy':          (["ctrl", "c"], "Copied to clipboard."),
        r'paste':         (["ctrl", "v"], "Pasted from clipboard."),
        r'undo':          (["ctrl", "z"], "Undone."),
        r'redo':          (["ctrl", "y"], "Redone."),
        r'select all':    (["ctrl", "a"], "Selected all."),
        r'save':          (["ctrl", "s"], "Saved."),
        r'close tab':     (["ctrl", "w"], "Tab closed."),
        r'new tab':       (["ctrl", "t"], "New tab opened."),
        r'address bar':   (["ctrl", "l"], "Address bar focused."),
        r'refresh|reload':(["f5"],        "Page refreshed."),
        r'fullscreen':    (["f11"],       "Toggled fullscreen."),
        r'show desktop':  (["win", "d"],  "Desktop revealed."),
        r'file explorer': (["win", "e"],  "File Explorer opened."),
        r'alt tab':       (["alt", "tab"],"Switching windows."),
    }
    for pattern, (keys, reply) in hotkey_map.items():
        if _re.search(pattern, t):
            return ({"action": "keyboard_hotkey", "keys": keys}, reply)

    type_m = _re.search(r"type\s+(?:out\s+)?(.+?)\s*$", raw.strip(), _re.IGNORECASE)
    if type_m:
        text_to_type = type_m.group(1)
        return ({"action": "keyboard_type", "text": text_to_type},
                f"Typing that out for you, sir.")

    learned = _load_learned()
    for entry in learned:
        trigger = entry.get("trigger", "").strip().lower()
        if trigger and t == trigger:
            command = entry.get("command", "").strip()
            if command:
                return ({"action": "learned_command", "command": command},
                        f"Running learned command for {trigger}, sir.")

    return None


def _direct_intent_extended(t: str, raw: str):
    m = _re.search(r'(?:pip\s+)?install\s+(?:package\s+)?(.+)', t)
    if m and not _re.search(r'(ollama|app|application|program|software)', t):
        pkgs = m.group(1).strip()
        return ({"action": "pip_install", "packages": pkgs},
                f"Installing {pkgs} via pip now, sir.")

    m = _re.search(r'pip\s+uninstall\s+(.+)', t)
    if m:
        pkgs = m.group(1).strip()
        return ({"action": "pip_uninstall", "packages": pkgs},
                f"Uninstalling {pkgs}, sir.")

    m = _re.search(r'(?:git\s+)?clone\s+(https?://\S+|git@\S+)', t)
    if m:
        url  = m.group(1).rstrip(".")
        dest = str(HOME_DIR / "Projects")
        return ({"action": "git_clone", "url": url, "dest": dest},
                f"Cloning the repository, sir.")

    m = _re.search(r'git\s+(status|pull|log|branch|fetch|diff|stash)(?:\s+in\s+(.+))?', t)
    if m:
        cmd  = m.group(1)
        path = m.group(2).strip() if m.group(2) else ""
        return ({"action": "git_run", "cmd": cmd, "path": path},
                f"Running git {cmd}, sir.")

    m = _re.search(r'(?:weather|temperature|forecast)(?:\s+(?:in|for|at))?\s+([a-zA-Z][a-zA-Z\s,]+)', t)
    if m:
        loc = m.group(1).strip()
        return ({"action": "weather", "location": loc},
                f"Checking the weather in {loc}, sir.")
    if _re.search(r"(?:what'?s?\s+the\s+weather|how'?s?\s+the\s+weather|is\s+it\s+(?:hot|cold|raining))", t):
        return ({"action": "weather", "location": "London"},
                "Checking local weather, sir.")

    if _re.search(r'screenshot|screen\s*shot|capture\s+(?:the\s+)?screen', t):
        return ({"action": "screenshot", "path": ""},
                "Capturing the screen, sir.")

    if _re.search(r"(?:read|get|what.s\s+(?:on|in))\s+(?:my\s+)?clipboard", t):
        return ({"action": "clipboard_read"},
                "Reading your clipboard, sir.")
    m = _re.search(r'copy\s+(?:to\s+clipboard\s+)?["\'](.+?)["\']', t)
    if m:
        return ({"action": "clipboard_write", "text": m.group(1)},
                "Copied to clipboard, sir.")

    m = _re.search(r'remind\s+(?:me\s+)?(?:in\s+)?(\d+)\s+(minute|min|hour|hr)s?\s*(?:to\s+(.+))?', t)
    if m:
        amount = int(m.group(1))
        unit   = m.group(2)
        mins   = amount * 60 if unit.startswith("h") else amount
        msg    = m.group(3).strip() if m.group(3) else "Reminder"
        return ({"action": "remind", "message": msg, "minutes": mins},
                f"Reminder set for {mins} minutes from now, sir.")

    m = _re.search(r'search\s+(?:the\s+web\s+(?:for\s+)?|online\s+(?:for\s+)?|for\s+)(.+)', t)
    if m:
        query = m.group(1).strip()
        return ({"action": "web_search_read", "query": query},
                f"Searching for {query}, sir.")

    m = _re.search(r'run\s+(?:script\s+)?(?:file\s+)?["\']?(.+\.(?:py|bat|sh|ps1|cmd))["\']?', t)
    if m:
        path = m.group(1).strip()
        return ({"action": "run_script", "path": path},
                f"Running {Path(path).name}, sir.")

    m = _re.search(r'(?:add\s+(?:a\s+)?note|note\s+(?:down|that)?|remember\s+(?:that\s+)?|todo[:\s]+)\s*(.+)', t)
    if m:
        text = m.group(1).strip()
        return ({"action": "add_note", "text": text},
                f"Note saved, sir.")
    if _re.search(r'(?:list|show|read|what(?:\'?s| are))\s+(?:my\s+)?(?:notes?|todos?|tasks?)', t):
        return ({"action": "list_notes"},
                "Here are your notes, sir.")
    m = _re.search(r'(?:mark|set|complete)\s+note\s+#?(\d+)\s+(?:as\s+)?done', t)
    if m:
        return ({"action": "done_note", "id": int(m.group(1))},
                f"Note {m.group(1)} marked done, sir.")
    m = _re.search(r'delete\s+note\s+#?(\d+)', t)
    if m:
        return ({"action": "delete_note", "id": int(m.group(1))},
                f"Note {m.group(1)} deleted, sir.")

    m = _re.search(r'ping\s+([\w.\-]+)', t)
    if m:
        return ({"action": "ping", "host": m.group(1)},
                f"Pinging {m.group(1)}, sir.")

    if _re.search(r'(?:network|internet|connection|wifi|ip\s+address)\s*(?:status|info|check|speed)?', t):
        return ({"action": "network_status"},
                "Checking network status, sir.")

    m = _re.search(r'recent\s+files?(?:\s+in\s+(.+))?', t)
    if m:
        path = m.group(1).strip() if m.group(1) else ""
        return ({"action": "recent_files", "path": path},
                "Pulling recent files, sir.")

    if _re.search(r'(?:disk\s+usage|folder\s+sizes?|what(?:\'?s| is)\s+taking\s+(?:up\s+)?space)', t):
        m = _re.search(r'in\s+(.+?)(?:\s*$)', t)
        path = m.group(1).strip() if m else ""
        return ({"action": "folder_sizes", "path": path},
                "Analysing folder sizes, sir.")

    if _re.search(r'mute|silence\s+(?:the\s+)?(?:volume|audio|sound)', t):
        return ({"action": "mute"}, "Muting, sir.")
    m = _re.search(r'(?:set\s+)?volume\s+(?:to\s+)?(\d+)', t)
    if m:
        return ({"action": "set_volume", "level": int(m.group(1))},
                f"Setting volume to {m.group(1)} percent, sir.")
    if _re.search(r'(?:turn\s+(?:up|down)|increase|decrease|lower|raise)\s+(?:the\s+)?(?:volume|audio|sound)', t):
        delta = 20 if any(w in t for w in ("up","increase","raise")) else -20
        return ({"action": "set_volume", "level": 50 + delta},
                f"Adjusting volume, sir.")

    # ── Factual / informational catch-all → always web-search, never the LLM ──
    # Question words and info requests go straight to web_search_read so the
    # model cannot answer from its (unreliable) training data.
    _FACTUAL = _re.search(
        r'^(?:what|who|where|when|why|how|which|give\s+me|tell\s+me|'
        r'info(?:rmation)?\s+on|look\s+up|find\s+out|do\s+you\s+know)',
        t.strip()
    )
    if _FACTUAL:
        return ({"action": "web_search_read", "query": raw.strip()},
                "Looking that up now, sir.")

    return None



# ─────────────────────────────────────────────
#  SINGLE-COMMAND EXECUTOR  (no multi-command splitting)
# ─────────────────────────────────────────────
def _run_single_command(text: str):
    """Execute exactly one command (no multi-split).  Called by handle_command."""
    if not text or not text.strip():
        return
    t = text.strip().lower()
    if gui_app:
        gui_app.after(0, lambda: gui_app.set_status("Thinking..."))

    # ── Direct intent (fast path, no AI) ──────────────────────────────────
    direct = _direct_intent(t, text) or _direct_intent_extended(t, text)
    if direct is not None:
        action, reply = direct
        if gui_app:
            gui_app.after(0, lambda a=action: gui_app.set_status(f"Running {a.get('action')}"))
        result = dispatch(action)
        if gui_app:
            gui_app.after(0, lambda r=result: gui_app.add_message("Result", r, tag="system"))
        speak(reply)
        return

    # ── AI path ────────────────────────────────────────────────────────────
    response = ask_ai(text)
    actions  = extract_all_actions(response)

    if actions:
        for action in actions:
            if gui_app:
                gui_app.after(0, lambda a=action: gui_app.set_status(f"Running {a.get('action')}"))
            result = dispatch(action)
            if gui_app:
                gui_app.after(0, lambda r=result: gui_app.add_message("Result", r, tag="system"))
        # Speak just the text lines from the AI response (strip the JSON action lines)
        spoken = "\n".join(
            line for line in response.splitlines()
            if not line.strip().startswith("{")
        ).strip()
        if spoken:
            speak(spoken)
    else:
        speak(response)


# ─────────────────────────────────────────────
#  COMMAND HANDLER
#  IMPORTANT: handle_command must ALWAYS run on a daemon thread, never the
#  main/GUI thread.  All GUI mutations go through gui_app.after(0, ...).
# ─────────────────────────────────────────────
def handle_command(text: str):
    if not text:
        return
    if gui_app:
        gui_app.after(0, lambda: gui_app.set_status("Thinking…"))

    t = text.strip().lower()

    # ── Built-ins ──────────────────────────────────────────────────────────
    if t in ("help", "commands", "what can you do", "jarvis help"):
        help_text = (
            "I'm at your disposal, sir. Built-in commands: status, clear memory, settings, quit. "
            "Voice: say my name to wake me, then speak your command. All confirmations are by voice — say YES or NO. "
            "You may also say: 'install requests', 'git clone https://...', 'git status', "
            "'weather in Tokyo', 'screenshot', 'read my clipboard', "
            "'remind me in 10 minutes to call Bob', 'search online for Python tips', "
            "'run script myscript.py', 'add note buy milk', 'show my notes', "
            "'ping google.com', 'network status', 'recent files', 'folder sizes', "
            "'set volume to 50', 'mute', or just ask me anything naturally. "
            "You can chain commands too — just say 'open notepad and then open chrome'."
        )
        speak(help_text)
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status("Ready"))
        return

    if t in ("status", "system status", "system stats"):
        result = system_stats()
        if gui_app:
            gui_app.after(0, lambda r=result: gui_app.add_message("System", r, tag="system"))
        cpu = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory().percent
        speak(f"CPU's at {cpu:.0f}%, RAM at {ram:.0f}%. Looking fine.")
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status("Ready"))
        return

    if t in ("clear", "clear history", "clear memory", "reset", "forget everything", "new conversation"):
        chat_history.clear()
        speak("Done. Fresh start.")
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status("Ready"))
        return

    if t in ("reconfigure", "setup", "settings", "change settings", "change my name"):
        if gui_app:
            gui_app.after(0, gui_app.open_settings)
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status("Ready"))
        return

    if t in ("quit", "exit", "bye", "goodbye", "shut down", "shutdown", "turn off"):
        speak(f"Going offline, {CFG.get('owner_name','sir')}. Try not to break anything.")
        global running
        running = False
        os._exit(0)

    if t in ("minimize", "minimise", "minimise window", "minimize window"):
        if gui_app:
            gui_app.after(0, gui_app.iconify)
        speak("Minimised.")
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status("Ready"))
        return

    if t in ("hide", "go to tray", "hide window", "tray"):
        if gui_app:
            gui_app.after(0, gui_app.withdraw)
        speak("Hidden to tray, sir.")
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status("Ready"))
        return

    if t in ("enable startup", "start on boot", "add to startup", "run on startup", "autostart"):
        result = register_startup()
        speak(result)
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status("Ready"))
        return

    if t in ("disable startup", "remove from startup", "don't start on boot", "remove autostart"):
        result = unregister_startup()
        speak(result)
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status("Ready"))
        return

    # ── Multi-command split ────────────────────────────────────────────────
    # Split "open notepad and then open chrome" into individual sub-commands.
    # Each sub runs through _run_single_command which handles direct-intent
    # AND AI fallback, so mixed commands like "open chrome and then what's 2+2"
    # work correctly too.
    sub_commands = _split_multi_command(text)
    if len(sub_commands) > 1:
        speak(f"On it, running {len(sub_commands)} commands, sir.")
        for sub in sub_commands:
            _run_single_command(sub)
        if gui_app:
            gui_app.after(0, lambda: gui_app.set_status("Ready"))
        return

    # ── Single command ─────────────────────────────────────────────────────
    _run_single_command(text)
    if gui_app:
        gui_app.after(0, lambda: gui_app.set_status("Ready"))



def background_monitor():
    while running:
        time.sleep(CFG.get("monitor_interval", 60))
        try:
            cpu = psutil.cpu_percent(interval=2)
            ram = psutil.virtual_memory().percent
            if cpu > CFG.get("cpu_alert", 90):
                msg = f"Sir, CPU usage has reached {cpu:.0f} percent. You may wish to investigate."
                desktop_notify("⚠️ High CPU Usage", msg)
                speak(msg)
            if ram > CFG.get("ram_alert", 90):
                msg = f"Sir, RAM usage is at {ram:.0f} percent. Memory is running rather thin."
                desktop_notify("⚠️ High RAM Usage", msg)
                speak(msg)
        except Exception:
            pass



def make_geass_icon(size: int = 64) -> Image.Image:
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size // 2, size // 2
    r = size // 2 - 2

    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 fill=(10, 0, 5, 230), outline=(180, 0, 30), width=max(1, size // 32))

    ro = int(r * 0.88)
    draw.ellipse([cx - ro, cy - ro, cx + ro, cy + ro],
                 outline=(200, 160, 20), width=max(1, size // 22))

    ri = int(r * 0.55)
    draw.ellipse([cx - ri, cy - ri, cx + ri, cy + ri],
                 outline=(220, 20, 40), width=max(1, size // 28))

    rp = int(r * 0.22)
    draw.ellipse([cx - rp, cy - rp, cx + rp, cy + rp],
                 fill=(240, 30, 50), outline=(255, 180, 180), width=max(1, size // 40))

    spoke_outer = int(r * 0.83)
    spoke_inner = int(r * 0.55)
    spoke_w     = max(1, size // 40)
    for i in range(6):
        angle_rad = math.radians(i * 60 - 90)
        outer_r = spoke_outer if i % 2 == 0 else spoke_inner
        x1 = cx + int(spoke_inner * math.cos(angle_rad))
        y1 = cy + int(spoke_inner * math.sin(angle_rad))
        x2 = cx + int(outer_r    * math.cos(angle_rad))
        y2 = cy + int(outer_r    * math.sin(angle_rad))
        colour = (200, 160, 20) if i % 2 == 0 else (220, 20, 40)
        draw.line([x1, y1, x2, y2], fill=colour, width=spoke_w)

    tip_r = max(1, size // 20)
    for i in range(0, 6, 2):
        angle_rad = math.radians(i * 60 - 90)
        tx = cx + int(spoke_outer * math.cos(angle_rad))
        ty = cy + int(spoke_outer * math.sin(angle_rad))
        draw.ellipse([tx - tip_r, ty - tip_r, tx + tip_r, ty + tip_r],
                     fill=(220, 180, 30))

    wing_r = int(r * 0.70)
    wing_w = max(1, size // 28)
    draw.arc([cx - wing_r - int(r*0.25), cy - wing_r,
              cx - int(r*0.25),           cy + wing_r],
             start=200, end=340, fill=(220, 20, 40), width=wing_w)
    draw.arc([cx + int(r*0.25),           cy - wing_r,
              cx + wing_r + int(r*0.25),  cy + wing_r],
             start=200, end=340, fill=(220, 20, 40), width=wing_w)

    return img


def _set_window_icon(root: tk.Tk):
    try:
        import io, tempfile, os as _os
        icon_img = make_geass_icon(64)
        buf = io.BytesIO()
        icon_img.save(buf, format="ICO", sizes=[(64, 64), (32, 32), (16, 16)])
        buf.seek(0)
        tmp = tempfile.NamedTemporaryFile(suffix=".ico", delete=False)
        tmp.write(buf.read())
        tmp.close()
        root.iconbitmap(tmp.name)
        root.after(4000, lambda: _os.unlink(tmp.name) if _os.path.exists(tmp.name) else None)
    except Exception:
        try:
            icon_img = make_geass_icon(32)
            photo = ImageTk.PhotoImage(icon_img)
            root.iconphoto(True, photo)
            root._geass_icon_ref = photo
        except Exception:
            pass


def make_tray_image() -> Image.Image:
    return make_geass_icon(64)


def show_jarvis_tray(icon, item):
    if gui_app:
        gui_app.after(0, gui_app.deiconify)
        gui_app.after(0, lambda: gui_app.lift())
        gui_app.after(0, lambda: gui_app.focus_force())


def toggle_listening_tray(icon, item):
    global listening
    listening = not listening
    desktop_notify("JARVIS", f"Wake word: {'ON' if listening else 'OFF'}")


def quit_jarvis_tray(icon, item):
    global running
    running = False
    icon.stop()
    os._exit(0)


def run_tray():
    global tray_icon
    owner = CFG.get("owner_name", "User")
    menu  = Menu(
        MenuItem("Show JARVIS",       show_jarvis_tray, default=True),
        MenuItem("Toggle Wake Word",  toggle_listening_tray),
        MenuItem("Clear Memory",      lambda i, it: chat_history.clear()),
        MenuItem("Quit JARVIS",       quit_jarvis_tray),
    )
    tray_icon = Icon("JARVIS", make_tray_image(), f"JARVIS — {owner}", menu)
    tray_icon.run()



def _list_microphones() -> list[tuple[int, str]]:
    if _PYAUDIO_OK:
        try:
            return list(enumerate(sr.Microphone.list_microphone_names()))
        except Exception:
            pass

    if _SOUNDDEVICE_OK:
        try:
            import sounddevice as _sd
            result = []
            for i, d in enumerate(_sd.query_devices()):
                try:
                    ch = d['max_input_channels'] if isinstance(d, dict) else getattr(d, 'max_input_channels', 0)
                    name = d['name'] if isinstance(d, dict) else getattr(d, 'name', str(d))
                    if int(ch) > 0:
                        result.append((i, name))
                except Exception:
                    pass
            if result:
                return result
        except Exception:
            pass

    try:
        import subprocess as _sp
        ps = (
            "Get-WmiObject Win32_SoundDevice | "
            "Select-Object -ExpandProperty Name"
        )
        out = _sp.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            timeout=5, text=True, stderr=_sp.DEVNULL
        )
        names = [n.strip() for n in out.strip().splitlines() if n.strip()]
        if names:
            return list(enumerate(names))
    except Exception:
        pass

    return []


class SettingsDialog(tk.Toplevel):
    BG    = "#0d1117"
    PANEL = "#161b22"
    ACCENT= "#58a6ff"
    FG    = "#e6edf3"
    ENTRY_BG = "#21262d"

    def __init__(self, parent):
        super().__init__(parent)
        self.title("JARVIS — Settings")
        self.configure(bg=self.BG)
        self.resizable(True, True)
        self.geometry("480x620")
        self.minsize(460, 560)
        self.grab_set()

        self.result = None
        self._build()

    def _label(self, parent, text, row, col=0, **kw):
        tk.Label(parent, text=text, bg=self.PANEL, fg=self.FG,
                 font=("Segoe UI", 10), **kw).grid(row=row, column=col,
                 sticky="w", padx=12, pady=6)

    def _entry(self, parent, row, default=""):
        e = tk.Entry(parent, bg=self.ENTRY_BG, fg=self.FG, insertbackground=self.FG,
                     relief="flat", font=("Segoe UI", 10), width=28,
                     highlightthickness=1, highlightcolor=self.ACCENT,
                     highlightbackground="#30363d")
        e.insert(0, default)
        e.grid(row=row, column=1, padx=12, pady=6, sticky="ew")
        return e

    def _build(self):
        # ── header ────────────────────────────────────────────────────────────
        header = tk.Frame(self, bg=self.PANEL, height=56)
        header.pack(fill="x")
        tk.Label(header, text="⚙  Settings", bg=self.PANEL, fg=self.ACCENT,
                 font=("Segoe UI", 14, "bold")).pack(side="left", padx=16, pady=12)

        # ── bottom button bar (packed BEFORE body so it stays pinned) ─────────
        btn_frame = tk.Frame(self, bg=self.BG)
        btn_frame.pack(side="bottom", fill="x", padx=16, pady=12)
        tk.Button(btn_frame, text="Save", bg=self.ACCENT, fg="#0d1117",
                  font=("Segoe UI", 10, "bold"), relief="flat", padx=20, pady=8,
                  cursor="hand2", command=self._save).pack(side="right", padx=4)
        tk.Button(btn_frame, text="Cancel", bg="#21262d", fg=self.FG,
                  font=("Segoe UI", 10), relief="flat", padx=20, pady=8,
                  cursor="hand2", command=self.destroy).pack(side="right", padx=4)

        # ── scrollable body ───────────────────────────────────────────────────
        body = tk.Frame(self, bg=self.BG)
        body.pack(fill="both", expand=True)

        canvas = tk.Canvas(body, bg=self.PANEL, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        form = tk.Frame(canvas, bg=self.PANEL)
        form_window = canvas.create_window((0, 0), window=form, anchor="nw")

        def _on_canvas_resize(event):
            canvas.itemconfig(form_window, width=event.width)
        canvas.bind("<Configure>", _on_canvas_resize)

        def _on_form_resize(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        form.bind("<Configure>", _on_form_resize)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        self.protocol("WM_DELETE_WINDOW", lambda: (
            canvas.unbind_all("<MouseWheel>"), self.destroy()))

        form.columnconfigure(1, weight=1)

        # ── fields ────────────────────────────────────────────────────────────
        self._label(form, "Your name", 0)
        self.name_var = self._entry(form, 0, CFG.get("owner_name", ""))

        self._label(form, "Wake word", 1)
        self.wake_var = self._entry(form, 1, CFG.get("wake_word", "jarvis"))

        self._label(form, "Voice speed\n(words/min)", 2)
        self.speed_var = self._entry(form, 2, str(CFG.get("voice_speed", 175)))

        self._label(form, "Ollama model", 3)
        self.model_var = self._entry(form, 3, CFG.get("model", "llama3.2"))

        self._label(form, "CPU alert %", 4)
        self.cpu_var = self._entry(form, 4, str(CFG.get("cpu_alert", 90)))

        self._label(form, "RAM alert %", 5)
        self.ram_var = self._entry(form, 5, str(CFG.get("ram_alert", 90)))

        # ── microphone row ────────────────────────────────────────────────────
        self._label(form, "Microphone", 6)

        mic_frame = tk.Frame(form, bg=self.PANEL)
        mic_frame.grid(row=6, column=1, padx=12, pady=6, sticky="ew")
        mic_frame.columnconfigure(0, weight=1)

        self._mic_list = _list_microphones()
        self.mic_cb = ttk.Combobox(mic_frame, state="readonly", font=("Segoe UI", 10))
        self.mic_cb.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._populate_mic_combobox()

        # Refresh button — re-scans for mics
        tk.Button(mic_frame, text="↺", bg="#21262d", fg=self.ACCENT,
                  font=("Segoe UI", 11, "bold"), relief="flat", width=3,
                  cursor="hand2", command=self._refresh_mics,
                  activebackground="#30363d", activeforeground=self.ACCENT
                  ).grid(row=0, column=1, padx=(0, 2))

        # Test button — records 2 s and shows whether audio was heard
        self._test_btn = tk.Button(mic_frame, text="Test", bg="#21262d", fg=self.FG,
                  font=("Segoe UI", 9), relief="flat", padx=6,
                  cursor="hand2", command=self._test_mic,
                  activebackground="#30363d", activeforeground=self.ACCENT)
        self._test_btn.grid(row=0, column=2)

        # Status label shown after test
        self._mic_status = tk.Label(form, text="", bg=self.PANEL,
                                    fg=self.ACCENT, font=("Segoe UI", 9, "italic"))
        self._mic_status.grid(row=7, column=1, padx=12, sticky="w")

        # ── startup checkbox ──────────────────────────────────────────────────
        self._label(form, "Start on boot", 8)
        self._startup_var = tk.BooleanVar(value=is_startup_registered())
        tk.Checkbutton(form, variable=self._startup_var,
                       bg=self.PANEL, fg=self.FG, selectcolor=self.ENTRY_BG,
                       activebackground=self.PANEL, activeforeground=self.ACCENT,
                       font=("Segoe UI", 10), relief="flat",
                       text="Launch JARVIS at Windows login"
                       ).grid(row=8, column=1, padx=12, pady=6, sticky="w")

    def _populate_mic_combobox(self):
        """Fill the combobox from self._mic_list and restore saved selection."""
        mics = self._mic_list
        if mics:
            self.mic_cb["values"] = [f"[{i}] {n}" for i, n in mics]
            cur_mic = CFG.get("mic_index")
            if cur_mic is not None:
                pos = next((p for p, (dev_idx, _) in enumerate(mics) if dev_idx == cur_mic), 0)
            else:
                pos = 0
            self.mic_cb.current(pos)
        else:
            self.mic_cb["values"] = ["(no microphones found)"]
            self.mic_cb.current(0)

    def _refresh_mics(self):
        """Re-scan microphones and repopulate the dropdown."""
        self._mic_list = _list_microphones()
        self._populate_mic_combobox()
        count = len(self._mic_list)
        self._mic_status.config(
            fg=self.ACCENT if count else "#ff6b6b",
            text=f"Found {count} mic(s)." if count else "No microphones detected."
        )

    def _test_mic(self):
        """Quick 2-second listen to verify the selected mic picks up audio."""
        sel = self.mic_cb.current()
        if sel < 0 or not self._mic_list:
            self._mic_status.config(fg="#ff6b6b", text="Select a mic first.")
            return

        mic_idx = self._mic_list[sel][0]
        self._test_btn.config(state="disabled", text="…")
        self._mic_status.config(fg=self.ACCENT, text="Listening for 2 s — say something…")
        self.update_idletasks()

        def _do_test():
            try:
                r = sr.Recognizer()
                r.energy_threshold = 200
                r.dynamic_energy_threshold = True
                with sr.Microphone(device_index=mic_idx) as src:
                    r.adjust_for_ambient_noise(src, duration=0.3)
                    audio = r.listen(src, timeout=3, phrase_time_limit=2)
                # If we got here, audio was captured successfully
                msg = ("✔ Mic working — audio captured.", "#3fb950")
            except sr.WaitTimeoutError:
                msg = ("✘ Nothing heard — check mic or try another.", "#ff6b6b")
            except Exception as e:
                msg = (f"✘ Error: {e}", "#ff6b6b")

            self.after(0, lambda: self._mic_status.config(fg=msg[1], text=msg[0]))
            self.after(0, lambda: self._test_btn.config(state="normal", text="Test"))

        threading.Thread(target=_do_test, daemon=True, name="mic-test").start()

    def _save(self):
        global CFG
        try:
            speed = int(self.speed_var.get())
        except ValueError:
            speed = 175
        try:
            cpu_a = int(self.cpu_var.get())
        except ValueError:
            cpu_a = 90
        try:
            ram_a = int(self.ram_var.get())
        except ValueError:
            ram_a = 90

        mic_idx = None
        sel = self.mic_cb.current()
        if sel >= 0 and self._mic_list:
            mic_idx = self._mic_list[sel][0]

        CFG.update({
            "owner_name":  self.name_var.get().strip() or CFG.get("owner_name",""),
            "wake_word":   self.wake_var.get().strip().lower() or "jarvis",
            "voice_speed": speed,
            "model":       self.model_var.get().strip() or "llama3.2",
            "cpu_alert":   cpu_a,
            "ram_alert":   ram_a,
            "mic_index":   mic_idx,
        })
        save_config(CFG)
        if tts_engine:
            tts_engine.setProperty("rate", speed)
        if self._startup_var.get():
            register_startup()
        else:
            unregister_startup()
        self.destroy()
        if gui_app:
            gui_app.after(0, lambda: gui_app.add_message("JARVIS", "Settings saved.", tag="jarvis"))


class SplashScreen(tk.Toplevel):
    BG     = "#020d18"
    ACCENT = "#00d4ff"
    ACCENT2= "#0077b6"
    ACCENT3= "#48cae4"
    FG     = "#caf0f8"
    FG_DIM = "#1a4a6a"

    _BOOT_LINES = [
        "INITIALISING J.A.R.V.I.S. ...",
        "LOADING NEURAL INTERFACE ...",
        "CALIBRATING VOICE SYSTEMS ...",
        "SCANNING ENVIRONMENT ...",
        "ESTABLISHING OLLAMA LINK ...",
        "ALL SYSTEMS ONLINE.",
    ]

    def __init__(self, root: tk.Tk):
        super().__init__(root)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=self.BG)
        w, h = 600, 420
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self._angle  = 0
        self._angle2 = 0
        self._angle3 = 0
        self._line_idx = 0
        self._alive  = True
        self._scan_y = 0
        self._build()
        self._spin()
        self._advance_text()
        self._scan()

    def _build(self):
        self.canvas = tk.Canvas(self, width=600, height=420,
                                bg=self.BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        # static labels on canvas
        self.canvas.create_text(300, 310, text="J.A.R.V.I.S.",
            font=("Courier New", 26, "bold"), fill=self.ACCENT, tags="title")
        self.canvas.create_text(300, 338, text="JUST A RATHER VERY INTELLIGENT SYSTEM",
            font=("Courier New", 7), fill=self.FG_DIM, tags="sub")
        self._status_id = self.canvas.create_text(300, 368,
            text=self._BOOT_LINES[0], font=("Courier New", 9), fill=self.ACCENT3)
        # progress bar background
        self.canvas.create_rectangle(110, 388, 490, 396,
            fill="#061220", outline=self.FG_DIM, width=1, tags="progbg")
        self._prog_bar = self.canvas.create_rectangle(110, 388, 110, 396,
            fill=self.ACCENT, outline="", tags="prog")

    def _draw_rings(self):
        c = self.canvas
        c.delete("ring")
        cx, cy = 300, 175
        # outer dim circle
        for r, col, w in [(130, self.FG_DIM, 1), (108, self.ACCENT2, 1), (82, self.FG_DIM, 1)]:
            c.create_oval(cx-r, cy-r, cx+r, cy+r, outline=col, width=w, tags="ring")
        # animated arcs
        c.create_arc(cx-130, cy-130, cx+130, cy+130,
            start=self._angle, extent=220, outline=self.ACCENT, width=3,
            style="arc", tags="ring")
        c.create_arc(cx-130, cy-130, cx+130, cy+130,
            start=self._angle+240, extent=60, outline=self.ACCENT3, width=2,
            style="arc", tags="ring")
        c.create_arc(cx-108, cy-108, cx+108, cy+108,
            start=-self._angle2, extent=160, outline=self.ACCENT2, width=2,
            style="arc", tags="ring")
        c.create_arc(cx-108, cy-108, cx+108, cy+108,
            start=-self._angle2+180, extent=80, outline=self.ACCENT3, width=1,
            style="arc", tags="ring")
        c.create_arc(cx-82, cy-82, cx+82, cy+82,
            start=self._angle3, extent=270, outline=self.ACCENT, width=2,
            style="arc", tags="ring")
        # tick marks on outer ring
        for i in range(24):
            rad = math.radians(i * 15 + self._angle * 0.2)
            r_in  = 122 if i % 6 == 0 else 126
            x1 = cx + r_in  * math.cos(rad)
            y1 = cy + r_in  * math.sin(rad)
            x2 = cx + 130   * math.cos(rad)
            y2 = cy + 130   * math.sin(rad)
            col = self.ACCENT if i % 6 == 0 else self.FG_DIM
            c.create_line(x1, y1, x2, y2, fill=col, width=1, tags="ring")
        # spokes on inner ring
        for i in range(8):
            rad = math.radians(i * 45 + self._angle2 * 0.5)
            x2 = cx + 76 * math.cos(rad)
            y2 = cy + 76 * math.sin(rad)
            c.create_line(cx, cy, x2, y2, fill=self.FG_DIM, width=1, tags="ring")
        # core glow
        glow_r = 28 + 4 * math.sin(math.radians(self._angle * 3))
        c.create_oval(cx-glow_r, cy-glow_r, cx+glow_r, cy+glow_r,
            fill=self.ACCENT2, outline=self.ACCENT, width=2, tags="ring")
        c.create_oval(cx-12, cy-12, cx+12, cy+12,
            fill=self.ACCENT, outline=self.ACCENT3, width=1, tags="ring")
        # HUD corner brackets
        for bx, by, sx, sy in [(40,20,1,1),(560,20,-1,1),(40,400,1,-1),(560,400,-1,-1)]:
            c.create_line(bx,by, bx+sx*30,by, fill=self.ACCENT2, width=1, tags="ring")
            c.create_line(bx,by, bx,by+sy*30, fill=self.ACCENT2, width=1, tags="ring")

    def _draw_scan(self):
        c = self.canvas
        c.delete("scan")
        alpha_line = self.canvas.create_line(
            0, self._scan_y, 600, self._scan_y,
            fill=self.ACCENT3, width=1, tags="scan")
        # faint gradient below scan line (simulate with rectangles)
        for i in range(6):
            y = self._scan_y + i * 3
            if y < 420:
                c.create_rectangle(0, y, 600, y+2,
                    fill=self.ACCENT2, outline="", tags="scan",
                    stipple="gray25" if i > 2 else "gray50")

    def _scan(self):
        if not self._alive:
            return
        self._scan_y = (self._scan_y + 4) % 420
        self._draw_scan()
        self.after(30, self._scan)

    def _spin(self):
        if not self._alive:
            return
        self._angle  = (self._angle  + 4) % 360
        self._angle2 = (self._angle2 + 2) % 360
        self._angle3 = (self._angle3 + 6) % 360
        self._draw_rings()
        # keep labels and progress on top
        self.canvas.tag_raise("title")
        self.canvas.tag_raise("sub")
        self.canvas.tag_raise("progbg")
        self.canvas.tag_raise("prog")
        self.canvas.tag_raise("scan")
        self.canvas.lift(self._status_id)
        self.after(16, self._spin)

    def _advance_text(self):
        if not self._alive:
            return
        if self._line_idx < len(self._BOOT_LINES):
            self.canvas.itemconfig(self._status_id, text=self._BOOT_LINES[self._line_idx])
            frac = self._line_idx / max(len(self._BOOT_LINES) - 1, 1)
            self.canvas.coords(self._prog_bar, 110, 388, 110 + int(380 * frac), 396)
            self._line_idx += 1
            delay = 900 if self._line_idx < len(self._BOOT_LINES) else 400
            self.after(delay, self._advance_text)

    def finish(self):
        self._alive = False
        self.destroy()


class JarvisApp(tk.Tk):
    # ── Tron HUD palette ──────────────────────────────────────────────────────
    BG       = "#020d18"
    PANEL    = "#040f1e"
    BORDER   = "#0a2a45"
    ACCENT   = "#00d4ff"
    ACCENT2  = "#0077b6"
    ACCENT3  = "#48cae4"
    JARVIS_C = "#00d4ff"
    USER_C   = "#90e0ef"
    SYS_C    = "#2a6a8a"
    FG       = "#b8e8f8"
    FG_DIM   = "#1a4a6a"
    INPUT_BG = "#03111f"
    ENTRY_BG = "#03111f"

    def __init__(self):
        super().__init__()
        self.title("J.A.R.V.I.S.")
        self.configure(bg=self.BG)
        self.geometry("980x740")
        self.minsize(720, 520)

        # ── HUD animation ────────────────────────────────────────────────────────
        self._arc_angle  = 0
        self._arc2_angle = 180
        self._arc3_angle = 90
        self._pulse_running = False
        self._pulse_count   = 0
        self._scan_y        = 0
        self._hud_tick      = 0

        # ── Particle system ─────────────────────────────────────────────────────
        self.particles = []
        self.max_particles = 80
        self.speech_active = False
        self.particle_colors = [
            "#00d4ff",  # ACCENT
            "#0077b6",  # ACCENT2
            "#48cae4",  # ACCENT3
            "#90e0ef",  # USER_C
            "#00b4d8",
            "#0096c7",
        ]

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick_clock()
        self._animate_hud()
        self._animate_scan()
        self.after(100, lambda: _set_window_icon(self))
        # Initialize particles after a brief delay to ensure UI is ready
        self.after(200, self._init_particles)

    # ── Particle system methods ─────────────────────────────────────────────────────
    def _init_particles(self):
        """Initialize the particle system with floating particles"""
        import random
        # Wait until window has proper dimensions (not withdrawn/iconic)
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            # Window not ready yet, try again in 50ms
            self.after(50, self._init_particles)
            return

        self.particles = []
        for _ in range(self.max_particles):
            self.particles.append({
                'x': random.randint(0, width),
                'y': random.randint(0, height),
                'vx': random.uniform(-0.5, 0.5),
                'vy': random.uniform(-1, -0.2),  # float upward
                'size': random.uniform(1, 3),
                'life': random.randint(20, 60),
                'max_life': random.randint(20, 60),
                'color': random.choice(self.particle_colors),
                'alpha': 1.0
            })

    def _update_particles(self):
        """Update particle positions and handle speech effects"""
        import random, math
        width = self.winfo_width()
        height = self.winfo_height()

        for p in self.particles:
            # Apply speech effects
            if self.speech_active:
                # When speaking, particles move more energetically
                p['vx'] += random.uniform(-0.5, 0.5) * 0.1
                p['vy'] += random.uniform(-0.3, 0.3) * 0.1
                # Occasionally burst outward from center
                if random.random() < 0.02:
                    angle = random.uniform(0, 2 * math.pi)
                    speed = random.uniform(2, 4)
                    p['vx'] += math.cos(angle) * speed * 0.1
                    p['vy'] += math.sin(angle) * speed * 0.1
            else:
                # Normal gentle float
                p['vx'] *= 0.99  # slight damping
                p['vy'] *= 0.99
                # Add very slight drift
                p['vx'] += random.uniform(-0.02, 0.02)
                p['vy'] += random.uniform(-0.01, 0.01)

            # Update position
            p['x'] += p['vx']
            p['y'] += p['vy']

            # Wrap around edges
            if p['x'] < 0:
                p['x'] = width
            elif p['x'] > width:
                p['x'] = 0
            if p['y'] < 0:
                p['y'] = height
            elif p['y'] > height:
                p['y'] = 0

            # Update life
            p['life'] -= 1
            if p['life'] <= 0:
                # Reset particle
                p['x'] = random.randint(0, width)
                p['y'] = random.randint(0, height)
                p['vx'] = random.uniform(-0.5, 0.5)
                p['vy'] = random.uniform(-1, -0.2)
                p['size'] = random.uniform(1, 3)
                p['life'] = random.randint(20, 60)
                p['max_life'] = p['life']
                p['color'] = random.choice(self.particle_colors)
                p['alpha'] = 1.0

            # Calculate alpha based on life (fade out)
            p['alpha'] = p['life'] / p['max_life']

    def _draw_particles(self):
        """Draw all particles to the particle canvas"""
        if not hasattr(self, 'particle_canvas'):
            return

        self.particle_canvas.delete("particle")

        for p in self.particles:
            if p['alpha'] > 0.1:  # Only draw visible particles
                x, y = int(p['x']), int(p['y'])
                size = p['size']

                # Vary size based on alpha for a twinkling effect
                draw_size = size * (0.5 + p['alpha'] * 0.5)

                self.particle_canvas.create_oval(
                    x - draw_size, y - draw_size,
                    x + draw_size, y + draw_size,
                    fill=p['color'], outline="", tags="particle"
                )

    def _set_speech_active(self, active: bool):
        """Called when speech starts or stops to update particle behavior"""
        self.speech_active = active
        # Optional: trigger a burst when speech starts
        if active and hasattr(self, 'particle_canvas'):
            self._speech_burst()

    def _speech_burst(self):
        """Create a burst of particles when speech starts"""
        import random, math
        width = self.winfo_width()
        height = self.winfo_height()
        center_x, center_y = width // 2, height // 2

        # Add some burst particles
        for _ in range(20):
            angle = random.uniform(0, 2 * math.pi)
            speed = random.uniform(3, 6)
            self.particles.append({
                'x': center_x,
                'y': center_y,
                'vx': math.cos(angle) * speed * 0.1,
                'vy': math.sin(angle) * speed * 0.1,
                'size': random.uniform(2, 4),
                'life': random.randint(15, 30),
                'max_life': random.randint(15, 30),
                'color': random.choice(self.particle_colors),
                'alpha': 1.0
            })
        # Keep particle count at max by removing excess particles (oldest first)
        if len(self.particles) > self.max_particles:
            excess = len(self.particles) - self.max_particles
            self.particles = self.particles[excess:]

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        # ── Top HUD bar ───────────────────────────────────────────────────────
        # ── Top HUD bar (canvas for animation) ───────────────────────────────
        self._topbar_frame = tk.Frame(self, bg=self.PANEL, height=68)
        self._topbar_frame.pack(fill="x", side="top")
        self._topbar_frame.pack_propagate(False)

        self.topbar_canvas = tk.Canvas(self._topbar_frame, bg=self.PANEL,
                                       height=68, highlightthickness=0)
        self.topbar_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.topbar_canvas.bind("<Configure>", self._redraw_topbar)

        # ── Clickable HUD buttons placed over the canvas ──────────────────────
        _btn_bar = tk.Frame(self._topbar_frame, bg=self.PANEL)
        _btn_bar.place(relx=1.0, rely=1.0, anchor="se")

        for label, cmd in [("SETTINGS", self.open_settings),
                            ("STATUS",   self._cmd_status),
                            ("CLEAR",    self._cmd_clear)]:
            btn = tk.Button(
                _btn_bar, text=f"[ {label} ]",
                bg=self.PANEL, fg=self.FG_DIM,
                font=("Courier New", 8), relief="flat",
                cursor="hand2", bd=0, padx=6, pady=5,
                activebackground=self.PANEL, activeforeground=self.ACCENT,
                command=cmd,
            )
            btn.pack(side="right", padx=2)
            btn.bind("<Enter>", lambda e, b=btn: b.config(fg=self.ACCENT))
            btn.bind("<Leave>", lambda e, b=btn: b.config(fg=self.FG_DIM))

        # ── Glowing separator ─────────────────────────────────────────────────
        sep = tk.Canvas(self, bg=self.BG, height=3, highlightthickness=0)
        sep.pack(fill="x")
        self._sep_canvas = sep
        sep.bind("<Configure>", lambda e: self._draw_sep(e.width))

        # ── Left HUD panel (animated ring) ────────────────────────────────────
        body_frame = tk.Frame(self, bg=self.BG)
        body_frame.pack(fill="both", expand=True)

        # ── Particle Canvas (behind everything) ─────────────────────────────────────
        self.particle_canvas = tk.Canvas(body_frame, bg=self.BG, highlightthickness=0)
        self.particle_canvas.place(x=0, y=0, relwidth=1, relheight=1)

        self.left_hud = tk.Canvas(body_frame, width=130, bg=self.BG,
                                  highlightthickness=0)
        self.left_hud.pack(side="left", fill="y", padx=(6, 0))

        # ── Chat area ─────────────────────────────────────────────────────────
        chat_outer = tk.Frame(body_frame, bg=self.BORDER, bd=0)
        chat_outer.pack(side="left", fill="both", expand=True, padx=6, pady=6)

        self.chat = scrolledtext.ScrolledText(
            chat_outer,
            bg=self.BG, fg=self.FG,
            font=("Courier New", 11),
            wrap="word", relief="flat",
            state="disabled",
            padx=18, pady=12,
            selectbackground=self.BORDER,
            cursor="arrow",
            spacing1=2, spacing3=2,
        )
        self.chat.pack(fill="both", expand=True, padx=1, pady=1)
        self.chat.tag_config("jarvis",      foreground=self.ACCENT,  font=("Courier New", 11, "bold"))
        self.chat.tag_config("jarvis_body", foreground=self.FG,       font=("Courier New", 11))
        self.chat.tag_config("user",        foreground=self.USER_C,   font=("Courier New", 11, "bold"))
        self.chat.tag_config("user_body",   foreground=self.FG,       font=("Courier New", 11))
        self.chat.tag_config("system",      foreground=self.SYS_C,    font=("Courier New", 10))
        self.chat.tag_config("time",        foreground=self.FG_DIM,   font=("Courier New", 9))

        # ── Right HUD panel ───────────────────────────────────────────────────
        self.right_hud = tk.Canvas(body_frame, width=130, bg=self.BG,
                                   highlightthickness=0)
        self.right_hud.pack(side="right", fill="y", padx=(0, 6))

        # ── Bottom separator ──────────────────────────────────────────────────
        bot_sep = tk.Canvas(self, bg=self.BG, height=3, highlightthickness=0)
        bot_sep.pack(fill="x")
        bot_sep.bind("<Configure>", lambda e: self._draw_sep(e.width, bot_sep))

        # ── Input bar ─────────────────────────────────────────────────────────
        bottom = tk.Frame(self, bg=self.PANEL, height=58)
        bottom.pack(fill="x", side="bottom")
        bottom.pack_propagate(False)

        self.mic_btn = tk.Button(
            bottom, text="◉", font=("Courier New", 15),
            bg=self.INPUT_BG, fg=self.ACCENT, relief="flat",
            cursor="hand2", padx=8,
            activebackground=self.BORDER, activeforeground=self.ACCENT3,
            command=self._toggle_mic
        )
        self.mic_btn.pack(side="left", padx=(10, 3), pady=9)

        self.mute_btn = tk.Button(
            bottom, text="⏹", font=("Courier New", 15),
            bg=self.INPUT_BG, fg="#ff6b6b", relief="flat",
            cursor="hand2", padx=8,
            activebackground=self.BORDER, activeforeground="#ff6b6b",
            command=self._mute_speech
        )
        self.mute_btn.pack(side="left", padx=(0, 3), pady=9)

        self.input_var = tk.StringVar()
        self.input_box = tk.Entry(
            bottom, textvariable=self.input_var,
            bg=self.INPUT_BG, fg=self.ACCENT3,
            insertbackground=self.ACCENT,
            font=("Courier New", 12), relief="flat",
            highlightthickness=1,
            highlightcolor=self.ACCENT,
            highlightbackground=self.BORDER,
        )
        self.input_box.pack(side="left", fill="x", expand=True, padx=4, pady=9, ipady=7)
        self.input_box.bind("<Return>",   self._on_send)
        self.bind("<Escape>", lambda e: self._mute_speech())
        self.input_box.bind("<FocusIn>",  lambda e: self.input_box.config(highlightbackground=self.ACCENT))
        self.input_box.bind("<FocusOut>", lambda e: self.input_box.config(highlightbackground=self.BORDER))

        self.send_btn = tk.Button(
            bottom, text="EXECUTE ▶",
            bg=self.ACCENT2, fg=self.FG,
            font=("Courier New", 10, "bold"),
            relief="flat", padx=14, pady=7,
            cursor="hand2",
            activebackground=self.ACCENT, activeforeground=self.BG,
            command=self._on_send
        )
        self.send_btn.pack(side="right", padx=10, pady=9)

        # ── Status bar ────────────────────────────────────────────────────────
        status_bar = tk.Frame(self, bg=self.BG, height=20)
        status_bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="● STANDBY")
        tk.Label(status_bar, textvariable=self.status_var,
                 bg=self.BG, fg=self.ACCENT, font=("Courier New", 8),
                 anchor="w").pack(side="left", padx=10)
        wake = CFG.get("wake_word", "jarvis").upper()
        tk.Label(status_bar,
                 text=f'SAY "{wake}" TO ACTIVATE VOICE  //  FAILSAFE: MOUSE TOP-LEFT',
                 bg=self.BG, fg=self.FG_DIM, font=("Courier New", 8)
                 ).pack(side="right", padx=10)

    # ── Drawing helpers ───────────────────────────────────────────────────────
    def _draw_sep(self, w, canvas=None):
        c = canvas or self._sep_canvas
        c.delete("all")
        c.create_line(0, 1, w, 1, fill=self.ACCENT2,  width=1)
        c.create_line(0, 2, w, 2, fill=self.ACCENT,   width=1)
        c.create_line(0, 3, w, 3, fill=self.ACCENT2,  width=1)

    def _redraw_topbar(self, event=None):
        self._draw_topbar()

    def _draw_topbar(self):
        c = self.topbar_canvas
        c.delete("all")
        w = c.winfo_width() or 980
        h = 68

        # background gradient effect (horizontal bands)
        for i in range(h):
            shade = int(4 + (i / h) * 8)
            col = f"#{shade:02x}{shade+4:02x}{shade+8:02x}"
            c.create_line(0, i, w, i, fill=col)

        # Outer border lines
        c.create_line(0, 0, w, 0, fill=self.BORDER, width=1)
        c.create_line(0, h-1, w, h-1, fill=self.ACCENT2, width=1)

        # Animated ring logo (left side)
        cx, cy, R = 34, 34, 26
        # outer ring
        c.create_oval(cx-R, cy-R, cx+R, cy+R, outline=self.FG_DIM, width=1)
        # spinning arc
        c.create_arc(cx-R, cy-R, cx+R, cy+R,
                     start=self._arc_angle, extent=230,
                     outline=self.ACCENT, width=3, style="arc")
        c.create_arc(cx-R, cy-R, cx+R, cy+R,
                     start=self._arc_angle+250, extent=60,
                     outline=self.ACCENT3, width=1, style="arc")
        # inner ring
        r2 = 16
        c.create_oval(cx-r2, cy-r2, cx+r2, cy+r2, outline=self.ACCENT2, width=1)
        c.create_arc(cx-r2, cy-r2, cx+r2, cy+r2,
                     start=-self._arc2_angle, extent=180,
                     outline=self.ACCENT3, width=1, style="arc")
        # core
        r3 = 7 + (2 if self._pulse_running and self._pulse_count % 2 == 0 else 0)
        c.create_oval(cx-r3, cy-r3, cx+r3, cy+r3,
                      fill=self.ACCENT, outline=self.ACCENT3, width=1)
        # tick marks
        for i in range(12):
            rad = math.radians(i * 30 + self._arc_angle * 0.4)
            r_in = R - 3 if i % 3 == 0 else R - 1
            c.create_line(cx + r_in*math.cos(rad), cy + r_in*math.sin(rad),
                          cx + R   *math.cos(rad), cy + R   *math.sin(rad),
                          fill=self.ACCENT if i % 3 == 0 else self.FG_DIM, width=1)

        # Title text
        c.create_text(76, 22, text="J.A.R.V.I.S.", anchor="w",
                      font=("Courier New", 18, "bold"), fill=self.ACCENT)
        owner = CFG.get("owner_name", "")
        sub = f"ONLINE  ·  {owner.upper()}" if owner else "ONLINE"
        c.create_text(76, 44, text=sub, anchor="w",
                      font=("Courier New", 8), fill=self.FG_DIM)

        # Clock (right side)
        now = datetime.now()
        c.create_text(w - 16, 22, text=now.strftime("%H:%M:%S"), anchor="e",
                      font=("Courier New", 14, "bold"), fill=self.ACCENT)
        c.create_text(w - 16, 42, text=now.strftime("%d %b %Y"), anchor="e",
                      font=("Courier New", 9), fill=self.FG_DIM)

        # Scanning line across topbar
        scan_y = int(self._scan_y * (h / 740))
        if scan_y < h:
            c.create_line(0, scan_y, w, scan_y, fill=self.ACCENT3,
                          width=1, stipple="gray50")

        # Corner HUD brackets
        blen = 18
        for bx, by, sx, sy in [(0,0,1,1),(w-1,0,-1,1),(0,h-1,1,-1),(w-1,h-1,-1,-1)]:
            c.create_line(bx, by, bx+sx*blen, by,       fill=self.ACCENT, width=2)
            c.create_line(bx, by, bx,         by+sy*blen, fill=self.ACCENT, width=2)

    def _draw_left_hud(self):
        c = self.left_hud
        c.delete("all")
        h = c.winfo_height() or 600
        cx, cy, R = 65, 110, 52

        # Outer rings
        c.create_oval(cx-R, cy-R, cx+R, cy+R, outline=self.FG_DIM, width=1)
        c.create_arc(cx-R, cy-R, cx+R, cy+R,
                     start=self._arc_angle, extent=200,
                     outline=self.ACCENT, width=2, style="arc")
        c.create_arc(cx-R, cy-R, cx+R, cy+R,
                     start=self._arc_angle+220, extent=80,
                     outline=self.ACCENT3, width=1, style="arc")
        r2 = 34
        c.create_oval(cx-r2, cy-r2, cx+r2, cy+r2, outline=self.ACCENT2, width=1)
        c.create_arc(cx-r2, cy-r2, cx+r2, cy+r2,
                     start=-self._arc3_angle, extent=150,
                     outline=self.ACCENT3, width=1, style="arc")
        # tick marks
        for i in range(16):
            rad = math.radians(i * 22.5 + self._arc_angle * 0.3)
            r_in = R - 5 if i % 4 == 0 else R - 2
            c.create_line(cx + r_in*math.cos(rad), cy + r_in*math.sin(rad),
                          cx + R  *math.cos(rad),  cy + R  *math.sin(rad),
                          fill=self.ACCENT if i % 4 == 0 else self.FG_DIM, width=1)
        # spokes
        for i in range(6):
            rad = math.radians(i * 60 + self._arc2_angle * 0.4)
            c.create_line(cx, cy, cx+28*math.cos(rad), cy+28*math.sin(rad),
                          fill=self.BORDER, width=1)
        # core
        glow = 10 + 3 * abs(math.sin(math.radians(self._hud_tick * 3)))
        c.create_oval(cx-glow, cy-glow, cx+glow, cy+glow,
                      fill=self.ACCENT2, outline=self.ACCENT, width=1)
        c.create_oval(cx-5, cy-5, cx+5, cy+5, fill=self.ACCENT, outline="")

        # HUD data readouts below the ring
        y0 = cy + R + 18
        try:
            cpu = psutil.cpu_percent()
            ram = psutil.virtual_memory().percent
        except Exception:
            cpu, ram = 0.0, 0.0
        # CPU bar
        c.create_text(10, y0, text="CPU", anchor="w",
                      font=("Courier New", 7), fill=self.FG_DIM)
        c.create_text(120, y0, text=f"{cpu:.0f}%", anchor="e",
                      font=("Courier New", 7), fill=self.ACCENT)
        c.create_rectangle(10, y0+10, 120, y0+16, fill=self.BORDER, outline="")
        c.create_rectangle(10, y0+10, 10+int(110*(cpu/100)), y0+16,
                           fill=self.ACCENT, outline="")
        # RAM bar
        y1 = y0 + 28
        c.create_text(10, y1, text="RAM", anchor="w",
                      font=("Courier New", 7), fill=self.FG_DIM)
        c.create_text(120, y1, text=f"{ram:.0f}%", anchor="e",
                      font=("Courier New", 7), fill=self.ACCENT3)
        c.create_rectangle(10, y1+10, 120, y1+16, fill=self.BORDER, outline="")
        c.create_rectangle(10, y1+10, 10+int(110*(ram/100)), y1+16,
                           fill=self.ACCENT3, outline="")

        # vertical left-border line
        c.create_line(128, 0, 128, h, fill=self.BORDER, width=1)

        # Scan line
        scan_frac = (self._scan_y % h)
        c.create_line(0, scan_frac, 130, scan_frac, fill=self.ACCENT2,
                      width=1, stipple="gray50")

    def _draw_right_hud(self):
        c = self.right_hud
        c.delete("all")
        h = c.winfo_height() or 600
        cx, cy, R = 65, 110, 52

        # Outer ring (counter-rotating)
        c.create_oval(cx-R, cy-R, cx+R, cy+R, outline=self.FG_DIM, width=1)
        c.create_arc(cx-R, cy-R, cx+R, cy+R,
                     start=-self._arc_angle, extent=200,
                     outline=self.ACCENT3, width=2, style="arc")
        c.create_arc(cx-R, cy-R, cx+R, cy+R,
                     start=-self._arc_angle+220, extent=80,
                     outline=self.ACCENT2, width=1, style="arc")
        r2 = 34
        c.create_oval(cx-r2, cy-r2, cx+r2, cy+r2, outline=self.ACCENT2, width=1)
        c.create_arc(cx-r2, cy-r2, cx+r2, cy+r2,
                     start=self._arc3_angle, extent=150,
                     outline=self.ACCENT, width=1, style="arc")
        for i in range(16):
            rad = math.radians(i * 22.5 - self._arc_angle * 0.3)
            r_in = R - 5 if i % 4 == 0 else R - 2
            c.create_line(cx + r_in*math.cos(rad), cy + r_in*math.sin(rad),
                          cx + R  *math.cos(rad),  cy + R  *math.sin(rad),
                          fill=self.ACCENT3 if i % 4 == 0 else self.FG_DIM, width=1)
        for i in range(6):
            rad = math.radians(i * 60 - self._arc2_angle * 0.4)
            c.create_line(cx, cy, cx+28*math.cos(rad), cy+28*math.sin(rad),
                          fill=self.BORDER, width=1)
        glow = 10 + 3 * abs(math.cos(math.radians(self._hud_tick * 3)))
        c.create_oval(cx-glow, cy-glow, cx+glow, cy+glow,
                      fill=self.ACCENT2, outline=self.ACCENT3, width=1)
        c.create_oval(cx-5, cy-5, cx+5, cy+5, fill=self.ACCENT3, outline="")

        # Clock
        y0 = cy + R + 18
        now = datetime.now()
        c.create_text(65, y0, text=now.strftime("%H:%M"),
                      font=("Courier New", 13, "bold"), fill=self.ACCENT, anchor="center")
        c.create_text(65, y0+20, text=now.strftime("%S"),
                      font=("Courier New", 9), fill=self.FG_DIM, anchor="center")
        c.create_text(65, y0+36, text=now.strftime("%d %b"),
                      font=("Courier New", 7), fill=self.FG_DIM, anchor="center")

        # vertical right-border line
        c.create_line(2, 0, 2, h, fill=self.BORDER, width=1)

        # Scan line
        scan_frac = (self._scan_y % h)
        c.create_line(0, scan_frac, 130, scan_frac, fill=self.ACCENT2,
                      width=1, stipple="gray50")

    # ── Animation loops ───────────────────────────────────────────────────────
    def _animate_hud(self):
        self._arc_angle  = (self._arc_angle  + 2) % 360
        self._arc2_angle = (self._arc2_angle + 1) % 360
        self._arc3_angle = (self._arc3_angle + 3) % 360
        self._hud_tick  += 1
        if self._pulse_running:
            self._pulse_count += 1
            if self._pulse_count > 12:
                self._pulse_running = False
                self._pulse_count   = 0

        # Update and draw particles
        self._update_particles()
        self._draw_particles()

        self._draw_topbar()
        self._draw_left_hud()
        self._draw_right_hud()
        self.after(33, self._animate_hud)   # ~30 fps

    def _animate_scan(self):
        self._scan_y = (self._scan_y + 3) % max(self.winfo_height(), 600)
        self.after(20, self._animate_scan)

    def _tick_clock(self):
        # Clock is now drawn inside _draw_topbar / _draw_right_hud
        self.after(1000, self._tick_clock)

    # ── Wake flash ────────────────────────────────────────────────────────────
    def flash_wake(self):
        self._pulse_running = True
        self._pulse_count   = 0

    # ── Message display ───────────────────────────────────────────────────────
    def add_message(self, sender: str, text: str, tag: str = "system"):
        self.chat.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        if tag == "jarvis":
            self.chat.insert("end", f"\n  ◈ J.A.R.V.I.S.  ", "jarvis")
            self.chat.insert("end", f"[{ts}]\n", "time")
            self.chat.insert("end", f"  {text}\n", "jarvis_body")
        elif tag == "user":
            self.chat.insert("end", f"\n  ▸ {sender.upper()}  ", "user")
            self.chat.insert("end", f"[{ts}]\n", "time")
            self.chat.insert("end", f"  {text}\n", "user_body")
        else:
            self.chat.insert("end", f"\n  // {text}\n", "system")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def set_status(self, msg: str):
        icons = {"Thinking": "◌", "Running": "◉", "Ready": "●", "Listening": "◎"}
        prefix = next((v for k, v in icons.items() if k in msg), "●")
        self.status_var.set(f"{prefix} {msg.upper()}")
        self.update_idletasks()

    # ── Event handlers ────────────────────────────────────────────────────────
    def _on_send(self, event=None):
        text = self.input_var.get().strip()
        if not text:
            return
        self.input_var.set("")
        self.add_message("You", text, tag="user")
        threading.Thread(target=handle_command, args=(text,),
                         daemon=True, name="cmd-gui").start()

    def _toggle_mic(self):
        global listening
        listening = not listening
        self.mic_btn.config(fg=self.ACCENT if listening else "#ff6b6b")
        self.set_status(f"Wake word: {'active' if listening else 'muted'}")

    def _toggle_mute_speech(self):
        global _speech_muted
        if _speech_muted:
            _speech_muted = False
            _tts_stop_event.clear()
            self.mute_btn.config(fg="#ff6b6b", text="⏹")
            self.set_status("Speech unmuted")
        else:
            _speech_muted = True
            interrupt_speech()
            self.mute_btn.config(fg=self.ACCENT, text="▶")
            self.set_status("Speech muted — click ▶ to unmute")

    def _mute_speech(self):
        self._toggle_mute_speech()

    def _cmd_status(self):
        threading.Thread(target=lambda: handle_command("status"),
                         daemon=True, name="cmd-status").start()

    def _cmd_clear(self):
        chat_history.clear()
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self.add_message("JARVIS", "Done. Fresh start.", tag="jarvis")

    def open_settings(self):
        SettingsDialog(self)

    def _on_close(self):
        self.withdraw()
        desktop_notify("JARVIS — Running in background",
                       "J.A.R.V.I.S. is still active. Use the tray icon to restore or quit.")



def gui_setup_wizard():
    root = tk.Tk()
    root.withdraw()

    messagebox.showinfo(
        "JARVIS — First Run",
        "Welcome to JARVIS!\n\nLet's do a quick setup.\n\n"
        "Make sure Ollama is running before starting.\n"
        "(Run: ollama serve  and  ollama pull llama3.2)"
    )

    name = simpledialog.askstring("Your name", "Enter your first name:", initialvalue=os.getlogin().title())
    if not name:
        name = os.getlogin().title()

    mics = _list_microphones()

    mic_idx = 0
    if mics:
        mic_names = [f"[{i}] {n}" for i, n in mics]
        mic_win = tk.Toplevel()
        mic_win.title("Select Microphone")
        mic_win.configure(bg="#0d1117")
        mic_win.geometry("400x300")
        tk.Label(mic_win, text="Choose your microphone:", bg="#0d1117", fg="#e6edf3",
                 font=("Segoe UI", 11)).pack(pady=12)
        lb = tk.Listbox(mic_win, bg="#161b22", fg="#e6edf3", font=("Segoe UI", 10),
                        selectbackground="#58a6ff", relief="flat", height=8)
        for m in mic_names:
            lb.insert("end", m)
        lb.select_set(0)
        lb.pack(fill="both", expand=True, padx=16)
        chosen = [0]
        def confirm_mic():
            sel = lb.curselection()
            if sel:
                chosen[0] = mics[sel[0]][0]
            mic_win.destroy()
        tk.Button(mic_win, text="Confirm", bg="#58a6ff", fg="#0d1117",
                  font=("Segoe UI", 10, "bold"), relief="flat", padx=16, pady=6,
                  command=confirm_mic).pack(pady=12)
        mic_win.grab_set()
        mic_win.wait_window()
        mic_idx = chosen[0]

    cfg = dict(DEFAULT_CONFIG)
    cfg["owner_name"] = name
    cfg["mic_index"]  = mic_idx
    save_config(cfg)
    root.destroy()
    return cfg



_STARTUP_REG_KEY  = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_REG_NAME = "JARVIS_Vanitas"


def _get_vbs_launcher_path() -> Path:
    return Path(__file__).parent / "start_jarvis.vbs"


def register_startup() -> str:
    import winreg
    vbs = _get_vbs_launcher_path()
    if not vbs.exists():
        vbs.write_text(
            'Dim s: Set s = CreateObject("WScript.Shell")\r\n'
            f's.Run "pythonw """ & Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName,"\\")) & "jarvis_gui.py""", 0, False\r\n',
            encoding="utf-8"
        )
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _STARTUP_REG_NAME, 0, winreg.REG_SZ,
                              f'wscript.exe "{vbs}"')
        return "✅ JARVIS will now start automatically at login."
    except Exception as e:
        return f"Could not register startup: {e}"


def unregister_startup() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _STARTUP_REG_NAME)
        return "✅ JARVIS removed from startup."
    except FileNotFoundError:
        return "JARVIS was not registered for startup."
    except Exception as e:
        return f"Error: {e}"


def is_startup_registered() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY) as key:
            winreg.QueryValueEx(key, _STARTUP_REG_NAME)
            return True
    except Exception:
        return False



def main():
    global CFG, HOME_DIR, gui_app

    CFG = load_config()
    if not CFG.get("owner_name"):
        CFG = gui_setup_wizard()
        CFG = load_config()

    HOME_DIR = Path.home()

    _ollama_running = False
    for _ollama_addr in ("http://localhost:11434", "http://127.0.0.1:11434"):
        try:
            _r = requests.get(_ollama_addr, timeout=5)
            _ollama_running = True
            CFG["ollama_url"] = _ollama_addr + "/api/chat"
            break
        except requests.exceptions.ConnectionError:
            continue
        except Exception:
            _ollama_running = True
            CFG["ollama_url"] = _ollama_addr + "/api/chat"
            break

    if not _ollama_running:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror(
            "JARVIS — Ollama Not Found",
            "Ollama is not running!\n\n"
            "Please start it by running:\n"
            "  ollama serve\n\n"
            "And make sure you've pulled the model:\n"
            "  ollama pull llama3.2"
        )
        root.destroy()
        sys.exit(1)

    init_tts()

    if not is_startup_registered():
        register_startup()

    gui_app = JarvisApp()
    gui_app.withdraw()

    splash = SplashScreen(gui_app)
    splash.update()

    threading.Thread(target=background_monitor, daemon=True).start()
    threading.Thread(target=wake_word_loop,      daemon=True).start()
    threading.Thread(target=run_tray,            daemon=True).start()

    total_splash_ms = len(SplashScreen._BOOT_LINES) * 900 + 600

    def _show_main():
        splash.finish()
        gui_app.after(350, _reveal)

    def _reveal():
        gui_app.deiconify()
        gui_app.lift()
        gui_app.focus_force()
        hour   = datetime.now().hour
        period = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
        owner  = CFG.get("owner_name", "")
        name   = owner if owner else "sir"
        gui_app.after(400, lambda: threading.Thread(
            target=lambda: speak(f"Good {period}, {name}. Everything's up and running."),
            daemon=True, name="speak-greeting"
        ).start())

    gui_app.after(total_splash_ms, _show_main)
    gui_app.mainloop()


if __name__ == "__main__":
    main()
