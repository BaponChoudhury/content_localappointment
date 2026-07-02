import os
import json
import subprocess
import tempfile
import threading
import asyncio

import streamlit as st


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list) -> None:
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode())


def _fetch_broll(keywords: list, key: str, work_dir: str) -> list:
    import requests
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
            with open(clip_path, "wb") as fh:
                for chunk in r.iter_content(8192):
                    fh.write(chunk)
            clips.append(clip_path)
        except Exception:
            continue
    return clips


def _assemble(clips, audio, hook, output, work_dir):
    processed = []
    for i, clip in enumerate(clips):
        out = os.path.join(work_dir, f"proc_{i}.mp4")
        _run([
            "ffmpeg", "-y", "-i", clip,
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-c:v", "libx264", "-preset", "fast", "-an", "-t", "10", out,
        ])
        processed.append(out)

    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", audio,
    ]))
    duration = float(probe["format"]["duration"])

    concat_txt = os.path.join(work_dir, "concat.txt")
    with open(concat_txt, "w") as fh:
        total = 0.0
        while total < duration:
            for p in processed:
                fh.write(f"file '{os.path.abspath(p)}'\n")
                total += 10
                if total >= duration:
                    break

    looped = os.path.join(work_dir, "looped.mp4")
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", looped])

    trimmed = os.path.join(work_dir, "trimmed.mp4")
    _run(["ffmpeg", "-y", "-i", looped, "-t", str(duration), "-c", "copy", trimmed])

    # Wrap hook text at ~30 chars per line
    words = hook.split()
    lines, line = [], []
    for word in words:
        line.append(word)
        if len(" ".join(line)) > 30:
            lines.append(" ".join(line[:-1]))
            line = [word]
    if line:
        lines.append(" ".join(line))
    wrapped_hook = "\n".join(lines).replace("'", "’").replace(":", "\\:")

    vf = (
        f"drawtext=text='{wrapped_hook}':"
        "fontsize=28:fontcolor=white:"
        "x=(w-text_w)/2:y=h/6:"
        "box=1:boxcolor=black@0.55:boxborderw=10:"
        "enable='between(t,0,2)'"
    )

    _run([
        "ffmpeg", "-y",
        "-i", trimmed, "-i", audio,
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", vf,
        "-c:v", "libx264", "-crf", "28", "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", output,
    ])


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="LocalAppointments — Video Generator",
    page_icon="🎬",
    layout="centered",
)

st.title("🎬 LocalAppointments Video Generator")
st.caption("Paste your script, fill in the details, and download a ready-to-post 9:16 video.")

try:
    pexels_key_preset = st.secrets["PEXELS_API_KEY"]
except Exception:
    pexels_key_preset = os.environ.get("PEXELS_API_KEY", "")

# ── Form ──────────────────────────────────────────────────────────────────────
with st.form("video_form"):
    script = st.text_area(
        "TikTok / Reels Script",
        height=220,
        placeholder="Paste the full script here...",
    )
    hook = st.text_input(
        "Hook Text (bold overlay, first 2 seconds)",
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

        progress.progress(20, text="🎙️  Generating voiceover...")
        try:
            import edge_tts
            audio_path = os.path.join(work_dir, "voice.mp3")
            tts_error = [None]

            async def _speak():
                communicate = edge_tts.Communicate(script, "en-GB-SoniaNeural")
                await communicate.save(audio_path)

            def _run_tts():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(_speak())
                    loop.close()
                except Exception as ex:
                    tts_error[0] = ex

            t = threading.Thread(target=_run_tts)
            t.start()
            t.join()

            if tts_error[0]:
                raise tts_error[0]
            if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
                raise RuntimeError("Audio file was not created — edge-tts may have failed silently.")
        except Exception as e:
            st.error(f"Voice generation failed: {e}")
            st.stop()

        progress.progress(50, text="🎬  Fetching B-roll from Pexels...")
        clips = _fetch_broll(keyword_list, pexels_key, work_dir)
        if not clips:
            st.error("No B-roll clips found. Check your Pexels API key and try different keywords.")
            st.stop()

        progress.progress(70, text="🎞️  Assembling video...")
        output_path = os.path.join(work_dir, "final_video.mp4")
        try:
            _assemble(clips, audio_path, hook.strip(), output_path, work_dir)
        except Exception as e:
            st.error(f"Video assembly failed: {e}")
            st.stop()

        progress.progress(100, text="✅  Done!")

        with open(output_path, "rb") as fh:
            video_bytes = fh.read()

    st.success("Your video is ready!")
    st.video(video_bytes)
    st.download_button(
        label="⬇️  Download MP4",
        data=video_bytes,
        file_name="localappointments_video.mp4",
        mime="video/mp4",
        use_container_width=True,
    )
