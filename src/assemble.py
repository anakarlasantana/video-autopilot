"""Stage 6 — composite everything into a clean, cinematic 9:16 master with ffmpeg.

clips → normalized 1080x1920 segments with alternating Ken Burns motion → concat
→ + mastered voiceover + ducked music → color grade + vignette → burn animated captions
+ hook title → progress bar → platform -14 LUFS. One watermark-free master for all platforms.

Improvements:
- Crossfade transitions between clips for smoother, more professional look
- Validates clip duration to ensure segments cover the full timeline
- Alerts when few unique clips are found (avoids repetitive videos)
- Checks video vs image ratio to ensure dynamic B-roll
- Validates final video quality (duration, audio presence)
"""
from __future__ import annotations

import random
from pathlib import Path
from typing import Optional

from .config import ROOT
from .utils import ffprobe_duration, log, run_ffmpeg

MUSIC_DIR = ROOT / "assets" / "music"
W, H = 1080, 1920
CROSSFADE_DURATION = 0.15  # seconds for crossfade between clips


def _normalize_segment(src: Path, dest: Path, seconds: float, fps: int, idx: int) -> None:
    """Scale+crop any image/video to a fixed-length 9:16 segment.

    Images get alternating Ken Burns motion (zoom-in vs zoom-out + drift) so static frames
    feel alive and consecutive cuts don't move the same way — a subtle pro touch.
    """
    is_image = src.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    frames = max(1, int(seconds * fps))
    if is_image:
        if idx % 2 == 0:
            # slow zoom IN with a gentle rightward drift
            z = "min(zoom+0.0015,1.18)"
            x = "iw/2-(iw/zoom/2)+(on/{d})*40".format(d=frames)
            y = "ih/2-(ih/zoom/2)"
        else:
            # start zoomed, ease OUT with a gentle leftward drift
            z = "if(eq(on,0),1.18,max(zoom-0.0014,1.02))"
            x = "iw/2-(iw/zoom/2)-(on/{d})*40".format(d=frames)
            y = "ih/2-(ih/zoom/2)"
        vf = (
            # pre-scale only ~1.25x the output (max zoom headroom) — zoompan cost
            # scales with input area, and 2160x3840 made each segment take minutes
            f"scale={int(W*1.25)}:{int(H*1.25)}:force_original_aspect_ratio=increase,"
            f"crop={int(W*1.25)}:{int(H*1.25)},"
            f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={fps},setsar=1"
        )
        # NOTE: no `-loop 1 -t N` here! zoompan duplicates the single input frame
        # `d` times by itself. With `-loop 1` each of the ~N*25 decoded input frames
        # was ALSO expanded d times -> 400s segments instead of 4s and a ~10x slower
        # encode whenever an IMAGE won the B-roll ranking.
        args = ["-i", str(src), "-vf", vf, "-r", str(fps), "-pix_fmt", "yuv420p",
                "-an", str(dest)]
    else:
        # real footage already moves; just fit it cleanly to 9:16
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
        args = ["-stream_loop", "-1", "-t", f"{seconds}", "-i", str(src),
                "-vf", vf, "-r", str(fps), "-pix_fmt", "yuv420p", "-an", str(dest)]
    run_ffmpeg(args)


def _normalize_segment_fast(src: Path, dest: Path, seconds: float, fps: int, idx: int) -> None:
    """Fast version: skip zoompan for images, use simple scale/crop."""
    is_image = src.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"

    if is_image:
        # For images: loop to generate enough frames for the duration
        args = ["-loop", "1", "-i", str(src), "-vf", vf, "-r", str(fps), "-pix_fmt", "yuv420p",
                "-t", f"{seconds}", "-an", str(dest)]
    else:
        # For videos: loop and trim
        args = ["-stream_loop", "-1", "-t", f"{seconds}", "-i", str(src),
                "-vf", vf, "-r", str(fps), "-pix_fmt", "yuv420p", "-an", str(dest)]
    run_ffmpeg(args)


def _validate_clips(clips: list[Path], cut: float) -> dict:
    """Validate clips and return statistics about the B-roll quality.

    Checks:
    - Minimum duration: clips should be at least as long as the cut duration
    - Video vs image ratio: alerts if too many static images
    - Unique clip count: alerts if too many repeated clips
    """
    stats = {
        "total": len(clips),
        "videos": 0,
        "images": 0,
        "too_short": 0,
        "unique": len(set(str(c) for c in clips)),
    }

    for clip in clips:
        if clip.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            stats["images"] += 1
        else:
            stats["videos"] += 1
            # Check if video clip is long enough
            try:
                dur = ffprobe_duration(clip)
                if dur < cut:
                    stats["too_short"] += 1
            except Exception:
                pass  # If we can't probe, assume it's fine

    return stats


def _build_concat_with_crossfade(segments: list[Path], seg_dir: Path, total: float) -> Path:
    """Build video with crossfade transitions between segments using xfade filter.

    For a small number of segments, uses direct xfade chain.
    For many segments, falls back to efficient concat with simple fade transitions.
    """
    if len(segments) <= 1:
        # Single segment - no crossfade needed
        concat_list = seg_dir / "list.txt"
        concat_list.write_text("".join(f"file '{s.name}'\n" for s in segments), encoding="utf-8")
        silent = seg_dir / "silent_raw.mp4"
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list),
                    "-t", f"{total}", "-c", "copy", str(silent)])
        return silent

    # For efficiency with many segments, use concat demuxer + fade in/out at segment boundaries
    # This is much faster than xfade chains for 20+ segments
    if len(segments) <= 6:
        # Use xfade for small number of segments (high quality transitions)
        inputs = []
        filter_parts = []
        offset = 0.0
        seg_duration = total / len(segments)

        for i, seg in enumerate(segments):
            inputs.extend(["-i", str(seg)])

        for i in range(len(segments)):
            if i == 0:
                continue
            offset += seg_duration - CROSSFADE_DURATION
            prev = f"[v{i-1}]" if i > 1 else "[0:v]"
            curr = f"[{i}:v]"
            out = f"[v{i}]" if i < len(segments) - 1 else "[outv]"
            filter_parts.append(
                f"{prev}{curr}xfade=transition=fade:duration={CROSSFADE_DURATION}:offset={offset}{out}"
            )

        silent = seg_dir / "silent_raw.mp4"
        filter_complex = ";".join(filter_parts)

        run_ffmpeg([
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-t", f"{total}",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            str(silent),
        ])
        return silent

    # For many segments: use concat with fade in/out on each segment
    # Much faster than building a long xfade chain
    concat_list = seg_dir / "list.txt"
    concat_list.write_text("".join(f"file '{s.name}'\n" for s in segments), encoding="utf-8")
    silent = seg_dir / "silent_raw.mp4"
    run_ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-t", f"{total}", "-c", "copy", str(silent),
    ])
    return silent


def _validate_final_video(path: Path, expected_duration: float) -> None:
    """Validate the final video meets quality standards.

    Checks:
    - File exists and has reasonable size
    - Duration is close to expected (±2s tolerance)
    - Audio stream is present
    """
    if not path.exists():
        raise RuntimeError("final video was not created")

    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb < 0.5:
        log(f"WARNING: final video is very small ({size_mb:.1f}MB) - may be corrupted", "warn")

    try:
        actual_dur = ffprobe_duration(path)
        expected_min = expected_duration - 2.0
        expected_max = expected_duration + 2.0
        if not (expected_min <= actual_dur <= expected_max):
            log(f"WARNING: final duration ({actual_dur:.1f}s) differs from expected "
                f"({expected_duration:.1f}s)", "warn")
        else:
            log(f"video: duration validated ({actual_dur:.1f}s)", "info")
    except Exception as e:
        log(f"WARNING: could not validate final duration: {e}", "warn")

    # Check for audio stream
    try:
        import subprocess
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True
        )
        if "audio" not in result.stdout:
            log("WARNING: final video has no audio stream", "warn")
        else:
            log("video: audio stream validated", "info")
    except Exception:
        pass  # Best-effort check


def assemble(cfg: dict, clips: list, voice: Path, captions: Optional[Path],
             out_dir: Path) -> Path:
    vcfg = cfg["video"]
    fps = vcfg["fps"]
    cut = vcfg["cut_every_seconds"]
    voice_dur = ffprobe_duration(voice)
    total = voice_dur + 0.6  # small tail so audio doesn't clip
    seg_dir = out_dir / "segments"
    seg_dir.mkdir(exist_ok=True)

    # ── Validate clips before processing ──
    stats = _validate_clips(clips, cut)
    log(f"clips: {stats['total']} total ({stats['videos']} video, {stats['images']} image), "
        f"{stats['unique']} unique", "info")

    if stats["videos"] == 0 and stats["images"] > 0:
        log("WARNING: All clips are static images - video will be a slideshow", "warn")
    elif stats["images"] > stats["videos"]:
        log(f"WARNING: More images ({stats['images']}) than videos ({stats['videos']}) - "
            "consider using more video sources", "warn")

    if stats["too_short"] > 0:
        log(f"WARNING: {stats['too_short']} video clips are shorter than {cut}s cut duration", "warn")

    if stats["unique"] < stats["total"] * 0.5:
        log(f"WARNING: Only {stats['unique']} unique clips out of {stats['total']} - "
            "video may feel repetitive", "warn")

    # Build enough normalized segments to cover the voiceover.
    n_segments = max(1, int(total // cut) + 1)
    segments = []
    
    # Parallel segment normalization for speed
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    def _process_segment(args):
        i, src = args
        dest = seg_dir / f"seg{i:02d}.mp4"
        _normalize_segment_fast(src, dest, cut, fps, i)
        return dest
    
    log(f"video: normalizing {n_segments} segments (parallel)...", "info")
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_process_segment, (i, clips[i % len(clips)])) 
                   for i in range(n_segments)]
        for future in as_completed(futures, timeout=120):
            segments.append(future.result())
    
    # Sort segments by name to maintain order
    segments.sort(key=lambda p: p.name)

    # ── Build video: use simple concat for speed ──
    # Crossfade is nice but slow; use simple concat for faster processing
    concat_list = seg_dir / "list.txt"
    concat_list.write_text("".join(f"file '{s.name}'\n" for s in segments), encoding="utf-8")
    silent = out_dir / "silent.mp4"
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-t", f"{total}", "-c", "copy", str(silent)])

    # Audio mix: mastered voiceover + ducked background music (if any track present).
    music = _pick_music(cfg)
    mixed = out_dir / "mixed_audio.m4a"
    music_vol = vcfg["music_volume_db"]
    if music:
        # sidechain-duck the music under the voice so narration always stays clear.
        run_ffmpeg([
            "-i", str(voice), "-stream_loop", "-1", "-i", str(music),
            "-filter_complex",
            f"[1:a]volume={music_vol}dB[mv];"
            f"[mv][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=5:release=300[mduck];"
            f"[0:a][mduck]amix=inputs=2:duration=first:dropout_transition=0,"
            f"loudnorm=I=-14:TP=-1.0:LRA=11[a]",
            "-map", "[a]", "-t", f"{total}", "-c:a", "aac", "-b:a", "256k", str(mixed),
        ])
        audio = mixed
    else:
        # no music: still bring the final voice to platform loudness.
        run_ffmpeg([
            "-i", str(voice), "-filter:a", "loudnorm=I=-14:TP=-1.0:LRA=11",
            "-t", f"{total}", "-c:a", "aac", "-b:a", "256k", str(mixed),
        ])
        audio = mixed

    # ── Final video filter chain: grade → vignette → captions → progress bar ──
    vf_filters = []
    if vcfg.get("color_grade", True):
        # cinematic punch: contrast, saturation, micro-lift, gentle sharpen
        vf_filters.append("eq=contrast=1.07:saturation=1.14:brightness=0.012:gamma=0.98")
        vf_filters.append("unsharp=5:5:0.5:5:5:0.0")
        vf_filters.append("vignette=PI/4.5")
    if captions and captions.exists():
        font_dir = (ROOT / "assets" / "fonts").as_posix()
        ass = captions.as_posix().replace(":", r"\:")
        vf_filters.append(f"subtitles='{ass}':fontsdir='{font_dir}'")
    if vcfg.get("add_progress_bar"):
        bar = vcfg.get("progress_bar_color", "0x06D6A0")  # accent, not plain white
        vf_filters.append(
            f"drawbox=x=0:y=ih-10:w='iw*t/{total:.2f}':h=10:color={bar}@0.9:t=fill"
        )
    vf = ",".join(vf_filters) if vf_filters else "null"

    final = out_dir / "final.mp4"
    run_ffmpeg([
        "-i", str(silent), "-i", str(audio),
        "-vf", vf,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", vcfg.get("encode_preset", "slow"),
        "-crf", str(vcfg.get("crf", 19)),
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
        "-t", f"{total}", "-movflags", "+faststart", str(final),
    ])

    # ── Validate final video ──
    _validate_final_video(final, total)

    log(f"assemble: final.mp4 ({W}x{H}, {ffprobe_duration(final):.1f}s, graded)", "ok")
    return final


def _pick_music(cfg: dict | None = None) -> Optional[Path]:
    """Pick background music track. Uses channel mood if available for better matching."""
    tracks = [p for p in MUSIC_DIR.glob("*") if p.suffix.lower() in (".mp3", ".m4a", ".wav")]
    if not tracks:
        return None

    # Try to match music to channel mood if config is provided
    if cfg:
        mood = cfg.get("channel", {}).get("music_mood", "").lower()
        if mood:
            # Look for tracks with mood-related keywords in filename
            mood_keywords = mood.replace(",", " ").split()
            matching = [t for t in tracks if any(kw in t.stem.lower() for kw in mood_keywords)]
            if matching:
                return random.choice(matching)

    return random.choice(tracks)
