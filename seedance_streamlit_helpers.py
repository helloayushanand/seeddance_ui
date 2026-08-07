"""Shared helpers for Seedance 2.0 Streamlit test UI and scripts."""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

ARK_BASE_URL = os.environ.get(
    "ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3"
)
SEEDANCE_MODEL = os.environ.get(
    "SEEDANCE_MODEL", "dreamina-seedance-2-5-260628"
)
GEMINI_VIDEO_MODEL = os.environ.get("GEMINI_VIDEO_MODEL", "gemini-3.5-flash")
FILE_SERVER_URL = os.environ.get(
    "FILE_SERVER_URL", "https://file-service-g-p.sourcerer.tech/files/upload"
)
DIRECTUS_ASSET_URL = os.environ.get(
    "FILE_DIRECTUS_URL", "https://directus-g-p.sourcerer.tech/assets"
)

# Video compression defaults (override via env)
VIDEO_COMPRESS_MAX_HEIGHT = int(os.environ.get("VIDEO_COMPRESS_MAX_HEIGHT", "720"))
VIDEO_COMPRESS_MAX_FPS = int(os.environ.get("VIDEO_COMPRESS_MAX_FPS", "24"))
VIDEO_COMPRESS_CRF = int(os.environ.get("VIDEO_COMPRESS_CRF", "28"))
VIDEO_COMPRESS_MAX_WIDTH = int(os.environ.get("VIDEO_COMPRESS_MAX_WIDTH", "1280"))
VIDEO_UPLOAD_MAX_MB = float(os.environ.get("VIDEO_UPLOAD_MAX_MB", "30"))

STYLE_VIDEO_SYSTEM = """You are a cinematographer analyzing a reference video for style and camera language only.
Do NOT describe products, brands, logos, or specific scene objects. Focus on how the video is shot.

From the reference video, extract:
- Camera movements (push-in, pull-back, orbit, pan, tilt, handheld vs locked, speed)
- Shot types and framing (wide, medium, close-up, macro) and how they change over time
- Pacing and rhythm (cuts vs continuous shot, approximate beat changes)
- Lighting and color mood (directional, soft, high contrast, palette)
- Overall cinematic style (commercial, editorial, documentary, etc.)

Output ONE continuous paragraph suitable as a Seedance video-generation prompt section.
Use vivid cinematic language. No headings, bullets, timestamps, or meta commentary.
Start with phrasing like "The camera begins..." when describing motion."""

VIDEO_SCREEN_SYSTEM = """You are analyzing a reference video for production planning.
Watch the full video and respond with JSON only (no markdown, no extra text):

{
  "has_humans": true or false,
  "has_voiceover": true or false,
  "has_dialogue": true or false,
  "human_notes": "one short sentence if humans visible, else empty string",
  "voiceover_notes": "one short sentence if narration/VO exists, else empty string",
  "dialogue_notes": "one short sentence if any spoken words (on-screen or VO), else empty string"
}

Rules:
- has_humans: true if any visible person, face, or clearly human hands/body appear on screen.
- has_voiceover: true if off-screen narration or voiceover is heard (not on-screen lip-sync).
- has_dialogue: true if ANY spoken words exist (on-screen dialogue, narration, VO, or announcer).
- Be conservative: if uncertain, use false."""

VIDEO_HUMAN_VO_DETAILS_SYSTEM = """You are analyzing a reference video that contains humans and/or voiceover.
Extract production details to guide a NEW product video generation prompt.

Screening result from prior analysis:
{screen_json}

Output ONE continuous paragraph for a video generation model covering ONLY what applies:

If humans are present:
- How many, how they appear (silhouette, hands, full body), what they do, and how they relate to the product space.
- Do NOT include names, celebrity likeness, or identifiable personal features.
- Default instruction: omit humans in the new video unless the user explicitly wants them.

If voiceover is present:
- Tone, pace, gender presentation of voice, language, and delivery style.
- Note timing: when VO starts/stops relative to visuals, and whether audio should be generated.

Do NOT list dialogue line-by-line here (handled separately).
Do NOT describe camera moves, shot types, or lighting (handled separately).
Use vivid but concise cinematic language. No headings or bullet lists."""

VIDEO_DIALOGUE_FRAME_SYSTEM = """You are adapting spoken content from a reference video for a NEW product video.
Use the reference product image to know what product to feature in every shot beat.

Screening result:
{screen_json}

Extract ALL spoken content from the reference video — narration, voiceover, and on-screen dialogue — beat by beat in chronological order.

For EACH beat output exactly this pattern on its own line:

Beat N — [how the product from the reference image is framed on screen during this line, e.g. hero front shot, macro dial, three-quarter on pedestal]: "spoken line"

Rules:
- Include every spoken beat that exists in the reference video; do not skip lines.
- Rewrite each line so it promotes the NEW product shown in the reference image (use its visible type/category; do not copy the original brand unless visible on the image).
- Keep each quoted line concise (≤ 10 words) for video generation.
- Map framing to the product in the reference image, not the original video's product.
- If a person speaks on screen, write "Character says:" before the quote; if voiceover only, write "Voiceover:".
- Preserve the original pacing structure (same number of beats, same order).
- If no spoken words exist in the video, output exactly: NONE

Example format (do not copy content):
Beat 1 — Voiceover over tight hero shot of the product from the reference image, centered on white: "Crafted for those who notice details."
Beat 2 — Slow push-in on the product logo area from the reference image: "Precision you can feel."
"""

EXACT_DEFAULT_PROMPT = (
    "Replace the product in the reference video with the product shown in the reference image. "
    "Keep the same background, lighting, camera motion, pacing, and shot structure as the reference video. "
    "The new product must match the reference image exactly in shape, color, and branding."
)


def ark_api_key() -> Optional[str]:
    return os.environ.get("ARK_API_KEY") or os.environ.get("BYTEPLUS_API_KEY")


def gemini_api_key() -> Optional[str]:
    return os.environ.get("GEMINI_API_KEY")


def get_ark_client():
    key = ark_api_key()
    if not key:
        raise RuntimeError(
            "Missing ARK_API_KEY or BYTEPLUS_API_KEY. "
            "https://console.byteplus.com/ark/region:ark+ap-southeast-1/apikey"
        )
    from byteplussdkarkruntime import Ark

    return Ark(base_url=ARK_BASE_URL, api_key=key)


def _guess_image_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/png")


def _guess_video_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".mpeg": "video/mpeg",
        ".mpg": "video/mpeg",
    }.get(ext, "video/mp4")


def _guess_media_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
        return _guess_image_mime(path)
    if ext in {".mp4", ".mov", ".webm", ".mpeg", ".mpg"}:
        return _guess_video_mime(path)
    return _guess_image_mime(path)


def _is_image_mime(mime: str) -> bool:
    return (mime or "").lower().startswith("image/")


def file_to_data_url(path: str, *, kind: str) -> str:
    mime = _guess_video_mime(path) if kind == "video" else _guess_image_mime(path)
    with open(path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def is_http_url(url: Optional[str]) -> bool:
    return bool(url and str(url).strip().lower().startswith(("http://", "https://")))


def upload_to_file_service(local_path: str, mime: str) -> str:
    """Upload to sourcerer file service → public Directus asset URL."""
    import requests

    fname = os.path.basename(local_path) or "upload.bin"
    with open(local_path, "rb") as f:
        data = f.read()

    size_mb = len(data) / (1024 * 1024)
    timeout = min(600, max(180, int(size_mb * 45)))

    if _is_image_mime(mime):
        fields = ("image", "file")
    else:
        fields = ("file", "video", "image")

    last_err: Optional[Exception] = None
    for field in fields:
        try:
            resp = requests.post(
                FILE_SERVER_URL,
                files={field: (fname, data, mime)},
                timeout=timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            asset_id = (body.get("data") or {}).get("id")
            if asset_id:
                return f"{DIRECTUS_ASSET_URL}/{asset_id}"
            raise RuntimeError(f"Unexpected upload response: {body}")
        except Exception as e:
            last_err = e
    raise RuntimeError(f"File service upload failed: {last_err}")


def _extract_ark_file_url(data: dict) -> Optional[str]:
    for url_key in ("url", "download_url", "public_url", "file_url"):
        val = data.get(url_key)
        if is_http_url(val):
            return str(val).strip()
    return None


def upload_to_ark_files(local_path: str, mime: str) -> str:
    """Upload via BytePlus Ark Files API; return a public HTTPS URL if available."""
    import requests

    key = ark_api_key()
    if not key:
        raise RuntimeError("Missing ARK_API_KEY for Ark file upload")

    fname = os.path.basename(local_path) or "upload.bin"
    with open(local_path, "rb") as f:
        resp = requests.post(
            f"{ARK_BASE_URL}/files",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (fname, f, mime)},
            data={"purpose": "user_data"},
            timeout=300,
        )
    if resp.status_code >= 400:
        raise RuntimeError(resp.text[:500])

    data = resp.json()
    url = _extract_ark_file_url(data)
    if url:
        return url

    file_id = data.get("id")
    if not file_id:
        raise RuntimeError(f"Ark file upload returned no public URL: {data}")

    # Ark often returns status=processing before the URL is ready.
    deadline = time.time() + 300
    while time.time() < deadline:
        meta_resp = requests.get(
            f"{ARK_BASE_URL}/files/{file_id}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=60,
        )
        if meta_resp.ok:
            meta = meta_resp.json()
            url = _extract_ark_file_url(meta)
            if url:
                return url
            status = str(meta.get("status") or "").lower()
            if status in {"failed", "error", "deleted", "expired"}:
                raise RuntimeError(f"Ark file processing failed: {meta}")
        time.sleep(3)

    raise RuntimeError(f"Ark file upload timed out waiting for URL: {data}")


def upload_local_file_to_public_url(
    local_path: str,
    *,
    mime: Optional[str] = None,
    media_kind: str = "auto",
) -> Tuple[str, str]:
    """Upload local file and return (https_url, upload_method)."""
    if mime:
        resolved_mime = mime
    elif media_kind == "image":
        resolved_mime = _guess_image_mime(local_path)
    elif media_kind == "video":
        resolved_mime = _guess_video_mime(local_path)
    else:
        resolved_mime = _guess_media_mime(local_path)

    errors: List[str] = []
    for method, fn in (
        ("file_service", upload_to_file_service),
        ("ark_files", upload_to_ark_files),
    ):
        try:
            return fn(local_path, resolved_mime), method
        except Exception as e:
            errors.append(f"{method}: {e}")

    label = "image" if _is_image_mime(resolved_mime) else "file"
    raise RuntimeError(
        f"Could not upload {label} to a public HTTPS URL. " + " | ".join(errors)
    )


def download_url_to_temp(url: str, suffix: str = ".mp4") -> str:
    import urllib.request

    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    urllib.request.urlretrieve(url.strip(), path)
    return path


def _file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


def _find_ffmpeg() -> Optional[str]:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _compress_video_ffmpeg(input_path: str, output_path: str) -> None:
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    scale = (
        f"scale='min({VIDEO_COMPRESS_MAX_WIDTH},iw)':min({VIDEO_COMPRESS_MAX_HEIGHT},ih):"
        "force_original_aspect_ratio=decrease"
    )
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        input_path,
        "-vf",
        scale,
        "-r",
        str(VIDEO_COMPRESS_MAX_FPS),
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        str(VIDEO_COMPRESS_CRF),
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-movflags",
        "+faststart",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "ffmpeg failed")


def _compress_video_opencv(input_path: str, output_path: str) -> None:
    import cv2  # type: ignore

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {input_path}")

    src_fps = cap.get(cv2.CAP_PROP_FPS) or float(VIDEO_COMPRESS_MAX_FPS)
    src_fps = src_fps if src_fps > 0 else float(VIDEO_COMPRESS_MAX_FPS)
    out_fps = min(src_fps, float(VIDEO_COMPRESS_MAX_FPS))
    frame_step = max(1, int(round(src_fps / out_fps)))

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if h > VIDEO_COMPRESS_MAX_HEIGHT or w > VIDEO_COMPRESS_MAX_WIDTH:
        scale = min(VIDEO_COMPRESS_MAX_WIDTH / w, VIDEO_COMPRESS_MAX_HEIGHT / h)
        w = max(2, int(w * scale))
        h = max(2, int(h * scale))
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(output_path, fourcc, out_fps, (w, h))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError("OpenCV VideoWriter failed")

    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % frame_step == 0:
            if frame.shape[1] != w or frame.shape[0] != h:
                frame = cv2.resize(frame, (w, h))
            writer.write(frame)
        idx += 1

    cap.release()
    writer.release()
    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("OpenCV produced empty output")


def compress_video(input_path: str) -> Tuple[str, Dict[str, Any]]:
    """Return compressed mp4 path and stats. Skips re-compress if output already smaller."""
    if not input_path or not os.path.isfile(input_path):
        raise FileNotFoundError(input_path)

    original_mb = _file_size_mb(input_path)
    fd, out_path = tempfile.mkstemp(suffix="_compressed.mp4")
    os.close(fd)

    method = "none"
    try:
        if _find_ffmpeg():
            _compress_video_ffmpeg(input_path, out_path)
            method = "ffmpeg"
        else:
            raise RuntimeError("ffmpeg not available")
    except Exception as ffmpeg_err:
        try:
            _compress_video_opencv(input_path, out_path)
            method = "opencv"
        except Exception as opencv_err:
            if os.path.isfile(out_path):
                os.remove(out_path)
            if original_mb <= VIDEO_UPLOAD_MAX_MB:
                return input_path, {
                    "compressed": False,
                    "method": "uncompressed",
                    "original_mb": round(original_mb, 2),
                    "compressed_mb": round(original_mb, 2),
                }
            raise RuntimeError(
                f"Video compression failed (ffmpeg: {ffmpeg_err}; opencv: {opencv_err}). "
                "Install ffmpeg: `brew install ffmpeg`"
            ) from opencv_err

    compressed_mb = _file_size_mb(out_path)
    if compressed_mb >= original_mb:
        os.remove(out_path)
        return input_path, {
            "compressed": False,
            "method": "skipped",
            "original_mb": round(original_mb, 2),
            "compressed_mb": round(original_mb, 2),
        }

    return out_path, {
        "compressed": True,
        "method": method,
        "original_mb": round(original_mb, 2),
        "compressed_mb": round(compressed_mb, 2),
    }


def ensure_compressed_video(source_path: str) -> Tuple[str, Dict[str, Any]]:
    """Always returns a path suitable for API upload (compressed when possible)."""
    path, stats = compress_video(source_path)
    return path, stats


def video_cache_signature(
    cache_key: str,
    uploaded_bytes: Optional[bytes] = None,
    url_text: Optional[str] = None,
) -> Optional[str]:
    if uploaded_bytes:
        digest = hashlib.sha256(uploaded_bytes).hexdigest()[:24]
        return f"{cache_key}:bytes:{digest}"
    url_text = (url_text or "").strip()
    if url_text:
        return f"{cache_key}:url:{url_text}"
    return None


def resolve_video_for_api(
    uploaded_path: Optional[str],
    url_text: Optional[str],
    *,
    uploaded_bytes: Optional[bytes] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Compress (if needed), upload to HTTPS URL for Seedance reference_video."""
    url_text = (url_text or "").strip()

    # Pasted public URL with no local file — Seedance accepts it directly.
    if is_http_url(url_text) and not uploaded_path and not uploaded_bytes:
        return url_text, {
            "compressed": False,
            "method": "direct_url",
            "upload_method": "none",
            "public_url": url_text,
            "original_mb": None,
            "compressed_mb": None,
        }

    local_path = uploaded_path
    if not local_path and uploaded_bytes:
        fd, local_path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        with open(local_path, "wb") as f:
            f.write(uploaded_bytes)
    elif not local_path and is_http_url(url_text):
        local_path = download_url_to_temp(url_text)
    elif not local_path and url_text:
        local_path = download_url_to_temp(url_text)

    if not local_path or not os.path.isfile(local_path):
        return None, None

    original_mb = _file_size_mb(local_path)
    if _find_ffmpeg():
        compressed_path, stats = ensure_compressed_video(local_path)
    elif original_mb <= VIDEO_UPLOAD_MAX_MB:
        compressed_path = local_path
        stats = {
            "compressed": False,
            "method": "uncompressed",
            "original_mb": round(original_mb, 2),
            "compressed_mb": round(original_mb, 2),
        }
    else:
        raise RuntimeError(
            f"Video is {original_mb:.1f} MB and ffmpeg is not installed. "
            "Install ffmpeg (`brew install ffmpeg`) or paste a public HTTPS video URL."
        )

    public_url, upload_method = upload_local_file_to_public_url(
        compressed_path, media_kind="video"
    )
    stats["upload_method"] = upload_method
    stats["public_url"] = public_url
    return public_url, stats


IMAGE_UPLOAD_MAX_MB = float(os.environ.get("IMAGE_UPLOAD_MAX_MB", "8"))
IMAGE_UPLOAD_MAX_PX = int(os.environ.get("IMAGE_UPLOAD_MAX_PX", "4096"))


def _compress_image_for_upload(local_path: str) -> Tuple[str, Optional[str]]:
    """Shrink large images before upload. Returns (path, temp_path_to_delete)."""
    from PIL import Image

    size_mb = _file_size_mb(local_path)
    try:
        with Image.open(local_path) as im:
            w, h = im.size
            needs_resize = w > IMAGE_UPLOAD_MAX_PX or h > IMAGE_UPLOAD_MAX_PX
            if size_mb <= IMAGE_UPLOAD_MAX_MB and not needs_resize:
                return local_path, None

            if needs_resize:
                scale = min(IMAGE_UPLOAD_MAX_PX / w, IMAGE_UPLOAD_MAX_PX / h)
                w = max(1, int(round(w * scale)))
                h = max(1, int(round(h * scale)))
                im = im.resize((w, h), Image.Resampling.LANCZOS)

            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")

            fd, out_path = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            im.save(out_path, format="JPEG", quality=92, optimize=True)
            return out_path, out_path
    except Exception:
        return local_path, None


def resolve_image_for_api(
    uploaded_path: Optional[str],
    url_text: Optional[str],
    *,
    uploaded_bytes: Optional[bytes] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Upload image to public HTTPS URL for Seedance (avoids huge base64 payloads)."""
    url_text = (url_text or "").strip()

    if is_http_url(url_text) and not uploaded_path and not uploaded_bytes:
        return url_text, {
            "method": "direct_url",
            "upload_method": "none",
            "public_url": url_text,
        }

    local_path = uploaded_path
    if not local_path and uploaded_bytes:
        fd, local_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        with open(local_path, "wb") as f:
            f.write(uploaded_bytes)
    elif not local_path and is_http_url(url_text):
        local_path = download_url_to_temp(url_text, suffix=".png")

    if not local_path or not os.path.isfile(local_path):
        return None, None

    original_mb = _file_size_mb(local_path)
    upload_path, temp_compressed = _compress_image_for_upload(local_path)
    try:
        mime = _guess_image_mime(upload_path)
        public_url, upload_method = upload_local_file_to_public_url(
            upload_path, mime=mime, media_kind="image"
        )
    finally:
        if temp_compressed and os.path.isfile(temp_compressed):
            try:
                os.remove(temp_compressed)
            except OSError:
                pass

    return public_url, {
        "method": "uploaded",
        "upload_method": upload_method,
        "public_url": public_url,
        "original_mb": round(original_mb, 2),
        "compressed": temp_compressed is not None,
    }


def resolve_media_url(
    uploaded_path: Optional[str],
    url_text: Optional[str],
    *,
    kind: str,
) -> Optional[str]:
    url_text = (url_text or "").strip()
    if url_text:
        return url_text
    if uploaded_path and os.path.isfile(uploaded_path):
        return file_to_data_url(uploaded_path, kind=kind)
    return None


def build_content_exact(
    prompt: str,
    image_url: str,
    video_url: str,
) -> List[Dict[str, Any]]:
    return [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": image_url},
            "role": "reference_image",
        },
        {
            "type": "video_url",
            "video_url": {"url": video_url},
            "role": "reference_video",
        },
    ]


def build_content_style(
    prompt: str,
    image_url: str,
) -> List[Dict[str, Any]]:
    return [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": image_url},
            "role": "reference_image",
        },
    ]


def build_content_edit(prompt: str, video_url: str) -> List[Dict[str, Any]]:
    return [
        {"type": "text", "text": prompt},
        {
            "type": "video_url",
            "video_url": {"url": video_url},
            "role": "reference_video",
        },
    ]


def extract_output_video_url(get_result: Any) -> Optional[str]:
    try:
        content = getattr(get_result, "content", None)
        if content is not None:
            url = getattr(content, "video_url", None)
            if url:
                return url
        if isinstance(get_result, dict):
            return (get_result.get("content") or {}).get("video_url")
    except Exception:
        pass
    return None


def _obj_to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return None


def extract_task_usage(get_result: Any) -> Optional[Dict[str, Any]]:
    """Extract token / usage fields from a Seedance task response."""
    data = _obj_to_dict(get_result)
    if not isinstance(data, dict):
        data = {}

    usage = data.get("usage")
    if usage is None:
        usage = getattr(get_result, "usage", None)
    usage_dict = _obj_to_dict(usage) if usage is not None else None

    out: Dict[str, Any] = {}
    if isinstance(usage_dict, dict):
        for key in (
            "total_tokens",
            "tokens",
            "token_count",
            "prompt_tokens",
            "completion_tokens",
            "input_tokens",
            "output_tokens",
            "video_tokens",
            "image_tokens",
            "text_tokens",
        ):
            if key in usage_dict and usage_dict[key] is not None:
                out[key] = usage_dict[key]

    # Some responses put token count at top level
    if not out:
        for key in ("total_tokens", "tokens", "token_count", "usage_tokens"):
            val = data.get(key)
            if val is None:
                val = getattr(get_result, key, None)
            if val is not None:
                out[key] = val

    # Nested billing / metrics blobs
    for nested_key in ("metrics", "billing", "token_usage"):
        nested = data.get(nested_key) or getattr(get_result, nested_key, None)
        nested_dict = _obj_to_dict(nested)
        if isinstance(nested_dict, dict):
            for key in ("total_tokens", "tokens", "token_count"):
                if key in nested_dict and nested_dict[key] is not None:
                    out[key] = nested_dict[key]

    return out if out else None


def format_task_usage(usage: Optional[Dict[str, Any]]) -> str:
    if not usage:
        return "Token usage: not reported by API"
    if "total_tokens" in usage:
        return f"Token usage: {usage['total_tokens']:,} total"
    if "tokens" in usage:
        return f"Token usage: {usage['tokens']:,}"
    if "token_count" in usage:
        return f"Token usage: {usage['token_count']:,}"
    parts = []
    for k, v in usage.items():
        parts.append(f"{k}: {v:,}" if isinstance(v, (int, float)) else f"{k}: {v}")
    return "Token usage: " + ", ".join(parts)


def poll_task_once(client: Any, task_id: str) -> Tuple[str, Any, Optional[str], Optional[Dict[str, Any]]]:
    """Single status check. Returns (status, raw_result, video_url, usage)."""
    get_result = client.content_generation.tasks.get(task_id=task_id)
    status = getattr(get_result, "status", None) or (
        get_result.get("status") if isinstance(get_result, dict) else None
    )
    video_url = extract_output_video_url(get_result) if status == "succeeded" else None
    usage = extract_task_usage(get_result) if status == "succeeded" else None
    return status or "unknown", get_result, video_url, usage


def download_video_to_temp(url: str, suffix: str = ".mp4") -> str:
    import urllib.request

    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    urllib.request.urlretrieve(url.strip(), path)
    return path


def poll_seedance_task(
    client: Any,
    task_id: str,
    *,
    poll_interval_s: float = 15.0,
    max_polls: int = 120,
    on_status=None,
) -> Tuple[str, Any]:
    for i in range(max_polls):
        get_result = client.content_generation.tasks.get(task_id=task_id)
        status = getattr(get_result, "status", None) or (
            get_result.get("status") if isinstance(get_result, dict) else None
        )
        if on_status:
            on_status(status, i + 1)
        if status == "succeeded":
            return "succeeded", get_result
        if status == "failed":
            err = getattr(get_result, "error", None) or getattr(
                get_result, "message", None
            ) or str(get_result)
            raise RuntimeError(f"Seedance task failed: {err}")
        time.sleep(poll_interval_s)
    raise TimeoutError(
        f"Seedance task timed out after {max_polls} polls ({poll_interval_s}s interval)."
    )


def _extract_task_id(create_result: Any) -> Optional[str]:
    tid = getattr(create_result, "id", None)
    if tid:
        return str(tid)
    if isinstance(create_result, dict):
        val = create_result.get("id")
        return str(val) if val else None
    return None


def create_seedance_task(
    content: List[Dict[str, Any]],
    *,
    ratio: str = "adaptive",
    duration: int = -1,
    resolution: str = "1080p",
    generate_audio: bool = False,
) -> Any:
    """
    Create a Seedance content-generation task.

    Video-edit tasks (reference video in content) require:
      ratio="adaptive", duration=-1
    so output matches the selected input video (must be 4–30s).
    """
    client = get_ark_client()
    extra_body: Dict[str, Any] = {
        "ratio": ratio,
        "duration": int(duration),
        "resolution": resolution,
    }
    if generate_audio:
        extra_body["generate_audio"] = True
    try:
        return client.content_generation.tasks.create(
            model=SEEDANCE_MODEL,
            content=content,
            extra_body=extra_body,
        )
    except TypeError:
        return client.content_generation.tasks.create(
            model=SEEDANCE_MODEL,
            content=content,
        )


def _wait_gemini_file_active(client: Any, uploaded: Any, timeout_s: int = 300) -> Any:
    name = getattr(uploaded, "name", None) or uploaded.get("name")
    if not name:
        return uploaded
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        meta = client.files.get(name=name)
        state = getattr(meta, "state", None) or meta.get("state")
        if state == "ACTIVE":
            return meta
        if state == "FAILED":
            raise RuntimeError(f"Gemini file processing failed: {meta}")
        time.sleep(2)
    raise TimeoutError("Gemini file did not become ACTIVE in time.")


def _extract_gemini_text(response: Any) -> str:
    text = (getattr(response, "text", None) or "").strip()
    if text:
        return text
    for cand in getattr(response, "candidates", None) or []:
        for part in getattr(getattr(cand, "content", None), "parts", None) or []:
            t = getattr(part, "text", None)
            if t:
                return str(t).strip()
    return ""


def _parse_gemini_json(text: str) -> Dict[str, Any]:
    import json
    import re

    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise RuntimeError(f"Could not parse Gemini JSON: {text[:300]}")


def _bool_from_json(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "1")
    return bool(val)


def _build_gemini_video_parts(
    client: Any,
    video_meta: Any,
    system_prompt: str,
    *,
    user_context: str = "",
    image_path: Optional[str] = None,
) -> List[Any]:
    from google.genai import types

    parts: List[Any] = [
        types.Part.from_text(text=system_prompt),
        types.Part.from_uri(
            file_uri=video_meta.uri,
            mime_type=video_meta.mime_type or "video/mp4",
        ),
    ]
    if user_context.strip():
        parts.append(
            types.Part.from_text(
                text=f"User context for the new video (product/scene intent):\n{user_context.strip()}"
            )
        )
    if image_path and os.path.isfile(image_path):
        uploaded_img = client.files.upload(file=image_path)
        img_meta = _wait_gemini_file_active(client, uploaded_img)
        parts.append(
            types.Part.from_text(
                text=(
                    "Reference product image — frame every dialogue beat and spoken moment "
                    "around THIS product (shape, color, branding from the image):"
                )
            )
        )
        parts.append(
            types.Part.from_uri(
                file_uri=img_meta.uri,
                mime_type=img_meta.mime_type or _guess_image_mime(image_path),
            )
        )
    return parts


def _gemini_video_call(
    client: Any,
    video_meta: Any,
    system_prompt: str,
    *,
    user_context: str = "",
    image_path: Optional[str] = None,
    json_response: bool = False,
) -> str:
    from google.genai import types

    parts = _build_gemini_video_parts(
        client, video_meta, system_prompt,
        user_context=user_context, image_path=image_path,
    )
    config = None
    if json_response:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
        )
    kwargs: Dict[str, Any] = {
        "model": GEMINI_VIDEO_MODEL,
        "contents": [types.Content(role="user", parts=parts)],
    }
    if config is not None:
        kwargs["config"] = config
    response = client.models.generate_content(**kwargs)
    text = _extract_gemini_text(response)
    if not text:
        raise RuntimeError("Gemini returned empty response.")
    return text


def screen_reference_video(
    client: Any,
    video_meta: Any,
    *,
    user_context: str = "",
    image_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Layer 1: detect humans and voiceover."""
    raw = _gemini_video_call(
        client,
        video_meta,
        VIDEO_SCREEN_SYSTEM,
        user_context=user_context,
        image_path=image_path,
        json_response=True,
    )
    data = _parse_gemini_json(raw)
    has_voiceover = _bool_from_json(data.get("has_voiceover"))
    has_dialogue = _bool_from_json(data.get("has_dialogue")) or has_voiceover
    return {
        "has_humans": _bool_from_json(data.get("has_humans")),
        "has_voiceover": has_voiceover,
        "has_dialogue": has_dialogue,
        "human_notes": str(data.get("human_notes") or "").strip(),
        "voiceover_notes": str(data.get("voiceover_notes") or "").strip(),
        "dialogue_notes": str(data.get("dialogue_notes") or "").strip(),
    }


def analyze_human_voiceover_details(
    client: Any,
    video_meta: Any,
    screen: Dict[str, Any],
    *,
    user_context: str = "",
    image_path: Optional[str] = None,
) -> str:
    """Layer 2 (conditional): human + voiceover production details."""
    import json

    prompt = VIDEO_HUMAN_VO_DETAILS_SYSTEM.format(
        screen_json=json.dumps(screen, ensure_ascii=False)
    )
    return _gemini_video_call(
        client,
        video_meta,
        prompt,
        user_context=user_context,
        image_path=image_path,
    )


def analyze_dialogue_framed_for_product(
    client: Any,
    video_meta: Any,
    screen: Dict[str, Any],
    *,
    user_context: str = "",
    image_path: Optional[str] = None,
) -> str:
    """Layer 2b: point-by-point dialogue beats framed for the reference product image."""
    import json

    prompt = VIDEO_DIALOGUE_FRAME_SYSTEM.format(
        screen_json=json.dumps(screen, ensure_ascii=False)
    )
    raw = _gemini_video_call(
        client,
        video_meta,
        prompt,
        user_context=user_context,
        image_path=image_path,
    )
    if raw.strip().upper() == "NONE":
        return ""
    return raw.strip()


def analyze_camera_style(
    client: Any,
    video_meta: Any,
    *,
    user_context: str = "",
    image_path: Optional[str] = None,
) -> str:
    """Layer 3 (always): camera / style paragraph."""
    return _gemini_video_call(
        client,
        video_meta,
        STYLE_VIDEO_SYSTEM,
        user_context=user_context,
        image_path=image_path,
    )


def merge_video_analysis_layers(
    style_text: str,
    screen: Dict[str, Any],
    human_vo_details: str = "",
    dialogue_framed: str = "",
) -> str:
    """Merge style + human/VO + dialogue beats into one Seedance prompt section."""
    parts: List[str] = []

    style_text = (style_text or "").strip()
    if style_text:
        parts.append(
            "Cinematography and camera direction (from reference video):\n" + style_text
        )

    human_vo_details = (human_vo_details or "").strip()
    if human_vo_details:
        parts.append(
            "Human presence and voiceover direction (from reference video):\n"
            + human_vo_details
        )

    dialogue_framed = (dialogue_framed or "").strip()
    if dialogue_framed:
        parts.append(
            "Dialogue and narration (beat-by-beat, framed for the reference product image):\n"
            + dialogue_framed
        )

    if not human_vo_details and not dialogue_framed:
        flags = []
        if not screen.get("has_humans"):
            flags.append("no visible humans")
        if not screen.get("has_voiceover") and not screen.get("has_dialogue"):
            flags.append("no spoken dialogue or voiceover")
        elif not screen.get("has_voiceover"):
            flags.append("no voiceover")
        if flags:
            parts.append(
                "Human and audio direction: " + ", ".join(flags) + "; product-focused video only."
            )

    return "\n\n".join(parts)


def analyze_reference_video_style(
    video_path: str,
    user_context: str = "",
    image_path: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Full pipeline: screen → human/VO → dialogue beats → style → merged prompt."""
    from google import genai

    key = gemini_api_key()
    if not key:
        raise RuntimeError("Missing GEMINI_API_KEY for style analysis.")

    client = genai.Client(api_key=key)
    compressed_path, stats = ensure_compressed_video(video_path)
    uploaded_video = client.files.upload(file=compressed_path)
    video_meta = _wait_gemini_file_active(client, uploaded_video)

    screen = screen_reference_video(
        client, video_meta, user_context=user_context, image_path=image_path
    )
    stats["screen"] = screen

    human_vo_details = ""
    if screen.get("has_humans") or screen.get("has_voiceover"):
        human_vo_details = analyze_human_voiceover_details(
            client, video_meta, screen,
            user_context=user_context, image_path=image_path,
        )
        stats["human_vo_details"] = human_vo_details

    dialogue_framed = ""
    if screen.get("has_dialogue") or screen.get("has_voiceover"):
        dialogue_framed = analyze_dialogue_framed_for_product(
            client, video_meta, screen,
            user_context=user_context, image_path=image_path,
        )
        stats["dialogue_framed"] = dialogue_framed

    style_text = analyze_camera_style(
        client, video_meta, user_context=user_context, image_path=image_path
    )
    stats["style_text"] = style_text

    merged = merge_video_analysis_layers(
        style_text, screen, human_vo_details, dialogue_framed
    )
    return merged, stats


def merge_style_prompt(user_prompt: str, style_analysis: str) -> str:
    user_prompt = (user_prompt or "").strip()
    style_analysis = (style_analysis or "").strip()
    if user_prompt and style_analysis:
        return f"{user_prompt}\n\n{style_analysis}"
    return user_prompt or style_analysis
