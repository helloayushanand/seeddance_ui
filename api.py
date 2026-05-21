import base64
import mimetypes
import os
import uuid
from byteplussdkarkruntime import Ark

UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "uploads")


def save_upload(file_bytes: bytes, filename: str) -> str:
    """Save uploaded file locally and return the file path."""
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    ext = os.path.splitext(filename)[1] or ".bin"
    local_path = os.path.join(UPLOADS_DIR, f"{uuid.uuid4().hex}{ext}")
    with open(local_path, "wb") as f:
        f.write(file_bytes)
    return local_path


def get_client(api_key: str) -> Ark:
    return Ark(
        base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
        api_key=api_key,
    )


def file_to_data_uri(file_bytes: bytes, filename: str) -> str:
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    b64 = base64.b64encode(file_bytes).decode()
    return f"data:{mime};base64,{b64}"


def create_task(
    client: Ark,
    prompt: str,
    image_urls: list[str],
    video_url: str | None = None,
    audio_urls: list[str] | None = None,
    ratio: str = "16:9",
    duration: int = 5,
    resolution: str = "1080p",
    generate_audio: bool = True,
    watermark: bool = False,
) -> dict:
    content = [{"type": "text", "text": prompt}]

    for url in image_urls:
        content.append({
            "type": "image_url",
            "image_url": {"url": url},
            "role": "reference_image",
        })

    if video_url:
        content.append({
            "type": "video_url",
            "video_url": {"url": video_url},
            "role": "reference_video",
        })

    for aurl in audio_urls or []:
        if not aurl:
            continue
        content.append({
            "type": "audio_url",
            "audio_url": {"url": aurl},
            "role": "reference_audio",
        })

    result = client.content_generation.tasks.create(
        model="dreamina-seedance-2-0-260128",
        content=content,
        generate_audio=generate_audio,
        ratio=ratio,
        duration=duration,
        watermark=watermark,
        resolution=resolution,
    )
    return {"id": result.id, "raw": result}


def get_task(client: Ark, task_id: str):
    return client.content_generation.tasks.get(task_id=task_id)


def extract_task_error(result) -> str | None:
    """Human-readable error from a polled task (status failed)."""
    try:
        err = getattr(result, "error", None)
        if err is None:
            return None
        msg = getattr(err, "message", None) or ""
        code = getattr(err, "code", None) or ""
        if code and msg:
            return f"{code}: {msg}"
        return msg or code or None
    except Exception:
        pass
    try:
        raw = result.model_dump() if hasattr(result, "model_dump") else {}
        err = raw.get("error") or {}
        if isinstance(err, dict):
            code = err.get("code", "")
            msg = err.get("message", "")
            if code and msg:
                return f"{code}: {msg}"
            return msg or code or None
    except Exception:
        pass
    return None


def format_api_exception(e: Exception) -> str:
    """Format Ark API errors (e.g. create_task) for display."""
    try:
        from byteplussdkarkruntime._exceptions import ArkAPIError

        if isinstance(e, ArkAPIError):
            parts = [e.message]
            if e.code:
                parts.append(f"Code: {e.code}")
            if e.request_id:
                parts.append(f"Request ID: {e.request_id}")
            if e.body is not None:
                import json

                if isinstance(e.body, dict):
                    parts.append("Body:\n" + json.dumps(e.body, indent=2))
                else:
                    parts.append(f"Body: {e.body}")
            return "\n".join(parts)
    except Exception:
        pass
    return str(e)
