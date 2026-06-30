import os
import json
import subprocess
import sys
import tempfile

import requests
import streamlit as st

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="LocalAppointments — Video Generator",
    page_icon="🎬",
    layout="centered",
)

st.title("🎬 LocalAppointments Video Generator")
st.caption("Paste your script, fill in the details, and download a ready-to-post 9:16 video.")

# ── Pexels key: secrets first, fallback to env, fallback to form field ────────
pexels_key_preset = st.secrets.get("PEXELS_API_KEY", "") or os.environ.get("PEXELS_API_KEY", "")

# ── Form ─────────────────────────────────────────────────────────────────────
with st.form("video_form"):
    script = st.text_area(
        "TikTok / Reels Script",
        height=220,
        placeholder="Paste the full script here...",
    )
    hook = st.text_input(
        "Hook Text (shown as bold overlay in first 2 seconds)",
        placeholder="e.g. Every missed call is a booking lost.",
    )
    keywords = st.text_input(
        "B-roll Keywords (comma-separated)",
        placeholder="e.g. missed call, nail salon, dog grooming, AI chatbot",
    )
    if not pexels_key_preset:
        pexels_key = st.text_input(
            "Pexels API Key",
            type="password",
            placeholder="Get a free key at pexels.com/api",
        )
    else:
        pexels_key = pexels_key_preset
        st.caption("✅ Pexels API key loaded from settings.")
    submitted = st.form_submit_button("🎬 Generate Video", use_container_width=True)

# ── Pipeline ──────────────────────────────────────────────────────────────────
if submitted:
    errors = []
    if not script.strip():
        errors.append("Script is required.")
    if not hook.strip():
        errors.append("Hook text is required.")
    if not keywords.strip():
        errors.append("At least one keyword is required.")
    if not pexels_key.strip():
        errors.append("Pexels API key is required.")

    if errors:
        for e in errors:
            st.error(e)
        st.stop()

    keyword_list = [k.strip() for k in keywords.split(",") if k.strip()]

    with tempfile.TemporaryDirectory() as work_dir:
        progress = st.progress(0, text="Starting...")

        # ── Step 1: Voice ─────────────────────────────────────────────────────
        progress.progress(10, text="🎙️  Generating voiceover (British English)...")
        try:
            from gtts import gTTS
            audio_path = os.path.join(work_dir, "voice.mp3")
            gTTS(text=script, lang="en", tld="co.uk").save(audio_path)
        except Exception as e:
            st.error(f"Voice generation failed: {e}")
            st.stop()

        # ── Step 2: Captions ──────────────────────────────────────────────────
        progress.progress(30, text="📝  Transcribing for captions (Whisper)...")
        try:
            import whisper
            model = whisper.load_model("base")
            result = model.transcribe(audio_path)
            srt_path = os.path.join(work_dir, "captions.srt")
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, seg in enumerate(result["segments"], 1):
                    f.write(f"{i}\n{_srt(seg['start'])} --> {_srt(seg['end'])}\n{seg['text'].strip()}\n\n")
        except Exception as e:
            st.error(f"Caption generation failed: {e}")
            st.stop()

        # ── Step 3: B-roll ────────────────────────────────────────────────────
        progress.progress(50, text="🎬  Fetching B-roll from Pexels...")
        clips = _fetch_broll(keyword_list, pexels_key, work_dir)
        if not clips:
            st.error("No B-roll clips found. Check your Pexels API key and try different keywords.")
            st.stop()

        # ── Step 4: Assemble ──────────────────────────────────────────────────
        progress.progress(70, text="🎞️  Assembling video (this takes ~1 min)...")
        output_path = os.path.join(work_dir, "final_video.mp4")
        try:
            _assemble(clips, audio_path, srt_path, hook.strip(), output_path, work_dir)
        except Exception as e:
            st.error(f"Video assembly failed: {e}")
            st.stop()

        progress.progress(100, text="✅  Done!")

        # ── Download ──────────────────────────────────────────────────────────
        with open(output_path, "rb") as f:
            video_bytes = f.read()

    st.success("Your video is ready!")
    st.video(video_bytes)
    st.download_button(
        label="⬇️  Download MP4",
        data=video_bytes,
        file_name="localappointments_video.mp4",
        mime="video/mp4",
        use_container_width=True,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _srt(s: float) -> str:
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(sec):02d},{int((s % 1) * 1000):03d}"


def _fetch_broll(keywords: list, key: str, work_dir: str) -> list:
    headers = {"Authorization": key}
    clips = []
    for i, kw in enumerate(keywords[:4]):
        try:
            resp = requests.get(
                "https://api.pexels.com/videos/search",
                headers=headers,
                params={"query": kw, "orientation": "portrait", "per_page": 3, "size": "medium"},
                timeout=15,
            )
            videos = resp.json().get("videos", [])
            if not videos:
                continue
            files = videos[0]["video_files"]
            portrait = [f for f in files if f.get("height", 0) > f.get("width", 0)]
            best = sorted(portrait or files, key=lambda x: x.get("width", 0), reverse=True)[0]
            clip_path = os.path.join(work_dir, f"broll_{i}.mp4")
            r = requests.get(best["link"], stream=True, timeout=60)
            with open(clip_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            clips.append(clip_path)
        except Exception:
            continue
    return clips


def _run(cmd: list) -> None:
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode())


def _assemble(clips, audio, srt, hook, output, work_dir):
    # Resize clips to 1080x1920
    processed = []
    for i, clip in enumerate(clips):
        out = os.path.join(work_dir, f"proc_{i}.mp4")
        _run([
            "ffmpeg", "-y", "-i", clip,
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-c:v", "libx264", "-preset", "fast", "-an", "-t", "10", out,
        ])
        processed.append(out)

    # Audio duration
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", audio,
    ]))
    duration = float(probe["format"]["duration"])

    # Loop B-roll to fill duration
    concat_txt = os.path.join(work_dir, "concat.txt")
    with open(concat_txt, "w") as f:
        total = 0.0
        while total < duration:
            for p in processed:
                f.write(f"file '{os.path.abspath(p)}'\n")
                total += 10
                if total >= duration:
                    break

    looped = os.path.join(work_dir, "looped.mp4")
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", looped])

    trimmed = os.path.join(work_dir, "trimmed.mp4")
    _run(["ffmpeg", "-y", "-i", looped, "-t", str(duration), "-c", "copy", trimmed])

    # Filters: captions + hook overlay
    safe_hook = hook.replace("'", "’").replace(":", "\\:")
    abs_srt = os.path.abspath(srt).replace("\\", "/").replace(":", "\\:")
    vf = (
        f"subtitles='{abs_srt}':force_style='"
        "FontName=Arial,FontSize=16,Bold=1,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "Outline=2,Alignment=2,MarginV=100',"
        f"drawtext=text='{safe_hook}':"
        "fontsize=44:fontcolor=white:"
        "x=(w-text_w)/2:y=h/5:"
        "box=1:boxcolor=black@0.6:boxborderw=12:"
        "enable='between(t,0,2)'"
    )

    _run([
        "ffmpeg", "-y",
        "-i", trimmed, "-i", audio,
        "-map", "0:v", "-map", "1:a",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", output,
    ])
