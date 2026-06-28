"""Product showcase media processing.

Turns whatever the store owner uploads into web-light, instantly-playable
files:
  - any image  -> .webp, short side normalised to 1024px (no upscaling, no
    cropping/stretching — the UI uses object-fit: cover, centered, for display).
  - any video  -> H.264 .mp4 (universal instant playback) capped to 720p-ish,
    plus a .webp poster frame. The original upload is discarded after a
    successful encode (only the optimised file is ever stored).

All limits here are the single source of truth — the API surfaces them to the
UI so the "what you can upload" note never drifts from what the server enforces.
"""

import io
import os
import json
import uuid
import tempfile
import subprocess

from PIL import Image, ImageOps
from django.core.files.base import ContentFile

# ── Limits (surfaced to the UI via the API) ──────────────────────────────
MAX_IMAGES        = 5                       # photos per product
MAX_IMAGE_BYTES   = 10 * 1024 * 1024        # 10 MB per photo (source)
MAX_VIDEO_BYTES   = 80 * 1024 * 1024        # 80 MB source upload
MAX_VIDEO_SECONDS = 60                      # 1 minute max length
TARGET_SHORT_SIDE = 1024                    # min(w,h) target for photos
VIDEO_LONG_SIDE   = 1280                    # cap the long side -> keeps files light
WEBP_QUALITY      = 82

ALLOWED_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}
ALLOWED_VIDEO_EXT = {'.mp4', '.mov', '.webm', '.mkv', '.avi', '.m4v'}


class MediaError(Exception):
    """A user-facing media problem — the message is safe to show in the UI."""


def upload_specs():
    """The limits, in a shape the frontend can render directly."""
    return {
        'max_images': MAX_IMAGES,
        'max_image_mb': MAX_IMAGE_BYTES // (1024 * 1024),
        'max_video_mb': MAX_VIDEO_BYTES // (1024 * 1024),
        'max_video_seconds': MAX_VIDEO_SECONDS,
        'image_formats': sorted(e.lstrip('.') for e in ALLOWED_IMAGE_EXT),
        'video_formats': sorted(e.lstrip('.') for e in ALLOWED_VIDEO_EXT),
        'output_image': 'webp',
        'output_video': 'mp4 (H.264)',
    }


# ── Images ────────────────────────────────────────────────────────────────
def process_image(django_file):
    """Convert an uploaded image to a web-light .webp.

    Returns (ContentFile, width, height). Raises MediaError on any problem.
    """
    if django_file.size > MAX_IMAGE_BYTES:
        raise MediaError(f"Image too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB).")

    try:
        img = Image.open(django_file)
        img = ImageOps.exif_transpose(img)          # honour phone orientation
    except Exception:
        raise MediaError("Could not read that image file.")

    # webp keeps alpha; flatten anything exotic to a safe mode
    if img.mode not in ('RGB', 'RGBA'):
        img = img.convert('RGBA' if 'A' in img.mode else 'RGB')

    w, h = img.size
    short = min(w, h)
    if short > TARGET_SHORT_SIDE:               # downscale only — never upscale (blur)
        scale = TARGET_SHORT_SIDE / short
        w, h = round(w * scale), round(h * scale)
        img = img.resize((w, h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format='WEBP', quality=WEBP_QUALITY, method=6)
    buf.seek(0)
    return ContentFile(buf.read(), name=f"{uuid.uuid4().hex}.webp"), w, h


# ── Video ───────────────────────────────────────────────────────────────────
def _ffprobe(path):
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-print_format', 'json',
             '-show_format', '-show_streams', path],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        raise MediaError("Could not read that video file.")
    if out.returncode != 0:
        raise MediaError("Could not read that video file.")
    data = json.loads(out.stdout or '{}')
    vstream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
    if not vstream:
        raise MediaError("That file has no video track.")
    w = int(vstream.get('width') or 0)
    h = int(vstream.get('height') or 0)
    dur = float(data.get('format', {}).get('duration') or vstream.get('duration') or 0)
    return w, h, dur


def process_video(django_file):
    """Encode an uploaded video to a light H.264 .mp4 + a .webp poster frame.

    The original upload lives only in a temp dir and is deleted in `finally`,
    so once we return only the optimised file survives. Returns
    dict(file, poster, width, height, duration). Raises MediaError on problems.
    """
    if django_file.size > MAX_VIDEO_BYTES:
        raise MediaError(f"Video too large (max {MAX_VIDEO_BYTES // (1024 * 1024)} MB).")

    tmpdir = tempfile.mkdtemp(prefix='vmedia_')
    src = os.path.join(tmpdir, 'src')
    out_mp4 = os.path.join(tmpdir, 'out.mp4')
    out_poster = os.path.join(tmpdir, 'poster.webp')
    try:
        with open(src, 'wb') as fh:
            for chunk in django_file.chunks():
                fh.write(chunk)

        w, h, dur = _ffprobe(src)
        if dur > MAX_VIDEO_SECONDS + 0.5:
            raise MediaError(f"Video too long (max {MAX_VIDEO_SECONDS}s).")
        if w <= 0 or h <= 0:
            raise MediaError("Could not read the video dimensions.")

        # Cap the long side, keep aspect, force even dimensions (H.264 needs even).
        long_side = max(w, h)
        if long_side > VIDEO_LONG_SIDE:
            s = VIDEO_LONG_SIDE / long_side
            tw, th = round(w * s), round(h * s)
        else:
            tw, th = w, h
        tw -= tw % 2
        th -= th % 2

        enc = subprocess.run(
            ['ffmpeg', '-y', '-i', src,
             '-vf', f'scale={tw}:{th}',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '25',
             '-profile:v', 'high', '-pix_fmt', 'yuv420p',
             '-movflags', '+faststart',
             '-c:a', 'aac', '-b:a', '128k',
             '-max_muxing_queue_size', '1024',
             out_mp4],
            capture_output=True, text=True, timeout=600,
        )
        if enc.returncode != 0 or not os.path.exists(out_mp4):
            raise MediaError("Video conversion failed.")

        # Poster frame ~0.5s in (or frame 0 for very short clips).
        ts = '0.5' if dur > 1 else '0'
        poster_cf = None
        pr = subprocess.run(
            ['ffmpeg', '-y', '-ss', ts, '-i', out_mp4, '-frames:v', '1',
             '-vf', f'scale={tw}:{th}', out_poster],
            capture_output=True, text=True, timeout=60,
        )
        if pr.returncode == 0 and os.path.exists(out_poster):
            with open(out_poster, 'rb') as fh:
                poster_cf = ContentFile(fh.read(), name=f"{uuid.uuid4().hex}.webp")

        with open(out_mp4, 'rb') as fh:
            file_cf = ContentFile(fh.read(), name=f"{uuid.uuid4().hex}.mp4")

        return {'file': file_cf, 'poster': poster_cf,
                'width': tw, 'height': th, 'duration': round(dur, 2)}
    finally:
        # Deletes the temp dir including the original upload.
        try:
            for fn in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, fn))
            os.rmdir(tmpdir)
        except Exception:
            pass
