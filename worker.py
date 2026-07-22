"""
The actual video processing worker. Runs as a SEPARATE Render service
(Background Worker), started with:
    celery -A worker worker --loglevel=info

This file must NOT be imported by main.py — it's a standalone process.
It imports celery_app (the shared queue config) and database/models to
read the job and write results, but never touches FastAPI directly.

IMPORTANT DEPLOY NOTE: the ffmpeg binary must be installed on whatever
Render service runs this file. Render's default Python build does NOT
include ffmpeg — this needs an explicit apt-get install step in the
service's build command, e.g.:
    apt-get update && apt-get install -y ffmpeg && pip install -r requirements.txt
"""
import os
import tempfile
import shutil
import subprocess
import requests as http_requests
import ffmpeg
import mux_python

from celery_app import celery_app
from database import SessionLocal
from models import VideoEditJob
from config import settings


def _download_clip(url: str, dest_path: str):
    """Downloads a source clip (already-uploaded video, e.g. from Mux or a direct URL) to local disk for processing."""
    resp = http_requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def _apply_filters(stream, filters: list):
    """
    Applies color/style filters in sequence. `filters` is a list of dicts
    like {"type": "brightness", "value": 0.1} — each maps to a real ffmpeg
    filter, not a fake/decorative one.
    """
    for f in filters:
        ftype = f.get("type")
        if ftype == "brightness":
            stream = stream.filter("eq", brightness=f.get("value", 0))
        elif ftype == "contrast":
            stream = stream.filter("eq", contrast=f.get("value", 1.0))
        elif ftype == "saturation":
            stream = stream.filter("eq", saturation=f.get("value", 1.0))
        elif ftype == "grayscale":
            stream = stream.filter("hue", s=0)
        elif ftype == "sepia":
            # Real sepia via colorchannelmixer — a documented, correct ffmpeg technique.
            stream = stream.filter(
                "colorchannelmixer",
                rr=0.393, rg=0.769, rb=0.189,
                gr=0.349, gg=0.686, gb=0.168,
                br=0.272, bg=0.534, bb=0.131,
            )
    return stream


def _apply_text_overlays(stream, overlays: list, video_duration: float):
    """
    overlays: [{"text": "...", "start": 0, "end": 3, "x": "center", "y": "top",
                "font_size": 32, "color": "white"}]
    Uses drawtext — the real, documented ffmpeg text overlay filter.
    """
    for o in overlays:
        x_expr = "(w-text_w)/2" if o.get("x") == "center" else str(o.get("x", 10))
        if o.get("y") == "top":
            y_expr = "40"
        elif o.get("y") == "bottom":
            y_expr = "h-th-40"
        elif o.get("y") == "center":
            y_expr = "(h-text_h)/2"
        else:
            y_expr = str(o.get("y", 40))

        start = o.get("start", 0)
        end = o.get("end", video_duration)
        text = o.get("text", "").replace("'", "\\'").replace(":", "\\:")

        stream = stream.filter(
            "drawtext",
            text=text,
            fontsize=o.get("font_size", 32),
            fontcolor=o.get("color", "white"),
            x=x_expr,
            y=y_expr,
            box=1,
            boxcolor="black@0.4",
            boxborderw=8,
            enable=f"between(t,{start},{end})",
        )
    return stream


def _build_clip_stream(local_path: str, trim_start: float, trim_end: float):
    """Loads one clip and trims it to the requested in/out points, resetting timestamps."""
    stream = ffmpeg.input(local_path)
    video = stream.video.filter("trim", start=trim_start, end=trim_end).filter("setpts", "PTS-STARTPTS")
    audio = stream.audio.filter("atrim", start=trim_start, end=trim_end).filter("asetpts", "PTS-STARTPTS")
    return video, audio


def _process_edit(job_id: str, edit_spec: dict, work_dir: str, local_clip_paths: list = None) -> str:
    """
    The real processing pipeline. edit_spec shape:
    {
      "clips": [{"source_url": "...", "trim_start": 0, "trim_end": 10}, ...],
      "transitions": [{"type": "fade", "duration": 0.5}, ...],  # between consecutive clips, len = len(clips)-1
      "text_overlays": [{"text": "...", "start": 0, "end": 3, ...}],
      "filters": [{"type": "saturation", "value": 1.2}],
      "music": {"source_url": "...", "volume": 0.3} | None,
    }
    Returns the local path to the final rendered file.

    `local_clip_paths`, if provided, skips downloading and uses these files
    directly (in the same order as edit_spec["clips"]) — used by tests.
    """
    clips = edit_spec.get("clips", [])
    if not clips:
        raise ValueError("edit_spec has no clips")

    # 1. Get every source clip locally — download unless paths were pre-supplied.
    if local_clip_paths:
        local_paths = local_clip_paths
    else:
        local_paths = []
        for i, clip in enumerate(clips):
            local_path = os.path.join(work_dir, f"clip_{i}.mp4")
            _download_clip(clip["source_url"], local_path)
            local_paths.append(local_path)

    # 2. Build a trimmed video/audio stream pair for each clip.
    video_streams = []
    audio_streams = []
    total_duration = 0
    for path, clip in zip(local_paths, clips):
        start, end = clip.get("trim_start", 0), clip.get("trim_end")
        if end is None:
            probe = ffmpeg.probe(path)
            end = float(probe["format"]["duration"])
        v, a = _build_clip_stream(path, start, end)
        video_streams.append(v)
        audio_streams.append(a)
        total_duration += (end - start)

    # 3. Concatenate clips — with real xfade/acrossfade transitions if more
    #    than one clip and transitions were requested, otherwise a plain concat.
    transitions = edit_spec.get("transitions", [])
    if len(video_streams) == 1:
        video = video_streams[0]
        audio = audio_streams[0]
    elif transitions:
        # xfade requires explicit offsets and only chains two streams at a
        # time, so fold left-to-right through the clip list.
        video = video_streams[0]
        audio = audio_streams[0]
        running_duration = clips[0].get("trim_end", total_duration) - clips[0].get("trim_start", 0)
        for i in range(1, len(video_streams)):
            t = transitions[i - 1] if i - 1 < len(transitions) else {"type": "fade", "duration": 0.5}
            dur = t.get("duration", 0.5)
            offset = max(0, running_duration - dur)
            video = ffmpeg.filter([video, video_streams[i]], "xfade", transition=t.get("type", "fade"), duration=dur, offset=offset)
            audio = ffmpeg.filter([audio, audio_streams[i]], "acrossfade", d=dur)
            next_dur = clips[i].get("trim_end", 0) - clips[i].get("trim_start", 0)
            running_duration = offset + next_dur
        total_duration = running_duration
    else:
        # No transitions requested — plain concat, interleaving video/audio pairs.
        joined = ffmpeg.concat(*[x for pair in zip(video_streams, audio_streams) for x in pair], v=1, a=1)
        video = joined[0]
        audio = joined[1]

    # 4. Apply filters (color/style adjustments).
    filters = edit_spec.get("filters", [])
    if filters:
        video = _apply_filters(video, filters)

    # 5. Apply text overlays.
    overlays = edit_spec.get("text_overlays", [])
    if overlays:
        video = _apply_text_overlays(video, overlays, total_duration)

    # 6. Mix in background music if requested — real amix, ducks original
    #    audio to a lower volume rather than replacing it outright, so
    #    dialogue/original sound isn't just wiped out.
    music = edit_spec.get("music")
    if music:
        music_path = os.path.join(work_dir, "music.mp3")
        _download_clip(music["source_url"], music_path)
        music_stream = ffmpeg.input(music_path).audio.filter("atrim", end=total_duration).filter("volume", music.get("volume", 0.3))
        audio = ffmpeg.filter([audio, music_stream], "amix", inputs=2, duration="first")

    output_path = os.path.join(work_dir, f"{job_id}_final.mp4")
    (
        ffmpeg
        .output(video, audio, output_path, vcodec="libx264", acodec="aac", **{"movflags": "faststart"})
        .overwrite_output()
        .run(quiet=True)
    )
    return output_path


def _upload_result_to_mux(local_path: str) -> dict:
    """Uploads the finished render to Mux the same way live-recorded videos already go — reuses the existing pipeline."""
    config = mux_python.Configuration()
    config.username = settings.mux_token_id
    config.password = settings.mux_token_secret
    api_client = mux_python.ApiClient(config)
    uploads_api = mux_python.DirectUploadsApi(api_client)

    asset_settings = mux_python.CreateAssetRequest(playback_policy=["public"])
    upload_request = mux_python.CreateUploadRequest(cors_origin="*", new_asset_settings=asset_settings)
    response = uploads_api.create_direct_upload(upload_request)
    upload = response.data

    with open(local_path, "rb") as f:
        put_resp = http_requests.put(upload.url, data=f, headers={"Content-Type": "video/mp4"}, timeout=300)
        put_resp.raise_for_status()

    return {"upload_id": upload.id}


@celery_app.task(bind=True, name="process_video_edit")
def process_video_edit(self, job_id: str):
    """
    The actual Celery task. Reads the job from the database, runs the
    edit pipeline, uploads the result to Mux, and writes the outcome
    back to the same row the frontend is polling.
    """
    db = SessionLocal()
    work_dir = tempfile.mkdtemp(prefix=f"videoedit_{job_id}_")
    try:
        job = db.query(VideoEditJob).filter(VideoEditJob.id == job_id).first()
        if not job:
            return

        job.status = "processing"
        db.commit()

        output_path = _process_edit(job_id, job.edit_spec, work_dir)
        upload_result = _upload_result_to_mux(output_path)

        job.status = "done"
        job.result_mux_upload_id = upload_result["upload_id"]
        db.commit()

    except Exception as e:
        db.rollback()
        job = db.query(VideoEditJob).filter(VideoEditJob.id == job_id).first()
        if job:
            job.status = "failed"
            job.error_message = str(e)
            db.commit()
        raise

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        db.close()
