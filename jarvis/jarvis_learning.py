from __future__ import annotations
 
import json, re, difflib
from pathlib import Path
from datetime import datetime
 
# ── Storage ──────────────────────────────────────────────────────────────────
LEARNED_FILE = Path(__file__).parent / "jarvis_learned.json"
 
 
def _load() -> list[dict]:
    if LEARNED_FILE.exists():
        try:
            return json.loads(LEARNED_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []
 
 
def _save(entries: list[dict]):
    LEARNED_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
 
 
# ── Lookup ────────────────────────────────────────────────────────────────────
def lookup_learned(text: str, cutoff: float = 0.78) -> dict | None:
    """
    Return the best matching learned entry for `text`, or None.
    Tries exact match first, then fuzzy match against stored triggers.
    """
    entries = _load()
    if not entries:
        return None
 
    t = text.strip().lower()
 
    # 1. Exact match
    for e in entries:
        if t == e["trigger"].lower():
            return e
        # Also check aliases saved alongside the entry
        for alias in e.get("aliases", []):
            if t == alias.lower():
                return e
 
    # 2. Fuzzy match over all triggers + aliases
    corpus: list[tuple[str, dict]] = []
    for e in entries:
        corpus.append((e["trigger"].lower(), e))
        for alias in e.get("aliases", []):
            corpus.append((alias.lower(), e))
 
    triggers_only = [c[0] for c in corpus]
    matches = difflib.get_close_matches(t, triggers_only, n=1, cutoff=cutoff)
    if matches:
        matched_trigger = matches[0]
        for trigger, entry in corpus:
            if trigger == matched_trigger:
                return entry
 
    return None
 
 
# ── Teaching flow ─────────────────────────────────────────────────────────────
def teach_jarvis(trigger: str, response_text: str, action_json: dict | None = None) -> str:
    """
    Save a new learned entry.
    `trigger`       — the phrase that will match this entry
    `response_text` — what JARVIS says / displays
    `action_json`   — optional action dict (e.g. {"action": "open_link", "url": "..."})
    """
    entries = _load()
 
    # Overwrite if trigger already exists
    existing_idx = next(
        (i for i, e in enumerate(entries) if e["trigger"].lower() == trigger.strip().lower()),
        None
    )
    entry = {
        "trigger":   trigger.strip().lower(),
        "response":  response_text.strip(),
        "action":    action_json,
        "aliases":   [],
        "learned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "hits":       0,
    }
    if existing_idx is not None:
        entry["aliases"]   = entries[existing_idx].get("aliases", [])
        entry["hits"]      = entries[existing_idx].get("hits", 0)
        entries[existing_idx] = entry
    else:
        entries.append(entry)
 
    _save(entries)
    return f"✅ Learned: when you say \"{trigger}\", I'll know what to do."
 
 
def add_alias(trigger: str, alias: str) -> str:
    """Add an alternate phrasing for an existing learned entry."""
    entries = _load()
    for e in entries:
        if e["trigger"].lower() == trigger.strip().lower():
            if alias.lower() not in [a.lower() for a in e.get("aliases", [])]:
                e.setdefault("aliases", []).append(alias.strip().lower())
            _save(entries)
            return f"✅ Added alias \"{alias}\" for \"{trigger}\"."
    return f"No learned entry found for \"{trigger}\"."
 
 
def forget_trigger(trigger: str) -> str:
    """Remove a learned entry."""
    entries = _load()
    before  = len(entries)
    entries = [e for e in entries if e["trigger"].lower() != trigger.strip().lower()]
    if len(entries) == before:
        return f"No learned entry found for \"{trigger}\"."
    _save(entries)
    return f"✅ Forgotten: \"{trigger}\"."
 
 
def list_learned() -> str:
    entries = _load()
    if not entries:
        return "Nothing learned yet, sir. Try saying 'teach me to do X'."
    lines = [f"📚 Learned commands ({len(entries)} total):"]
    for e in entries:
        action_tag = f"  [action: {e['action']['action']}]" if e.get("action") else ""
        lines.append(f"  • \"{e['trigger']}\"{action_tag}")
        if e.get("aliases"):
            lines.append(f"      aliases: {', '.join(e['aliases'])}")
    return "\n".join(lines)
 
 
def increment_hits(trigger: str):
    """Track how many times a learned entry has been used."""
    entries = _load()
    for e in entries:
        if e["trigger"].lower() == trigger.strip().lower():
            e["hits"] = e.get("hits", 0) + 1
            _save(entries)
            return
 
 
# ── Interactive teaching dialog ────────────────────────────────────────────────
def interactive_teach(
    original_command: str,
    listen_fn,          # listen_for_command() from main script
    speak_fn,           # speak() from main script
    gui_app=None,
) -> str | None:
    """
    Called after a failed command to ask the user how to handle it in future.
    Returns a spoken reply string, or None if the user declined to teach.
 
    listen_fn  : callable() -> str | None   (voice input)
    speak_fn   : callable(str)              (TTS output)
    """
    speak_fn(
        f"I didn't have a handler for that one. "
        f"Want to teach me what to do when you say \"{original_command}\"? "
        "Say YES to teach me, or NO to skip."
    )
 
    answer = listen_fn("YES to teach, NO to skip…")
    if not answer:
        return None
 
    YES = {"yes", "yeah", "yep", "sure", "go ahead", "teach", "ok", "okay"}
    NO  = {"no", "nope", "nah", "skip", "forget it", "cancel"}
 
    if any(w in answer.lower() for w in NO):
        return None
    if not any(w in answer.lower() for w in YES):
        return None   # ambiguous — don't bother
 
    # Ask what JARVIS should say/do
    speak_fn(
        "Got it. What should I say or do next time? "
        "You can say something like 'open Spotify' or just give me a reply to speak."
    )
    instruction = listen_fn("Tell me what to do…")
    if not instruction:
        speak_fn("Didn't catch that. We can try again later.")
        return None
 
    instruction = instruction.strip()
 
    # Try to detect if instruction is an action phrase we can parse into JSON
    action_json = _parse_instruction_to_action(instruction)
 
    response_text = (
        f"Got it. I'll {instruction}."
        if action_json
        else instruction
    )
 
    result = teach_jarvis(original_command, response_text, action_json)
    speak_fn(f"Done. {result}")
    return result
 
 
def _parse_instruction_to_action(instruction: str) -> dict | None:
    """
    Heuristically convert a spoken instruction into a JARVIS action dict.
    Covers the most common cases. For anything else, store as a plain spoken response.
    """
    t = instruction.strip().lower()
 
    # open / launch <app>
    m = re.match(r'(?:open|launch|start)\s+(.+)', t)
    if m:
        name = m.group(1).strip()
        # Looks like a URL?
        if re.search(r'\.\w{2,}', name) or name.startswith("http"):
            url = name if name.startswith("http") else "https://" + name
            return {"action": "open_link", "url": url}
        return {"action": "open_app", "name": name}
 
    # go to / navigate to <url>
    m = re.match(r'(?:go to|navigate to|browse to)\s+(https?://\S+|[\w\-]+\.\w{2,}\S*)', t)
    if m:
        url = m.group(1)
        if not url.startswith("http"):
            url = "https://" + url
        return {"action": "open_link", "url": url}
 
    # search for <query>
    m = re.match(r'search\s+(?:for\s+)?(.+)', t)
    if m:
        return {"action": "web_search_read", "query": m.group(1).strip()}
 
    # play / focus <app>
    m = re.match(r'(?:play|focus|switch to|bring up)\s+(.+)', t)
    if m:
        return {"action": "focus_app", "name": m.group(1).strip()}
 
    # run script <path>
    m = re.match(r'run\s+(?:script\s+)?(.+\.(?:py|bat|sh|ps1|cmd))', t)
    if m:
        return {"action": "run_script", "path": m.group(1).strip()}
 
    # set volume
    m = re.match(r'set\s+volume\s+(?:to\s+)?(\d+)', t)
    if m:
        return {"action": "set_volume", "level": int(m.group(1))}
 
    # weather in <location>
    m = re.match(r'(?:check\s+)?weather\s+(?:in\s+)?(.+)', t)
    if m:
        return {"action": "weather", "location": m.group(1).strip()}
 
    # screenshot
    if re.search(r'screenshot|capture\s+screen', t):
        return {"action": "screenshot", "path": ""}
 
    # mute
    if re.search(r'^mute$', t):
        return {"action": "mute"}
 
    return None   # store as plain spoken response
 