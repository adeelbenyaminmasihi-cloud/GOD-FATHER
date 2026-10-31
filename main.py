#!/usr/bin/env python3
"""
GenZ Roast Video Bot  v2
(Gemini script -> clean Urdu voiceover -> fast-cut Giphy visuals -> Telegram)

What's new in v2
  * Gemini: smarter model cascade with instant fail-over when a model is busy (503),
    several rounds with waiting, and gemini-3.1-flash-lite as the stable safety net.
  * Urdu audio: better script rules (Urdu punctuation, numbers as words), text clean-up
    before speech, silence trimming, voice EQ + compression, final loudness normalization.
  * Faster pace: more and shorter segments, no dead air between lines, GIF changes
    every ~2 seconds, flash + camera-shake on every cut, whoosh sound on cuts.
  * 3 videos per day is configured in the workflow file (roast.yml).

Required environment variables (GitHub repository secrets):
  GEMINI_API_KEY, GIPHY_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Optional environment variables:
  TTS_VOICE, TTS_RATE, TTS_PITCH, WATERMARK
"""

import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import time

import requests

# =============================================================================
# SETTINGS  (safe to edit)
# =============================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GIPHY_API_KEY = os.environ.get("GIPHY_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Voices: "ur-PK-AsadNeural" (male) or "ur-PK-UzmaNeural" (female)
TTS_VOICE = os.environ.get("TTS_VOICE", "ur-PK-AsadNeural").strip()
TTS_RATE = os.environ.get("TTS_RATE", "+12%").strip()   # speaking speed: "+0%" normal, "+20%" very fast
TTS_PITCH = os.environ.get("TTS_PITCH", "+0Hz").strip()  # natural pitch sounds best
TTS_FALLBACK_VOICES = ["ur-PK-UzmaNeural", "ur-IN-SalmanNeural", "ur-IN-GulNeural"]

# Optional small text at the top of the video, e.g. "@MyChannel". Empty = off.
WATERMARK = os.environ.get("WATERMARK", "").strip()

# Tried in order. If a model is busy (503) we jump to the next one immediately.
# (model name, thinking settings)
GEMINI_MODELS = [
    ("gemini-3.8-flash", {"thinkingLevel": "low"}),  # best quality, but often busy
    ("gemini-3.1-flash-lite", None),                 # stable (GA) safety net
    ("gemini-3.6-flash", None),
    ("gemini-3.5-flash", None),
    ("gemini-3.5-flash-lite", None),
]
GEMINI_ROUNDS = 3                 # go through the whole list up to 3 times
GEMINI_ROUND_WAIT = 30            # seconds to wait before round 2 (60s before round 3)
GEMINI_ATTEMPTS_PER_MODEL = 2     # only used for bad/garbled answers and connection errors
GEMINI_CONNECT_TIMEOUT = 15
GEMINI_READ_TIMEOUT = 150

HISTORY_FILE = "topics_history.json"
HISTORY_KEEP = 400
PROMPT_HISTORY = 80
WORK_DIR = "build_temp"
FINAL_VIDEO = "final_roast.mp4"

VIDEO_W, VIDEO_H, VIDEO_FPS = 1080, 1920, 30
TELEGRAM_MAX_MB = 48  # Telegram bots can upload up to 50 MB
MAX_SEGMENTS = 20
TAIL_PAD = 0.06       # seconds of silence after each line (smaller = faster pace)
LOUDNESS_TARGET = -14  # LUFS, the usual level for Shorts/Reels/TikTok

# Each video gets a random focus area so the 3 daily videos feel different.
FOCUS_AREAS = [
    "Instagram Reels, scrolling addiction and fake online life",
    "situationships, texting games and relationship delusions",
    "university and student life (attendance, assignments, last-night exam panic)",
    "gaming, PUBG/Free Fire behavior and online toxicity",
    "jobs, freelancing, side-hustle gurus and money delusions",
    "gym, fitness and self-improvement delusions",
    "AI, gadgets, apps and tech addiction",
    "desi parents vs Gen-Z (rishta aunty, 'log kya kahenge', phone time)",
    "friend-group drama, group chats and plans that never happen",
    "late-night overthinking, sleep schedule and 'I will start tomorrow' energy",
    "online shopping, food delivery and wallet destruction",
    "trending memes, viral challenges and cringe content creators",
]

ALLOWED_SFX = ("vine_boom", "beep", "horn", "whoosh")
CAPTION_COLORS = ["yellow", "white", "0x00E5FF", "0xFF5252"]
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]

FFMPEG = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]


# =============================================================================
# SMALL HELPERS
# =============================================================================
class FatalError(Exception):
    """Something that retrying will not fix (bad key, missing tool, ...)."""


def log(msg=""):
    print(msg, flush=True)


def work(name):
    return os.path.join(WORK_DIR, name)


def run_cmd(cmd, what):
    """Run a command; on failure show the real error (the old script hid it)."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-25:])
        raise RuntimeError(f"{what} failed (exit {proc.returncode}):\n{tail}")
    return proc


def media_duration(path):
    proc = run_cmd(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        f"ffprobe duration of {path}",
    )
    return float(proc.stdout.strip())


def video_size(path):
    proc = run_cmd(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        f"ffprobe size of {path}",
    )
    w, h = proc.stdout.strip().split(",")[:2]
    return int(w), int(h)


def is_valid_video(path):
    """True if the file is a real, playable video with a video stream."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 2000:
            return False
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip()) and media_duration(path) > 0.05
    except Exception:
        return False


def find_font():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def normalize_topic(t):
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


# =============================================================================
# HISTORY (topic memory)
# =============================================================================
def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x) for x in data if str(x).strip()]
    except Exception as e:
        log(f"⚠️ Could not read {HISTORY_FILE} ({e}). Starting with empty memory.")
    return []


def save_history(history, new_topic):
    history = list(history) + [new_topic]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-HISTORY_KEEP:], f, ensure_ascii=False, indent=2)


# =============================================================================
# STEP 1: GEMINI SCRIPT
# =============================================================================
PROMPT_TEMPLATE = """
You are an ultra-witty, high-energy Pakistani/Desi roast creator with the comedic timing, sarcasm and savage energy of CarryMinati.
Your audience is Gen-Z. Pick a brand new, hyper-relatable, funny Gen-Z problem, current internet trend, viral online behavior, or modern delusion.
Focus area for THIS video (pick a fresh, specific angle inside it): __FOCUS__

CRITICAL RULE - DO NOT REPEAT ANY OF THESE PAST TOPICS:
[__PAST_TOPICS__]

Create a VERY fast-paced 40 to 55 second roast video script divided into 12 to 16 quick, punchy segments (rapid-fire like a YouTube Shorts edit, no filler, no slow build-up).
Rules for the script:
1. Segment 1 MUST be a brutal 3-second attention-grabber hook that stops the scroll immediately.
2. "text_urdu" is read aloud by an Urdu Text-to-Speech engine, so write it perfectly for speech:
   - ONLY Urdu script (اردو رسم الخط). NO English letters, NO digits, NO emojis, NO hashtags, NO symbols.
   - Write English Gen-Z words phonetically in Urdu script (e.g., سچویشن شپ, ریلز, کرنج, برو, جم بوائز, ڈپریشن, وائب, انسٹاگرام).
   - Write every number in Urdu words (e.g., تین بجے, پچاس فیصد).
   - Use proper Urdu punctuation for natural pauses and emphasis: ، for short pauses, ۔ at the end of sentences, ؟ for questions, ! for punches.
   - Natural spoken Urdu like a YouTuber talking to a friend, NOT formal bookish Urdu. Short sharp sentences.
   - Each segment must be 5 to 11 words, so it takes only about 2 to 4 seconds to speak.
   - Tone: sarcastic, energetic, hilarious. No abusive/NSFW curses, just savage psychological roasting.
3. "overlay_text" MUST be short (2 to 4 words max) in ALL CAPS English/Roman Urdu, flashing on screen like a meme caption (e.g., "GOLD MEDAL SCROLLING", "BRO THOUGHT HE ATE", "9-TO-5 SLAVERY"). Use only letters, numbers and basic punctuation, no emojis.
4. "giphy_query" MUST be a specific 2-4 word English search term for Giphy that fetches a hilarious reaction GIF matching that line (e.g., "shocked guy meme", "crying cat meme", "disappointed cricket fan", "typing fast meme", "clown makeup meme"). Use a DIFFERENT query for every segment.
5. "sfx" MUST be one of: "vine_boom", "beep", "horn", "whoosh".
6. Do NOT make fun of religion, politics, real named people, or any group of people. Roast behaviors and situations, not identities.

Return ONLY valid JSON matching this exact structure, with no extra text:
{
  "topic": "Short unique English name of the specific topic",
  "telegram_caption": "Catchy Roman Urdu caption for Telegram with 3-4 funny hashtags",
  "segments": [
    {
      "text_urdu": "برو، تمہاری سچویشن شپ نہیں، تمہارا ڈرامہ ہے۔",
      "overlay_text": "SAVAGE PUNCHLINE",
      "giphy_query": "shocked meme reaction",
      "sfx": "vine_boom"
    }
  ]
}
"""


def extract_gemini_text(data):
    """Pull the answer text out of a Gemini response (ignores 'thought' parts)."""
    cands = data.get("candidates") or []
    if not cands:
        return ""
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


def parse_json_loose(raw):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise
        data = json.loads(m.group(0))
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("Gemini JSON is not an object")
    return data


def clean_overlay(text):
    t = (text or "").upper()
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"[^A-Z0-9 !?.,'\"&%$#@+:/\-]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:40] or "BRUH MOMENT"


def clean_query(text):
    t = re.sub(r"[^A-Za-z0-9 ]", " ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:60] or "funny reaction meme"


def validate_script(data):
    """Turn whatever Gemini returned into a clean, safe structure (or raise)."""
    segs_in = data.get("segments")
    if not isinstance(segs_in, list):
        raise ValueError("no 'segments' list in Gemini JSON")

    segments = []
    for i, s in enumerate(segs_in):
        if not isinstance(s, dict):
            continue
        text = re.sub(r"\s+", " ", str(s.get("text_urdu", ""))).strip()
        if len(text) < 3:
            continue
        sfx = str(s.get("sfx", "")).strip().lower()
        if sfx not in ALLOWED_SFX:
            sfx = "vine_boom" if i == 0 else random.choice(["whoosh", "vine_boom", "horn"])
        segments.append({
            "text_urdu": text,
            "overlay_text": clean_overlay(s.get("overlay_text")),
            "giphy_query": clean_query(s.get("giphy_query")),
            "sfx": sfx,
        })

    if len(segments) < 4:
        raise ValueError(f"only {len(segments)} usable segments (need at least 4)")

    topic = re.sub(r"\s+", " ", str(data.get("topic", ""))).strip() or f"GenZ Roast {int(time.time())}"
    caption = str(data.get("telegram_caption", "")).strip() or f"🔥 {topic} #GenZRoast #Savage"
    return {"topic": topic, "telegram_caption": caption, "segments": segments[:MAX_SEGMENTS]}


def call_gemini_model(model, thinking_cfg, prompt):
    """Ask ONE model for a script. Returns a validated script dict, or None to move on."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    gen_cfg = {
        "responseMimeType": "application/json",
        "temperature": 1.0,
        "maxOutputTokens": 16384,
    }
    if thinking_cfg:
        gen_cfg["thinkingConfig"] = dict(thinking_cfg)
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg}

    for attempt in range(1, GEMINI_ATTEMPTS_PER_MODEL + 1):
        started = time.time()
        try:
            resp = requests.post(
                url, headers=headers, json=payload,
                timeout=(GEMINI_CONNECT_TIMEOUT, GEMINI_READ_TIMEOUT),
            )
        except requests.Timeout:
            log(f"⚠️ {model}: too slow ({time.time() - started:.0f}s). Moving to the next model.")
            return None
        except requests.ConnectionError as e:
            log(f"⚠️ {model} attempt {attempt}: connection problem ({type(e).__name__})")
            time.sleep(4)
            continue

        code = resp.status_code
        body = resp.text[:200].replace("\n", " ")

        if code == 200:
            try:
                raw = extract_gemini_text(resp.json())
                if not raw:
                    raise ValueError("empty answer (maybe blocked or cut off)")
                script = validate_script(parse_json_loose(raw))
                log(f"✅ {model} answered in {time.time() - started:.0f}s")
                return script
            except Exception as e:
                log(f"⚠️ {model} attempt {attempt}: bad answer ({e})")
                continue

        if code in (401, 403) or "API key not valid" in resp.text or "API_KEY_INVALID" in resp.text:
            raise FatalError(
                f"Gemini rejected your API key (HTTP {code}). Check the GEMINI_API_KEY secret. "
                f"Details: {body}"
            )
        if code == 400 and "thinkingConfig" in gen_cfg:
            log(f"⚠️ {model}: thinking setting rejected, retrying without it")
            gen_cfg.pop("thinkingConfig", None)
            continue
        if code in (429, 500, 502, 503, 504):
            reason = "busy (503)" if code == 503 else f"limit/error (HTTP {code})"
            log(f"⚠️ {model} is {reason}. Switching to the next model right away.")
            return None

        log(f"⚠️ {model}: HTTP {code}, skipping this model. {body}")
        return None

    return None


def request_script(blocked_topics, focus):
    topics_str = ", ".join(blocked_topics) if blocked_topics else "None yet"
    prompt = PROMPT_TEMPLATE.replace("__PAST_TOPICS__", topics_str).replace("__FOCUS__", focus)

    for round_no in range(1, GEMINI_ROUNDS + 1):
        for model, thinking in GEMINI_MODELS:
            log(f"🤖 Trying {model} ...")
            script = call_gemini_model(model, thinking, prompt)
            if script:
                return script
        if round_no < GEMINI_ROUNDS:
            wait = GEMINI_ROUND_WAIT * round_no
            log(f"⏳ Every model is busy right now. Waiting {wait}s, then trying again "
                f"(round {round_no + 1}/{GEMINI_ROUNDS})...")
            time.sleep(wait)

    raise FatalError("All Gemini models failed in every round (busy, quota or key problem). "
                     "Run the workflow again in a few minutes.")


def generate_roast_script(history):
    log("🔥 Generating Gen-Z roast script via Gemini...")
    blocked = list(history[-PROMPT_HISTORY:])
    known = {normalize_topic(t) for t in history}
    focus = random.choice(FOCUS_AREAS)
    log(f"🎲 Focus area: {focus}")
    script = None
    for _ in range(3):
        script = request_script(blocked, focus)
        if normalize_topic(script["topic"]) not in known:
            return script
        log(f"🔁 Topic '{script['topic']}' was already used. Asking for another one...")
        blocked.append(script["topic"])
    return script  # accept after 3 tries rather than failing the whole run


# =============================================================================
# STEP 2: URDU VOICEOVER  (text clean-up -> edge-tts -> studio-style mastering)
# =============================================================================
URDU_NUM = [
    "صفر", "ایک", "دو", "تین", "چار", "پانچ", "چھ", "سات", "آٹھ", "نو", "دس",
    "گیارہ", "بارہ", "تیرہ", "چودہ", "پندرہ", "سولہ", "سترہ", "اٹھارہ", "انیس", "بیس",
    "اکیس", "بائیس", "تئیس", "چوبیس", "پچیس", "چھبیس", "ستائیس", "اٹھائیس", "انتیس", "تیس",
    "اکتیس", "بتیس", "تینتیس", "چونتیس", "پینتیس", "چھتیس", "سینتیس", "اڑتیس", "انتالیس", "چالیس",
    "اکتالیس", "بیالیس", "تینتالیس", "چوالیس", "پینتالیس", "چھیالیس", "سینتالیس", "اڑتالیس", "انچاس", "پچاس",
    "اکاون", "باون", "ترپن", "چون", "پچپن", "چھپن", "ستاون", "اٹھاون", "انسٹھ", "ساٹھ",
    "اکسٹھ", "باسٹھ", "تریسٹھ", "چوسٹھ", "پینسٹھ", "چھیاسٹھ", "سڑسٹھ", "اڑسٹھ", "انہتر", "ستر",
    "اکہتر", "بہتر", "تہتر", "چوہتر", "پچھتر", "چھہتر", "ستتر", "اٹھہتر", "اناسی", "اسی",
    "اکیاسی", "بیاسی", "تراسی", "چوراسی", "پچاسی", "چھیاسی", "ستاسی", "اٹھاسی", "نواسی", "نوے",
    "اکانوے", "بانوے", "ترانوے", "چورانوے", "پچانوے", "چھیانوے", "ستانوے", "اٹھانوے", "ننانوے", "سو",
]
assert len(URDU_NUM) == 101

LATIN_TO_URDU = {
    "ok": "اوکے", "okay": "اوکے", "ai": "اے آئی", "wifi": "وائی فائی", "bro": "برو", "cringe": "کرنج",
    "vibe": "وائب", "vibes": "وائبز", "reel": "ریل", "reels": "ریلز", "story": "سٹوری", "like": "لائک",
    "share": "شیئر", "comment": "کمنٹ", "follow": "فالو", "online": "آن لائن", "offline": "آف لائن",
    "instagram": "انسٹاگرام", "insta": "انسٹا", "youtube": "یوٹیوب", "tiktok": "ٹک ٹاک",
    "whatsapp": "واٹس ایپ", "snapchat": "سنیپ چیٹ", "facebook": "فیس بک", "netflix": "نیٹ فلکس",
    "google": "گوگل", "chatgpt": "چیٹ جی پی ٹی", "pubg": "پب جی", "iphone": "آئی فون", "gym": "جم",
    "to": "ٹو", "gen": "جین", "z": "زی", "dm": "ڈی ایم", "crush": "کرش", "ex": "ایکس",
}

_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_URDU_CHARS = "\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF"


def urdu_number(n):
    """Whole number -> Urdu words (Urdu numbers up to 100 are all different words)."""
    if n <= 100:
        return URDU_NUM[n]
    if n < 1000:
        h, r = divmod(n, 100)
        s = f"{URDU_NUM[h]} سو"
        return s if r == 0 else f"{s} {URDU_NUM[r]}"
    if n < 100000:
        th, r = divmod(n, 1000)
        s = f"{urdu_number(th)} ہزار"
        return s if r == 0 else f"{s} {urdu_number(r)}"
    return " ".join(URDU_NUM[int(d)] for d in str(n))  # very long numbers: digit by digit


def prepare_tts_text(text):
    """Clean the script line so the Urdu voice reads it smoothly (numbers, symbols, pauses)."""
    t = str(text).translate(_DIGIT_MAP)
    t = re.sub(r"https?://\S+|www\.\S+", " ", t)           # links
    t = re.sub(r"[#@]\S+", " ", t)                           # hashtags / mentions
    t = t.replace("%", " فیصد ")
    t = re.sub(r"(\d+)\.(\d+)", lambda m: f"{m.group(1)} اعشاریہ {m.group(2)}", t)
    t = re.sub(r"\d+", lambda m: urdu_number(int(m.group())) if len(m.group()) <= 5
               else " ".join(URDU_NUM[int(d)] for d in m.group()), t)
    t = re.sub(r"[A-Za-z][A-Za-z']*", lambda m: LATIN_TO_URDU.get(m.group().lower(), m.group()), t)

    # keep only Urdu letters, leftover Latin letters, spaces and speech punctuation
    t = re.sub(rf"[^{_URDU_CHARS}A-Za-z\s،۔؟!?.,:;\-'\"]", " ", t)
    t = t.replace("-", " ").replace(":", "،").replace(";", "،")
    t = t.replace("…", "۔").replace(",", "،").replace("?", "؟")
    t = re.sub(r"\.{2,}", "۔", t).replace(".", "۔")
    t = re.sub(r"([!؟۔،])(?:\s*[!؟۔،])+", r"\1", t)         # no stacked punctuation
    t = re.sub(r"\s+", " ", t).strip(" ،")
    if t and t[-1] not in "۔!؟":
        t += "۔"
    return t


async def _tts_async(text, voice, out_path):
    import edge_tts  # imported here so a missing package gives a clear error

    communicate = edge_tts.Communicate(text, voice, rate=TTS_RATE, pitch=TTS_PITCH)
    await asyncio.wait_for(communicate.save(out_path), timeout=60)


def synthesize_speech(text, out_path):
    """Create an Urdu voiceover. Retries and falls back to other voices."""
    text = prepare_tts_text(text)
    if len(text) < 2:
        raise RuntimeError("nothing speakable left in this line after cleaning")

    voices = [TTS_VOICE] + [v for v in TTS_FALLBACK_VOICES if v != TTS_VOICE]
    last_error = None
    for voice in voices:
        for attempt in range(1, 4):
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
                asyncio.run(_tts_async(text, voice, out_path))
                if os.path.exists(out_path) and os.path.getsize(out_path) > 1000 \
                        and media_duration(out_path) > 0.3:
                    return voice
                last_error = "empty audio"
            except ImportError as e:
                raise FatalError("edge-tts is not installed (pip install edge-tts)") from e
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
            time.sleep(2 * attempt)
        log(f"   ⚠️ voice {voice} failed ({last_error}), trying next voice...")
    raise RuntimeError(f"Text-to-speech failed for every voice. Last error: {last_error}")


# Trim silence at both ends (tighter pace), remove rumble, add warmth + clarity,
# gently compress so every word is equally loud, then convert to 44.1 kHz stereo.
_TRIM = "silenceremove=start_periods=1:start_duration=0.01:start_threshold=-48dB:detection=peak"
VOICE_CHAIN = (
    f"{_TRIM},areverse,{_TRIM},areverse,"
    "highpass=f=90,"
    "equalizer=f=220:t=q:w=1:g=1.5,"
    "equalizer=f=3200:t=q:w=1:g=2.5,"
    "acompressor=threshold=-20dB:ratio=3:attack=4:release=70:makeup=3dB,"
    "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
)


def master_voice(tts_path, out_wav):
    try:
        run_cmd(FFMPEG + ["-i", tts_path, "-af", VOICE_CHAIN, "-c:a", "pcm_s16le", out_wav],
                "mastering the voice")
        if media_duration(out_wav) > 0.25:
            return
    except Exception as e:
        log(f"   ⚠️ voice mastering skipped ({str(e).splitlines()[0]})")
    # plain conversion if mastering failed or trimmed everything away
    run_cmd(FFMPEG + ["-i", tts_path, "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", out_wav],
            "converting the voice")


# =============================================================================
# STEP 3: SOUND EFFECTS (synthesized by FFmpeg, no downloads)
# =============================================================================
SFX_SOURCES = {
    "vine_boom": "aevalsrc='0.9*(sin(2*PI*70*t)+0.6*sin(2*PI*140*t)+0.35*sin(2*PI*210*t))*exp(-4*t)':d=0.6:s=44100",
    "beep": "sine=frequency=1000:duration=0.3:sample_rate=44100,volume=0.4",
    "horn": "aevalsrc='0.3*(sin(2*PI*440*t)+sin(2*PI*554*t)+sin(2*PI*659*t))*exp(-2.5*t):d=0.5:s=44100'",
    "whoosh": "anoisesrc=d=0.45:c=pink:r=44100:a=0.7,highpass=f=400,lowpass=f=6000,"
              "afade=t=in:d=0.18,afade=t=out:st=0.2:d=0.25",
    "cut": "anoisesrc=d=0.28:c=pink:r=44100:a=0.7,highpass=f=500,lowpass=f=7000,"
           "afade=t=in:d=0.12,afade=t=out:st=0.12:d=0.16",
}
_sfx_cache = {}


def get_sfx(sfx_type):
    if sfx_type not in SFX_SOURCES:
        sfx_type = "vine_boom"
    if sfx_type not in _sfx_cache:
        out = work(f"sfx_{sfx_type}.wav")
        run_cmd(FFMPEG + ["-f", "lavfi", "-i", SFX_SOURCES[sfx_type], "-c:a", "pcm_s16le", out],
                f"creating sound effect '{sfx_type}'")
        _sfx_cache[sfx_type] = out
    return _sfx_cache[sfx_type]


def mix_audio(voice_wav, sfx_wav, cut_times, total, out_wav):
    """Voice + main SFX + a soft whoosh on every picture cut, at constant volume."""
    cut_wav = get_sfx("cut") if cut_times else None
    inputs = ["-i", voice_wav, "-i", sfx_wav] + (["-i", cut_wav] * len(cut_times) if cut_wav else [])
    n = 2 + len(cut_times)
    fmt = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
    end = f"apad=whole_dur={total:.3f},atrim=0:{total:.3f}"

    chains = [f"[0:a]{fmt},volume={1.0 * n:.2f},{end}[a0]",
              f"[1:a]{fmt},volume={0.45 * n:.2f},{end}[a1]"]
    for i, ct in enumerate(cut_times):
        ms = max(0, int((ct - 0.06) * 1000))
        chains.append(f"[{2 + i}:a]{fmt},volume={0.30 * n:.2f},adelay={ms}|{ms},{end}[a{2 + i}]")
    labels = "".join(f"[a{i}]" for i in range(n))
    chains.append(f"{labels}amix=inputs={n}:duration=longest:dropout_transition=0,"
                  f"alimiter=limit=0.95,atrim=0:{total:.3f}[aout]")

    run_cmd(FFMPEG + inputs + ["-filter_complex", ";".join(chains), "-map", "[aout]",
                               "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", out_wav],
            "mixing voice and sound effects")


# =============================================================================
# STEP 4: GIPHY VISUALS
# =============================================================================
GIF_KEYS = ("original_mp4", "fixed_height", "downsized_small", "fixed_width")


def pick_mp4_url(item):
    images = item.get("images", {}) or {}
    for key in GIF_KEYS:
        url = (images.get(key) or {}).get("mp4")
        if url:
            return url
    return None


def make_fallback_background(out_path):
    run_cmd(FFMPEG + ["-f", "lavfi", "-i", "color=c=0x1a1a2e:s=720x720:d=3:r=30",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path],
            "creating fallback background")


def fetch_giphy_clips(query, count, base_name, used_ids):
    """Get `count` different GIFs (as mp4) for one segment. Never returns an empty list."""
    short = " ".join(query.split()[:2])
    queries = []
    for q in (query, short, "funny reaction meme", "shocked meme"):
        if q and q not in queries:
            queries.append(q)

    candidates, seen = [], set(used_ids)
    for q in queries:
        try:
            r = requests.get(
                "https://api.giphy.com/v1/gifs/search",
                params={"api_key": GIPHY_API_KEY, "q": q, "limit": 20, "rating": "pg-13", "lang": "en"},
                timeout=20,
            )
            if r.status_code == 429:
                log("   ⚠️ Giphy rate limit hit.")
                break
            if r.status_code != 200:
                log(f"   ⚠️ Giphy HTTP {r.status_code} for '{q}': {r.text[:120]}")
                continue
            top = [x for x in r.json().get("data", []) if x.get("id") not in seen][:10]
            random.shuffle(top)
            for item in top:
                url = pick_mp4_url(item)
                if url:
                    candidates.append((item.get("id"), url))
                    seen.add(item.get("id"))
        except Exception as e:
            log(f"   ⚠️ Giphy error for '{q}': {type(e).__name__}")
        if len(candidates) >= count + 3:
            break

    clips = []
    for cid, url in candidates:
        if len(clips) >= count:
            break
        path = f"{base_name}_{len(clips)}.mp4"
        try:
            vid = requests.get(url, timeout=30)
            if vid.status_code != 200 or len(vid.content) > 25_000_000:
                continue
            with open(path, "wb") as f:
                f.write(vid.content)
            if is_valid_video(path):
                used_ids.add(cid)
                clips.append(path)
        except requests.RequestException:
            continue

    if not clips:
        log("   ⚠️ No GIF found, using a plain background for this segment.")
        fb = f"{base_name}_fb.mp4"
        make_fallback_background(fb)
        clips = [fb]
    while len(clips) < count:           # not enough different GIFs: reuse the ones we have
        clips.append(clips[len(clips) % len(clips)])
    return clips[:count]


# =============================================================================
# STEP 5: RENDER VIDEO  (fast cuts, flash + shake on every cut)
# =============================================================================
def write_caption_files(idx, overlay):
    """Wrap the caption into up to 3 centered lines, each in its own text file."""
    lines = textwrap.wrap(overlay, width=16, break_long_words=False) or [overlay]
    lines = lines[:3]
    paths = []
    for n, line in enumerate(lines):
        p = os.path.abspath(work(f"caption_{idx}_{n}.txt"))
        with open(p, "w", encoding="utf-8") as f:
            f.write(line)
        paths.append(p)
    longest = max(len(l) for l in lines)
    fontsize = max(40, min(68, int(940 / (0.80 * max(longest, 1)))))
    return paths, fontsize


def build_filter(idx, overlay, font_path, part_durs, sfx_type):
    """One picture 'part' per GIF (a segment can have 1-3 quick cuts), then the caption on top."""
    W, H = VIDEO_W, VIDEO_H
    n = len(part_durs)
    color = CAPTION_COLORS[idx % len(CAPTION_COLORS)]
    amp = 14 if sfx_type in ("vine_boom", "horn") else 8  # shake strength in pixels

    chains = []
    for p, dp in enumerate(part_durs):
        chains += [
            f"[{p}:v]fps={VIDEO_FPS},trim=duration={dp:.3f},setpts=PTS-STARTPTS,split=2[bgs{p}][fgs{p}]",
            # blurred, darkened full-screen background (blur at low resolution = fast)
            f"[bgs{p}]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,"
            f"scale={W}:{H},eq=brightness=-0.22:saturation=1.1,setsar=1[bg{p}]",
            # sharp GIF in a white meme-style frame
            f"[fgs{p}]scale=960:1000:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad=iw+16:ih+16:8:8:color=white,setsar=1[fg{p}]",
            # compose, white flash at the cut, then a quick camera shake that settles
            f"[bg{p}][fg{p}]overlay=(W-w)/2:(H-h)/2-100,format=yuv420p,"
            f"eq=brightness='0.28*exp(-t*12)':eval=frame,"
            f"scale=1146:2036,"
            f"crop={W}:{H}:x='(iw-ow)/2+{amp}*sin(t*70)*exp(-t*9)':"
            f"y='(ih-oh)/2+{amp * 0.8:.1f}*cos(t*53)*exp(-t*9)',setsar=1[part{p}]",
        ]
    if n > 1:
        chains.append("".join(f"[part{p}]" for p in range(n)) + f"concat=n={n}:v=1:a=0[v1]")
    else:
        chains.append("[part0]null[v1]")

    # caption (stays on screen across the cuts)
    caption_files, fontsize = write_caption_files(idx, overlay)
    font_opt = f"fontfile='{font_path}':" if font_path else "font='Sans':"
    line_h = int(fontsize * 1.45)
    draw = []
    for k, path in enumerate(caption_files):
        draw.append(
            f"drawtext={font_opt}textfile='{path}':expansion=none:"
            f"fontcolor={color}:fontsize={fontsize}:borderw=6:bordercolor=black:"
            f"box=1:boxcolor=black@0.6:boxborderw=16:"
            f"x=(w-text_w)/2:y=1500+{k * line_h}:alpha='min(1,t/0.1)'"
        )
    if WATERMARK:
        wm_clean = re.sub(r"[^A-Za-z0-9@._\- ]", "", WATERMARK)[:30]
        if wm_clean:
            wm_path = os.path.abspath(work("watermark.txt"))
            with open(wm_path, "w", encoding="utf-8") as f:
                f.write(wm_clean)
            draw.append(
                f"drawtext={font_opt}textfile='{wm_path}':expansion=none:"
                "fontcolor=white@0.75:fontsize=40:borderw=3:bordercolor=black:"
                "x=(w-text_w)/2:y=110"
            )
    chains.append("[v1]" + ",".join(draw) + "[vout]")
    return ";".join(chains)


def build_segment_video(idx, segment, out_clip_path, used_gif_ids):
    tts_path = work(f"tts_{idx}.mp3")
    voice_wav = work(f"voice_{idx}.wav")
    mixed_wav = work(f"audio_{idx}.wav")

    # 1. Voiceover (clean text -> speech -> mastered, silence trimmed)
    synthesize_speech(segment["text_urdu"], tts_path)
    master_voice(tts_path, voice_wav)
    vo_dur = media_duration(voice_wav)
    if vo_dur < 0.25:
        raise RuntimeError("voice clip is empty")

    # 2. Plan the cuts: roughly one new GIF every ~2 seconds
    total = vo_dur + TAIL_PAD
    parts = 1 if total < 3.0 else (2 if total < 5.2 else 3)
    cut_times = [total * i / parts for i in range(1, parts)]

    # 3. Audio: voice + sound effect + whoosh on the cuts
    mix_audio(voice_wav, get_sfx(segment["sfx"]), cut_times, total, mixed_wav)
    duration = media_duration(mixed_wav)

    # 4. Visuals
    gifs = fetch_giphy_clips(segment["giphy_query"], parts, work(f"gif_{idx}"), used_gif_ids)
    part_durs = [duration / parts] * parts
    part_durs[-1] += 0.1  # last part slightly longer; -t below trims the exact end

    # 5. Render 1080x1920 clip
    fc = build_filter(idx, segment["overlay_text"], find_font(), part_durs, segment["sfx"])
    cmd = list(FFMPEG)
    for g in gifs:
        cmd += ["-stream_loop", "-1", "-i", g]
    cmd += [
        "-i", mixed_wav,
        "-filter_complex", fc,
        "-map", "[vout]", "-map", f"{parts}:a",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        out_clip_path,
    ]
    run_cmd(cmd, f"rendering segment {idx + 1}")
    return duration


def concatenate_clips(clip_paths, final_output):
    concat_list = work("concat_list.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for p in clip_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    try:
        run_cmd(FFMPEG + ["-f", "concat", "-safe", "0", "-i", concat_list,
                          "-c", "copy", "-movflags", "+faststart", final_output],
                "joining clips (fast copy)")
    except RuntimeError as e:
        log(f"⚠️ Fast join failed, re-encoding instead.\n{e}")
        run_cmd(FFMPEG + ["-f", "concat", "-safe", "0", "-i", concat_list,
                          "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                          "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                          "-movflags", "+faststart", final_output],
                "joining clips (re-encode)")


def normalize_loudness(src, dst):
    """Two-pass loudness normalization so the whole video has a clear, consistent volume."""
    af = f"loudnorm=I={LOUDNESS_TARGET}:TP=-1.5:LRA=11"
    first = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", src, "-vn", "-af", af + ":print_format=json",
         "-f", "null", "-"], capture_output=True, text=True)
    blocks = re.findall(r"\{[^{}]*\}", first.stderr or "")
    if first.returncode != 0 or not blocks:
        raise RuntimeError("loudness measurement failed")
    m = json.loads(blocks[-1])
    vals = {k: float(m[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    if any(abs(v) > 500 for v in vals.values()):
        raise RuntimeError("audio is silent or unmeasurable")
    second = (f"{af}:measured_I={vals['input_i']}:measured_TP={vals['input_tp']}:"
              f"measured_LRA={vals['input_lra']}:measured_thresh={vals['input_thresh']}:"
              f"offset={vals['target_offset']}:linear=true")
    run_cmd(FFMPEG + ["-i", src, "-c:v", "copy", "-af", second,
                      "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                      "-movflags", "+faststart", dst],
            "applying loudness normalization")
    log(f"🔊 Loudness: {vals['input_i']:.1f} LUFS  ->  {LOUDNESS_TARGET} LUFS")


def shrink_if_needed(path):
    """Telegram bots can only upload ~50 MB. Compress if the video is too big."""
    size_mb = os.path.getsize(path) / 1_000_000
    if size_mb <= TELEGRAM_MAX_MB:
        return path
    log(f"📦 Video is {size_mb:.1f} MB (too big for Telegram). Compressing...")
    small = "final_roast_small.mp4"
    run_cmd(FFMPEG + ["-i", path, "-vf", "scale=720:1280", "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", "29", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                      "-movflags", "+faststart", small],
            "compressing final video")
    return small


# =============================================================================
# STEP 6: TELEGRAM
# =============================================================================
def telegram_post(method, data, files):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, data=data, files=files, timeout=(20, 600))
    except requests.RequestException as e:
        # Do not print the exception text: it contains the URL (and your bot token).
        return None, f"network error ({type(e).__name__})"
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code == 200 and body.get("ok"):
        return True, ""
    return False, f"HTTP {resp.status_code}: {body.get('description', resp.text[:200])}"


def send_to_telegram(video_path, caption, duration):
    log("🚀 Uploading roast video to Telegram...")
    width, height = video_size(video_path)
    caption = caption[:1020]
    last_err = ""

    for attempt in range(1, 4):
        with open(video_path, "rb") as vf:
            ok, last_err = telegram_post(
                "sendVideo",
                {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "supports_streaming": "true",
                 "width": width, "height": height, "duration": int(duration)},
                {"video": ("roast.mp4", vf, "video/mp4")},
            )
        if ok:
            log("✅ Video delivered to Telegram!")
            return
        log(f"⚠️ Telegram sendVideo attempt {attempt} failed: {last_err}")
        if "chat not found" in last_err.lower() or "unauthorized" in last_err.lower():
            raise FatalError(f"Telegram rejected the bot token or chat id: {last_err}")
        time.sleep(5 * attempt)

    log("📎 Trying to send as a document instead...")
    with open(video_path, "rb") as vf:
        ok, last_err = telegram_post(
            "sendDocument",
            {"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
            {"document": ("roast.mp4", vf, "video/mp4")},
        )
    if not ok:
        raise RuntimeError(f"Telegram upload failed: {last_err}")
    log("✅ Video delivered to Telegram (as document).")


# =============================================================================
# MAIN
# =============================================================================
def check_environment():
    missing = [name for name, val in [
        ("GEMINI_API_KEY", GEMINI_API_KEY), ("GIPHY_API_KEY", GIPHY_API_KEY),
        ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN), ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
    ] if not val]
    if missing:
        raise FatalError("Missing GitHub secrets: " + ", ".join(missing)
                         + "  (Repo -> Settings -> Secrets and variables -> Actions)")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise FatalError(f"'{tool}' is not installed. Add: sudo apt-get install -y ffmpeg")
    if not find_font():
        log("⚠️ DejaVu font not found; captions will use the system default font.")


def main():
    started = time.time()
    check_environment()

    shutil.rmtree(WORK_DIR, ignore_errors=True)
    os.makedirs(WORK_DIR, exist_ok=True)

    history = load_history()
    script = generate_roast_script(history)

    topic = script["topic"]
    segments = script["segments"]
    log(f"🎯 Topic: {topic}")
    log(f"📝 Segments: {len(segments)}")

    clip_paths = []
    used_gif_ids = set()
    for idx, seg in enumerate(segments):
        log(f"⚡ Segment {idx + 1}/{len(segments)}: {seg['overlay_text']}  [{seg['giphy_query']}]")
        clip_out = work(f"segment_{idx}.mp4")
        try:
            dur = build_segment_video(idx, seg, clip_out, used_gif_ids)
            clip_paths.append(clip_out)
            log(f"   ✔ done ({dur:.1f}s)")
        except FatalError:
            raise
        except Exception as e:
            log(f"   ❌ Segment {idx + 1} skipped: {e}")

    if len(clip_paths) < 3:
        raise RuntimeError(f"Only {len(clip_paths)} segments rendered; refusing to send a broken video.")

    joined = work("joined.mp4")
    concatenate_clips(clip_paths, joined)
    try:
        normalize_loudness(joined, FINAL_VIDEO)
    except Exception as e:
        log(f"⚠️ Loudness normalization skipped ({str(e).splitlines()[0]}). Using the plain version.")
        shutil.copyfile(joined, FINAL_VIDEO)

    video_to_send = shrink_if_needed(FINAL_VIDEO)
    total_dur = media_duration(video_to_send)
    log(f"🎞️ Final video: {total_dur:.1f}s, {os.path.getsize(video_to_send) / 1_000_000:.1f} MB")

    caption = f"🔥 Topic: {topic}\n\n{script['telegram_caption']}"
    send_to_telegram(video_to_send, caption, total_dur)

    save_history(history, topic)
    log(f"🎉 Pipeline completed in {time.time() - started:.0f}s. Topic saved to memory!")


if __name__ == "__main__":
    try:
        main()
    except FatalError as e:
        log(f"\n❌ STOPPED: {e}")
        sys.exit(1)
    except Exception as e:
        log(f"\n❌ UNEXPECTED ERROR: {type(e).__name__}: {e}")
        raise
