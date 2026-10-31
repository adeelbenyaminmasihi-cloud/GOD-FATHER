import os
import json
import time
import random
import asyncio
import subprocess
import requests
import edge_tts

# ==================== CONFIGURATION ====================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GIPHY_API_KEY = os.environ.get("GIPHY_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Voice Options: "ur-PK-AsadNeural" (Male energetic) ya "ur-PK-UzmaNeural" (Female energetic)
TTS_VOICE = "ur-PK-AsadNeural"
TTS_RATE = "+15%"
TTS_PITCH = "+2Hz"

HISTORY_FILE = "topics_history.json"
WORK_DIR = "build_temp"
os.makedirs(WORK_DIR, exist_ok=True)


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_history(history, new_topic):
    history.append(new_topic)
    # Keep last 200 topics in prompt memory
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-200:], f, ensure_ascii=False, indent=2)


def generate_roast_script(past_topics):
    print("🔥 Generating CarryMinati-style Gen-Z roast script via Gemini...")
    past_topics_str = ", ".join(past_topics[-50:]) if past_topics else "None yet"

    prompt = f"""
You are an ultra-witty, high-energy Pakistani/Desi roast creator with the exact comedic timing, sarcasm, and savage energy of CarryMinati.
Your target audience is Gen-Z. Pick a brand new, hyper-relatable, funny Gen-Z problem, current internet trend, viral online behavior, or modern delusion.

CRITICAL RULE - DO NOT REPEAT ANY OF THESE PAST TOPICS:
[{past_topics_str}]

Create a fast-paced 45 to 60 second roast video script divided into 8 to 10 punchy segments.
Rules for the script:
1. Segment 1 MUST be a brutal 3-second attention-grabber hook that stops the scroll immediately.
2. "text_urdu" MUST be written in proper Urdu script (اردو رسم الخط) so the Urdu Text-to-Speech engine pronounces every word naturally. Write English Gen-Z slang phonetically in Urdu script (e.g., سچویشن شپ, ریلز, کرنج, برو, جم بوائز, ڈپریشن, وائب). Keep the tone conversational, sarcastic, energetic, and hilarious (no abusive/NSFW curses, just savage psychological roasting).
3. "overlay_text" MUST be short (2 to 4 words max) in ALL CAPS English/Roman Urdu to flash on screen like a meme caption (e.g., "GOLD MEDAL SCROLLING", "BRO THOUGHT HE ATE", "SITUationship CLOWN", "9-TO-5 SLAVERY").
4. "giphy_query" MUST be a specific 2-4 word English search term for Giphy to fetch a hilarious reaction GIF matching that line (e.g., "shocked guy meme", "crying cat meme", "disappointed cricket fan", "typing fast meme", "clown makeup meme").
5. "sfx" MUST be one of: "vine_boom", "beep", "horn", "whoosh".

Return ONLY valid JSON matching this exact structure:
{{
  "topic": "Short unique English name of the specific topic",
  "telegram_caption": "Catchy Roman Urdu caption for Telegram with 3-4 funny hashtags",
  "segments": [
    {{
      "text_urdu": "اردو میں تیز اور طنزیہ ڈائیلاگ...",
      "overlay_text": "SAVAGE PUNCHLINE",
      "giphy_query": "shocked meme reaction",
      "sfx": "vine_boom"
    }}
  ]
}}
"""

    # Updated to latest Gemini models as requested by API
    models = ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.0-flash"]
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 1.0
            }
        }
        resp = requests.post(url, json=payload, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(raw_text)
        else:
            print(f"⚠️ Model {model} returned {resp.status_code}: {resp.text}")

    raise RuntimeError("Failed to generate script from Gemini API.")


async def generate_tts(text, out_path):
    communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
    await communicate.save(out_path)


def get_audio_duration(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(res.stdout.strip())


def generate_sfx_audio(sfx_type, out_path):
    """Generates crisp meme sound effects using FFmpeg audio synthesis so links never break."""
    if sfx_type == "vine_boom":
        # Deep sub-bass drop (Vine Boom style)
        filter_str = "aevalsrc='0.9*sin(2*PI*(130-90*t)*t)*exp(-3.5*t):d=0.55:s=44100'"
    elif sfx_type == "beep":
        # Classic roast censor beep
        filter_str = "sine=frequency=1000:duration=0.25:sample_rate=44100,volume=0.4"
    elif sfx_type == "horn":
        # Airhorn / Meme punch chord
        filter_str = "aevalsrc='0.3*(sin(2*PI*440*t)+sin(2*PI*554*t)+sin(2*PI*659*t))*exp(-2.5*t):d=0.45:s=44100'"
    else:
        # Fast transition whoosh punch
        filter_str = "aevalsrc='0.6*sin(2*PI*(300-220*t)*t)*exp(-5*t):d=0.35:s=44100'"

    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", filter_str,
        "-c:a", "libmp3lame", "-q:a", "4", out_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def fetch_giphy_mp4(query, out_path):
    print(f"🎬 Fetching Giphy for: '{query}'")
    search_queries = [query, "funny reaction meme", "shocked meme"]

    for q in search_queries:
        try:
            url = "https://api.giphy.com/v1/gifs/search"
            params = {
                "api_key": GIPHY_API_KEY,
                "q": q,
                "limit": 10,
                "rating": "pg-13"
            }
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 200:
                results = r.json().get("data", [])
                if results:
                    chosen = random.choice(results[:min(5, len(results))])
                    mp4_url = (
                        chosen.get("images", {}).get("original_mp4", {}).get("mp4")
                        or chosen.get("images", {}).get("downsized_small", {}).get("mp4")
                    )
                    if mp4_url:
                        vid_data = requests.get(mp4_url, timeout=30).content
                        with open(out_path, "wb") as f:
                            f.write(vid_data)
                        return True
        except Exception as e:
            print(f"⚠️ Giphy fetch error for '{q}': {e}")

    # Ultimate fallback solid visual if API fails
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x1a1a2e:s=600x600:d=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return False


def build_segment_video(idx, segment, out_clip_path):
    tts_raw = os.path.join(WORK_DIR, f"tts_{idx}.mp3")
    sfx_path = os.path.join(WORK_DIR, f"sfx_{idx}.mp3")
    mixed_audio = os.path.join(WORK_DIR, f"audio_{idx}.aac")
    raw_gif_mp4 = os.path.join(WORK_DIR, f"gif_{idx}.mp4")
    text_file = os.path.join(WORK_DIR, f"text_{idx}.txt")

    # 1. Generate Voiceover
    asyncio.run(generate_tts(segment["text_urdu"], tts_raw))

    # 2. Generate SFX & Mix with Voiceover
    sfx_type = segment.get("sfx", "vine_boom")
    generate_sfx_audio(sfx_type, sfx_path)

    # Mix voiceover + punchy SFX at the start of the line
    mix_cmd = [
        "ffmpeg", "-y",
        "-i", tts_raw,
        "-i", sfx_path,
        "-filter_complex",
        "[0:a]volume=1.4[vo];[1:a]volume=0.55[sfx];[vo][sfx]amix=inputs=2:duration=first:dropout_transition=0,apad=pad_dur=0.15[aout]",
        "-map", "[aout]", "-c:a", "aac", "-b:a", "192k", mixed_audio
    ]
    subprocess.run(mix_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    duration = get_audio_duration(mixed_audio)

    # 3. Fetch Giphy MP4
    fetch_giphy_mp4(segment.get("giphy_query", "meme reaction"), raw_gif_mp4)

    # 4. Write sanitized overlay text to file (prevents FFmpeg escaping issues)
    overlay_text = segment.get("overlay_text", "BRUH MOMENT").upper()
    with open(text_file, "w", encoding="utf-8") as f:
        f.write(overlay_text)

    # 5. Render 1080x1920 Vertical Clip with Blurred Background + Crisp Meme + Bold Text Pop-up
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    filter_complex = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=25:10,eq=brightness=-0.25[bg];"
        "[0:v]scale=980:980:force_original_aspect_ratio=decrease,"
        "pad=1000:1000:(ow-iw)/2:(oh-ih)/2:color=black@0.4[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2-80[v1];"
        f"[v1]drawtext=fontfile='{font_path}':textfile='{text_file}':"
        "fontcolor=yellow:fontsize=64:borderw=6:bordercolor=black:"
        "box=1:boxcolor=black@0.65:boxborderw=20:"
        "x=(w-text_w)/2:y=h-380[vout]"
    )

    render_cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", raw_gif_mp4,
        "-i", mixed_audio,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "1:a",
        "-t", f"{duration:.2f}",
        "-r", "30",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        out_clip_path
    ]
    subprocess.run(render_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def concatenate_clips(clip_paths, final_output):
    concat_list = os.path.join(WORK_DIR, "concat_list.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for p in clip_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", concat_list,
        "-c", "copy", final_output
    ]
    subprocess.run(cmd, check=True)


def send_to_telegram(video_path, caption):
    print("🚀 Uploading roast video to Telegram...")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    with open(video_path, "rb") as video_file:
        files = {"video": video_file}
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
            "supports_streaming": True
        }
        resp = requests.post(url, data=data, files=files, timeout=120)
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram upload failed: {resp.text}")
    print("✅ Video successfully delivered to Telegram!")


def main():
    if not all([GEMINI_API_KEY, GIPHY_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        raise EnvironmentError("Missing one or more required environment secrets!")

    history = load_history()
    script_data = generate_roast_script(history)

    topic = script_data.get("topic", f"GenZ Roast {int(time.time())}")
    caption = script_data.get("telegram_caption", f"🔥 {topic} #GenZRoast #Savage")
    segments = script_data.get("segments", [])

    print(f"🎯 Selected New Topic: {topic}")
    print(f"📝 Total Segments: {len(segments)}")

    clip_paths = []
    for idx, seg in enumerate(segments):
        print(f"⚡ Rendering segment {idx + 1}/{len(segments)}: {seg.get('overlay_text')}")
        clip_out = os.path.join(WORK_DIR, f"segment_{idx}.mp4")
        build_segment_video(idx, seg, clip_out)
        clip_paths.append(clip_out)

    final_video = "final_roast.mp4"
    concatenate_clips(clip_paths, final_video)

    full_caption = f"🔥 Topic: {topic}\n\n{caption}"
    send_to_telegram(final_video, full_caption)

    save_history(history, topic)
    print("🎉 Pipeline completed and topic saved to memory!")


if __name__ == "__main__":
    main()
