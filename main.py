#!/usr/bin/env python3
"""
GenZ Roast Video Bot  (Gemini script -> Urdu voiceover -> Giphy visuals -> Telegram)

Pipeline:
  1. Gemini writes a 45-60s roast script as JSON (with retries + model fallbacks).
  2. edge-tts speaks every segment in Urdu.
  3. FFmpeg synthesizes a meme sound effect per segment and mixes it with the voice.
  4. Giphy supplies a reaction GIF (mp4) per segment.
  5. FFmpeg renders a 1080x1920 vertical clip per segment (blurred background,
     framed GIF, big meme caption) and joins all clips.
  6. The final video is sent to Telegram and the topic is saved to memory.

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
TTS_RATE = os.environ.get("TTS_RATE", "+15%").strip()
TTS_PITCH = os.environ.get("TTS_PITCH", "+2Hz").strip()
TTS_FALLBACK_VOICES = ["ur-PK-UzmaNeural", "ur-IN-SalmanNeural", "ur-IN-GulNeural"]

# Optional small text at the top of the video, e.g. "@MyChannel". Empty = off.
WATERMARK = os.environ.get("WATERMARK", "").strip()

# (model name, thinking settings). Tried in order until one works.
# "low" thinking keeps the newest model fast so requests do not time out.
GEMINI_MODELS = [
    ("gemini-3.8-flash", {"thinkingLevel": "low"}),
    ("gemini-3.6-flash", None),
    ("gemini-2.5-flash", {"thinkingBudget": 0}),
    ("gemini-3.5-flash-lite", None),
]
GEMINI_ATTEMPTS_PER_MODEL = 2
GEMINI_CONNECT_TIMEOUT = 15
GEMINI_READ_TIMEOUT = 180  # the old script used 60s, which was too short

HISTORY_FILE = "topics_history.json"
HISTORY_KEEP = 200
WORK_DIR = "build_temp"
FINAL_VIDEO = "final_roast.mp4"

VIDEO_W, VIDEO_H, VIDEO_FPS = 1080, 1920, 30
TELEGRAM_MAX_MB = 48  # Telegram bots can upload up to 50 MB

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

CRITICAL RULE - DO NOT REPEAT ANY OF THESE PAST TOPICS:
[__PAST_TOPICS__]

Create a fast-paced 45 to 60 second roast video script divided into 8 to 10 punchy segments.
Rules for the script:
1. Segment 1 MUST be a brutal 3-second attention-grabber hook that stops the scroll immediately.
2. "text_urdu" MUST be written in proper Urdu script (اردو رسم الخط) so the Urdu Text-to-Speech engine pronounces every word naturally. Write English Gen-Z slang phonetically in Urdu script (e.g., سچویشن شپ, ریلز, کرنج, برو, جم بوائز, ڈپریشن, وائب). Keep the tone conversational, sarcastic, energetic and hilarious (no abusive/NSFW curses, just savage psychological roasting). Each segment should be 10 to 22 words so it takes about 4 to 7 seconds to speak.
3. "overlay_text" MUST be short (2 to 4 words max) in ALL CAPS English/Roman Urdu, flashing on screen like a meme caption (e.g., "GOLD MEDAL SCROLLING", "BRO THOUGHT HE ATE", "9-TO-5 SLAVERY"). Use only letters, numbers and basic punctuation, no emojis.
4. "giphy_query" MUST be a specific 2-4 word English search term for Giphy that fetches a hilarious reaction GIF matching that line (e.g., "shocked guy meme", "crying cat meme", "disappointed cricket fan", "typing fast meme", "clown makeup meme").
5. "sfx" MUST be one of: "vine_boom", "beep", "horn", "whoosh".
6. Do NOT make fun of religion, politics, real named people, or any group of people. Roast behaviors and situations, not identities.

Return ONLY valid JSON matching this exact structure, with no extra text:
{
  "topic": "Short unique English name of the specific topic",
  "telegram_caption": "Catchy Roman Urdu caption for Telegram with 3-4 funny hashtags",
  "segments": [
    {
      "text_urdu": "اردو میں تیز اور طنزیہ ڈائیلاگ...",
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
    return {"topic": topic, "telegram_caption": caption, "segments": segments[:12]}


def call_gemini_model(model, thinking_cfg, prompt):
    """Ask ONE model for a script. Returns a validated script dict or None."""
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
        except (requests.Timeout, requests.ConnectionError) as e:
            log(f"⚠️ {model} attempt {attempt}: network problem ({type(e).__name__}) "
                f"after {time.time() - started:.0f}s")
            time.sleep(5 * attempt)
            continue

        code = resp.status_code
        body = resp.text[:300].replace("\n", " ")

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
            log(f"⚠️ {model}: thinking setting rejected, retrying without it ({body})")
            gen_cfg.pop("thinkingConfig", None)
            continue
        if code in (429, 500, 502, 503, 504):
            log(f"⚠️ {model} attempt {attempt}: HTTP {code} (busy/limit). Waiting, then retrying. {body}")
            time.sleep(8 * attempt)
            continue

        log(f"⚠️ {model}: HTTP {code}, skipping this model. {body}")
        return None

    return None


def request_script(blocked_topics):
    topics_str = ", ".join(blocked_topics) if blocked_topics else "None yet"
    prompt = PROMPT_TEMPLATE.replace("__PAST_TOPICS__", topics_str)
    for model, thinking in GEMINI_MODELS:
        log(f"🤖 Trying {model} ...")
        script = call_gemini_model(model, thinking, prompt)
        if script:
            return script
    raise FatalError("All Gemini models failed. See the warnings above (timeout, quota or key problem).")


def generate_roast_script(history):
    log("🔥 Generating Gen-Z roast script via Gemini...")
    blocked = list(history[-60:])
    known = {normalize_topic(t) for t in history}
    script = None
    for _ in range(3):
        script = request_script(blocked)
        if normalize_topic(script["topic"]) not in known:
            return script
        log(f"🔁 Topic '{script['topic']}' was already used. Asking for another one...")
        blocked.append(script["topic"])
    return script  # accept after 3 tries rather than failing the whole run


# =============================================================================
# STEP 2: VOICEOVER (edge-tts)
# =============================================================================
async def _tts_async(text, voice, out_path):
    import edge_tts  # imported here so a missing package gives a clear error

    communicate = edge_tts.Communicate(text, voice, rate=TTS_RATE, pitch=TTS_PITCH)
    await asyncio.wait_for(communicate.save(out_path), timeout=60)


def synthesize_speech(text, out_path):
    """Create an Urdu voiceover. Retries and falls back to other voices."""
    text = re.sub(r"[*_#`~|<>\[\]{}]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

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


# =============================================================================
# STEP 3: SOUND EFFECTS (synthesized by FFmpeg, no downloads)
# =============================================================================
SFX_SOURCES = {
    "vine_boom": "aevalsrc='0.9*(sin(2*PI*70*t)+0.6*sin(2*PI*140*t)+0.35*sin(2*PI*210*t))*exp(-4*t)':d=0.6:s=44100",
    "beep": "sine=frequency=1000:duration=0.3:sample_rate=44100,volume=0.4",
    "horn": "aevalsrc='0.3*(sin(2*PI*440*t)+sin(2*PI*554*t)+sin(2*PI*659*t))*exp(-2.5*t):d=0.5:s=44100'",
    "whoosh": "anoisesrc=d=0.45:c=pink:r=44100:a=0.7,highpass=f=400,lowpass=f=6000,"
              "afade=t=in:d=0.18,afade=t=out:st=0.2:d=0.25",
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


def mix_audio(tts_path, sfx_path, total, out_wav):
    """Voice + SFX with constant volume (no loudness jump when the SFX ends)."""
    fc = (
        f"[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=2.4,"
        f"apad=whole_dur={total:.3f}[vo];"
        f"[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=0.9,"
        f"apad=whole_dur={total:.3f}[sfx];"
        f"[vo][sfx]amix=inputs=2:duration=longest:dropout_transition=0,"
        f"alimiter=limit=0.95,atrim=0:{total:.3f}[aout]"
    )
    run_cmd(FFMPEG + ["-i", tts_path, "-i", sfx_path, "-filter_complex", fc,
                      "-map", "[aout]", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", out_wav],
            "mixing voice and sound effect")


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


def fetch_giphy_mp4(query, out_path, used_ids):
    """Download a GIF as mp4. Returns True on success, False if a plain background was used."""
    short = " ".join(query.split()[:2])
    queries = []
    for q in (query, short, "funny reaction meme", "shocked meme"):
        if q and q not in queries:
            queries.append(q)

    for q in queries:
        try:
            r = requests.get(
                "https://api.giphy.com/v1/gifs/search",
                params={"api_key": GIPHY_API_KEY, "q": q, "limit": 15, "rating": "pg-13", "lang": "en"},
                timeout=20,
            )
            if r.status_code == 429:
                log("   ⚠️ Giphy rate limit hit, using fallback queries/background.")
                break
            if r.status_code != 200:
                log(f"   ⚠️ Giphy HTTP {r.status_code} for '{q}': {r.text[:150]}")
                continue

            results = [x for x in r.json().get("data", []) if x.get("id") not in used_ids]
            random.shuffle(results)
            for item in results[:6]:
                mp4_url = pick_mp4_url(item)
                if not mp4_url:
                    continue
                try:
                    vid = requests.get(mp4_url, timeout=30)
                    if vid.status_code != 200 or len(vid.content) > 25_000_000:
                        continue
                    with open(out_path, "wb") as f:
                        f.write(vid.content)
                    if is_valid_video(out_path):
                        used_ids.add(item.get("id"))
                        return True
                except requests.RequestException:
                    continue
        except Exception as e:
            log(f"   ⚠️ Giphy error for '{q}': {type(e).__name__}")

    log("   ⚠️ No GIF found, using a plain background for this segment.")
    make_fallback_background(out_path)
    return False


# =============================================================================
# STEP 5: RENDER VIDEO
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


def build_filter(idx, overlay, font_path):
    color = CAPTION_COLORS[idx % len(CAPTION_COLORS)]
    caption_files, fontsize = write_caption_files(idx, overlay)
    font_opt = f"fontfile='{font_path}':" if font_path else "font='Sans':"

    parts = [
        f"[0:v]fps={VIDEO_FPS},split=2[bgsrc][fgsrc]",
        # Blurred, darkened full-screen background (blur at low res = fast)
        "[bgsrc]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        f"boxblur=8:2,scale={VIDEO_W}:{VIDEO_H},eq=brightness=-0.22:saturation=1.1,setsar=1[bg]",
        # Sharp GIF in a white meme-style frame
        "[fgsrc]scale=1000:1000:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=iw+16:ih+16:8:8:color=white,setsar=1[fg]",
        "[bg][fg]overlay=(W-w)/2:(H-h)/2-100,format=yuv420p[v1]",
    ]

    draw = []
    base_y = 1500
    line_h = int(fontsize * 1.45)
    for n, path in enumerate(caption_files):
        draw.append(
            f"drawtext={font_opt}textfile='{path}':expansion=none:"
            f"fontcolor={color}:fontsize={fontsize}:borderw=6:bordercolor=black:"
            f"box=1:boxcolor=black@0.6:boxborderw=16:"
            f"x=(w-text_w)/2:y={base_y}+{n * line_h}:alpha='min(1,t/0.12)'"
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

    parts.append("[v1]" + ",".join(draw) + "[vout]")
    return ";".join(parts)


def build_segment_video(idx, segment, out_clip_path, used_gif_ids):
    tts_path = work(f"tts_{idx}.mp3")
    mixed_wav = work(f"audio_{idx}.wav")
    gif_path = work(f"gif_{idx}.mp4")

    # 1. Voiceover
    voice = synthesize_speech(segment["text_urdu"], tts_path)
    vo_dur = media_duration(tts_path)

    # 2. SFX + mix
    total = vo_dur + 0.25
    mix_audio(tts_path, get_sfx(segment["sfx"]), total, mixed_wav)
    duration = media_duration(mixed_wav)

    # 3. Visual
    fetch_giphy_mp4(segment["giphy_query"], gif_path, used_gif_ids)

    # 4. Render 1080x1920 clip
    fc = build_filter(idx, segment["overlay_text"], find_font())
    run_cmd(
        FFMPEG + [
            "-stream_loop", "-1", "-i", gif_path,
            "-i", mixed_wav,
            "-filter_complex", fc,
            "-map", "[vout]", "-map", "1:a",
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-r", str(VIDEO_FPS),
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            out_clip_path,
        ],
        f"rendering segment {idx + 1}",
    )
    return voice, duration


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
            _, dur = build_segment_video(idx, seg, clip_out, used_gif_ids)
            clip_paths.append(clip_out)
            log(f"   ✔ done ({dur:.1f}s)")
        except FatalError:
            raise
        except Exception as e:
            log(f"   ❌ Segment {idx + 1} skipped: {e}")

    if len(clip_paths) < 3:
        raise RuntimeError(f"Only {len(clip_paths)} segments rendered; refusing to send a broken video.")

    concatenate_clips(clip_paths, FINAL_VIDEO)
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
