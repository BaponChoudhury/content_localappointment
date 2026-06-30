#!/usr/bin/env python3
"""
LocalAppointments — Faceless Video Pipeline

Takes a TikTok script and produces a ready-to-post 9:16 MP4.

Steps automated:
  1. Voice      — gTTS (British English, free, no API key)
  2. Captions   — OpenAI Whisper (local, free)
  3. B-roll     — Pexels API (free key from pexels.com/api)
  4. Assembly   — FFmpeg (hook text overlay + captions + audio + export 9:16)

Install:
  pip install gtts openai-whisper requests
  (FFmpeg must be installed on your system)

Usage:
  python video_pipeline.py \
    --script "Your script here..." \
    --hook "Every missed call is a booking lost." \
    --keywords "missed call,salon,dog grooming,AI booking" \
    --pexels-key YOUR_PEXELS_KEY \
    --output my_video.mp4

  Or pass a text file:
    --script-file week1_script.txt
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile


def generate_voice(script_text: str, output_path: str) -> str:
    from gtts import gTTS
    print("[1/4] Generating voice (British English)...")
    tts = gTTS(text=script_text, lang="en", tld="co.uk")
    tts.save(output_path)
    return output_path


def generate_captions(audio_path: str, work_dir: str) -> str:
    print("[2/4] Transcribing audio for captions (Whisper)...")
    import whisper
    model = whisper.load_model("base")
    result = model.transcribe(audio_path)

    srt_path = os.path.join(work_dir, "captions.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(result["segments"], 1):
            f.write(f"{i}\n")
            f.write(f"{_srt_time(seg['start'])} --> {_srt_time(seg['end'])}\n")
            f.write(f"{seg['text'].strip()}\n\n")
    return srt_path


def _srt_time(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    ms = int((s % 1) * 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def fetch_broll(keywords: list, pexels_key: str, work_dir: str, count: int = 4) -> list:
    print(f"[3/4] Fetching {count} B-roll clips from Pexels...")
    headers = {"Authorization": pexels_key}
    clips = []

    for i, keyword in enumerate(keywords[:count]):
        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers=headers,
            params={"query": keyword, "orientation": "portrait", "per_page": 3, "size": "medium"},
        )
        data = resp.json()
        videos = data.get("videos", [])
        if not videos:
            print(f"   No clip found for '{keyword}', skipping")
            continue

        # Pick highest-quality portrait file available
        video_files = [
            f for f in videos[0]["video_files"]
            if f.get("width", 0) > 0 and f.get("height", 0) > f.get("width", 0)
        ]
        if not video_files:
            video_files = videos[0]["video_files"]
        video_files.sort(key=lambda x: x.get("width", 0), reverse=True)
        file_url = video_files[0]["link"]

        clip_path = os.path.join(work_dir, f"broll_{i}.mp4")
        print(f"   Downloading '{keyword}'...")
        r = requests.get(file_url, stream=True)
        with open(clip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        clips.append(clip_path)

    return clips


def assemble_video(
    broll_clips: list,
    audio_path: str,
    srt_path: str,
    hook_text: str,
    output_path: str,
    work_dir: str,
) -> str:
    print("[4/4] Assembling video with FFmpeg...")

    # Resize each B-roll clip to 1080x1920 (9:16), 10s max, no audio
    processed = []
    for i, clip in enumerate(broll_clips):
        out = os.path.join(work_dir, f"proc_{i}.mp4")
        _run([
            "ffmpeg", "-y", "-i", clip,
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-c:v", "libx264", "-preset", "fast", "-an", "-t", "10",
            out,
        ])
        processed.append(out)

    # Get audio duration
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", audio_path,
    ]))
    duration = float(probe["format"]["duration"])

    # Concat B-roll, looping until audio ends
    concat_txt = os.path.join(work_dir, "concat.txt")
    with open(concat_txt, "w") as f:
        total = 0.0
        while total < duration:
            for clip in processed:
                f.write(f"file '{os.path.abspath(clip)}'\n")
                total += 10
                if total >= duration:
                    break

    looped = os.path.join(work_dir, "looped.mp4")
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", looped])

    trimmed = os.path.join(work_dir, "trimmed.mp4")
    _run(["ffmpeg", "-y", "-i", looped, "-t", str(duration), "-c", "copy", trimmed])

    # Build FFmpeg filter: captions + hook text overlay (0–2s)
    safe_hook = hook_text.replace("'", "’").replace(":", "\\:")
    abs_srt = os.path.abspath(srt_path).replace("\\", "/").replace(":", "\\:")

    vf = (
        f"subtitles='{abs_srt}':force_style='"
        "FontName=Arial,FontSize=16,Bold=1,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "Outline=2,Alignment=2,MarginV=100',"
        f"drawtext=text='{safe_hook}':"
        "fontsize=48:fontcolor=white:"
        "x=(w-text_w)/2:y=h/5:"
        "box=1:boxcolor=black@0.6:boxborderw=14:"
        "enable='between(t,0,2)'"
    )

    _run([
        "ffmpeg", "-y",
        "-i", trimmed,
        "-i", audio_path,
        "-map", "0:v", "-map", "1:a",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        output_path,
    ])

    print(f"\n✅  Done! Video saved to: {output_path}")
    return output_path


def _run(cmd: list) -> None:
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        print(f"FFmpeg error:\n{result.stderr.decode()}")
        sys.exit(1)


def main():
    global requests
    import requests as _requests
    requests = _requests

    parser = argparse.ArgumentParser(description="LocalAppointments Faceless Video Pipeline")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--script", type=str, help="Script text (wrap in quotes)")
    group.add_argument("--script-file", type=str, help="Path to .txt file with script")
    parser.add_argument("--hook", required=True, help="Bold hook text shown in first 2 seconds")
    parser.add_argument("--keywords", required=True, help="Comma-separated Pexels search terms")
    parser.add_argument("--pexels-key", required=True, help="Pexels API key (free at pexels.com/api)")
    parser.add_argument("--output", default="output_video.mp4", help="Output file name")
    args = parser.parse_args()

    script_text = args.script
    if args.script_file:
        with open(args.script_file, encoding="utf-8") as f:
            script_text = f.read()

    keywords = [k.strip() for k in args.keywords.split(",")]

    with tempfile.TemporaryDirectory() as work_dir:
        audio = generate_voice(script_text, os.path.join(work_dir, "voice.mp3"))
        srt = generate_captions(audio, work_dir)
        clips = fetch_broll(keywords, args.pexels_key, work_dir)

        if not clips:
            print("Error: no B-roll downloaded. Check your Pexels API key and keywords.")
            sys.exit(1)

        assemble_video(clips, audio, srt, args.hook, args.output, work_dir)


if __name__ == "__main__":
    main()
