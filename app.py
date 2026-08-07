"""
Seedance video edit UI (merged).

Flow:
  user_prompt → edited_prompt (Gemini: video + primary image)
  → final_prompt → Seedance (video asset + up to 5 extra images) in background

UI:
  1 video · 1 primary image (Gemini only) · up to 30 images (Seedance only)
  · prompt · parameters

Run:
  streamlit run app.py
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

import auth
import job_store

_ROOT = Path(__file__).resolve().parent


def _load_env_json(path: Path = _ROOT / "env.json") -> None:
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if value is None or isinstance(value, (dict, list)):
            continue
        name = str(key).strip()
        text = str(value).strip()
        if not name or not text or name in os.environ:
            continue
        os.environ[name] = text


_load_env_json()

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
    load_dotenv()
except ImportError:
    pass

if not (os.environ.get("ARK_API_KEY") or os.environ.get("BYTEPLUS_API_KEY")):
    if os.environ.get("SEEDREAM_API_KEY"):
        os.environ["ARK_API_KEY"] = os.environ["SEEDREAM_API_KEY"]
if not os.environ.get("ARK_BASE_URL") and os.environ.get("SEEDREAM_BASE_URL"):
    os.environ["ARK_BASE_URL"] = os.environ["SEEDREAM_BASE_URL"]
if not os.environ.get("SEEDANCE_MODEL") and os.environ.get("VIDEO_SEEDANCE_MODEL"):
    os.environ["SEEDANCE_MODEL"] = os.environ["VIDEO_SEEDANCE_MODEL"]

from byteplus_assets import create_asset, create_asset_group, extract_field
from seedance_streamlit_helpers import (
    SEEDANCE_MODEL,
    _extract_task_id,
    _find_ffmpeg,
    ark_api_key,
    create_seedance_task,
    download_url_to_temp,
    extract_task_usage,
    format_task_usage,
    get_ark_client,
    is_http_url,
    poll_task_once,
    resolve_image_for_api,
)
from video_edit_prompt import (
    GEMINI_MODEL,
    compose_final_prompt,
    enhance_user_prompt,
    gemini_api_key,
)

st.set_page_config(page_title="Seedance Edit → Video", page_icon="🎬", layout="wide")

POLL_INTERVAL_S = 15
ASSET_WARMUP_S = 30  # new video assets wait 30s before Seedance
MAX_SEEDANCE_IMAGES = 30
ACTIVE_STATUSES = {
    "queued",
    "running",
    "pending",
    "processing",
    "submitted",
    "in_progress",
    "preparing",
}


def _init_session() -> None:
    defaults = {
        "logged_in": False,
        "username": "",
        "result_video_url": None,
        "result_local_path": None,
        "result_usage": None,
        "result_task_id": None,
        "last_error": None,
        "cached_video_asset": None,
        "user_prompt": "",
        "edited_prompt": None,
        "final_prompt": None,
        "enhance_tokens": None,
        "jobs": [],
        "jobs_loaded_for": None,
        "last_activity_at": time.time(),
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


_init_session()

# Soft idle logout while the tab stays connected (seconds). Default 8 hours.
# Separate from Streamlit's disconnectedSessionTTL (websocket drop cleanup).
IDLE_LOGOUT_S = int(os.environ.get("IDLE_LOGOUT_S", str(8 * 60 * 60)))


def _touch_activity() -> None:
    st.session_state.last_activity_at = time.time()


def _check_idle_logout() -> None:
    if not st.session_state.logged_in:
        return
    last = float(st.session_state.get("last_activity_at") or time.time())
    if time.time() - last > IDLE_LOGOUT_S:
        st.session_state.logged_in = False
        st.session_state.username = ""
        st.session_state.jobs = []
        st.session_state.jobs_loaded_for = None
        st.warning(f"Signed out after {IDLE_LOGOUT_S // 3600}h of inactivity.")
        st.rerun()


def _load_jobs_for_user(username: str) -> None:
    """Hydrate sidebar jobs from disk (survives refresh / reconnect / re-login)."""
    if st.session_state.get("jobs_loaded_for") == username and st.session_state.jobs:
        return
    st.session_state.jobs = job_store.load(username)
    st.session_state.jobs_loaded_for = username


# ── Login gate ────────────────────────────────────────────────────────────────
if not st.session_state.logged_in:
    st.markdown("<br><br>", unsafe_allow_html=True)
    col = st.columns([1, 1, 1])[1]
    with col:
        st.title("🎬 Seedance")
        st.caption("Edit prompt → Video · Sign in to continue")
        st.divider()
        username = st.text_input("Username", key="login_username")
        password = st.text_input("Password", type="password", key="login_password")
        if st.button("Login", type="primary", use_container_width=True):
            if auth.verify(username, password):
                st.session_state.logged_in = True
                st.session_state.username = username
                _touch_activity()
                _load_jobs_for_user(username)
                st.rerun()
            else:
                st.error("Invalid username or password")
    st.stop()

_check_idle_logout()
_load_jobs_for_user(st.session_state.username)
_touch_activity()


def _seedream_ok() -> bool:
    return bool(
        os.getenv("SEEDREAM_ACCESS_KEY", "").strip()
        and os.getenv("SEEDREAM_SECRET_KEY", "").strip()
    )


def _save_upload(uploaded, default_suffix: str) -> Optional[str]:
    if uploaded is None:
        return None
    suffix = Path(uploaded.name).suffix or default_suffix
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    with open(path, "wb") as f:
        f.write(uploaded.getvalue())
    return path


def _video_fingerprint(uploaded=None, url_text: str = "", local_path: Optional[str] = None) -> str:
    h = hashlib.sha256()
    if uploaded is not None:
        h.update(uploaded.getvalue())
    elif local_path and os.path.isfile(local_path):
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
    else:
        h.update((url_text or "").strip().encode("utf-8"))
    return h.hexdigest()


VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value and str(value).strip():
            return str(value).strip()
    return default


def _video_upload_config() -> Dict[str, str]:
    return {
        "access_key": _env("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
        "secret_key": _env("S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"),
        "bucket": _env("S3_BUCKET", "AWS_S3_BUCKET", "BUCKET_NAME"),
        "region": _env("S3_REGION", "AWS_DEFAULT_REGION", "AWS_REGION", default="us-east-1"),
        "endpoint": _env("S3_ENDPOINT", "AWS_ENDPOINT_URL"),
        "cdn_base": _env(
            "VIDEO_CDN_BASE_URL",
            "CDN_BASE_URL",
            default="https://d13wztgkh59qmm.cloudfront.net",
        ),
        "tenant_id": _env("TENANT_ID", default="streamlit"),
        "resolution": _env("VIDEO_UPLOAD_RESOLUTION", default="720p"),
    }


def _s3_ok() -> bool:
    cfg = _video_upload_config()
    return bool(cfg["access_key"] and cfg["secret_key"] and cfg["bucket"] and cfg["cdn_base"])


def _s3_missing() -> List[str]:
    cfg = _video_upload_config()
    return [
        name
        for name, value in (
            ("S3_ACCESS_KEY_ID or AWS_ACCESS_KEY_ID", cfg["access_key"]),
            ("S3_SECRET_ACCESS_KEY or AWS_SECRET_ACCESS_KEY", cfg["secret_key"]),
            ("S3_BUCKET", cfg["bucket"]),
            ("VIDEO_CDN_BASE_URL", cfg["cdn_base"]),
        )
        if not value
    ]


def _build_s3_key(*, tenant_id: str, batch_id: str, resolution: str, suffix: str = ".mp4") -> str:
    from datetime import datetime, timezone
    import uuid as _uuid

    now = datetime.now(timezone.utc)
    year, month = now.strftime("%Y"), now.strftime("%m")
    file_id = str(_uuid.uuid4())
    return f"video/{year}/{month}/{tenant_id}/{batch_id}/{resolution}/{file_id}.mp4"


def _remux_h264_mp4(input_path: str) -> tuple[str, Optional[str]]:
    import subprocess

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return input_path, None

    fd, out_path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    cmd = [
        ffmpeg, "-y", "-i", input_path, "-t", "15",
        "-vf",
        "scale='min(1280,iw)':min(720,ih):force_original_aspect_ratio=decrease,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", "fast", "-crf", "23", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", out_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise RuntimeError(
            "ffmpeg failed to remux H.264 MP4: "
            + (proc.stderr.strip() or proc.stdout.strip() or "unknown error")
        )
    return out_path, out_path


def _upload_via_s3(
    local_path: str,
    *,
    resolution: Optional[str] = None,
    status_write=None,
) -> Dict[str, Any]:
    try:
        import boto3
        import uuid as _uuid
    except ImportError as e:
        raise RuntimeError("Install boto3: pip install boto3") from e

    missing = _s3_missing()
    if missing:
        raise RuntimeError("Missing video upload env: " + ", ".join(missing))

    cfg = _video_upload_config()
    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(local_path)

    res = (resolution or cfg["resolution"] or "720p").lower()
    key = _build_s3_key(
        tenant_id=cfg["tenant_id"] or "streamlit",
        batch_id=str(_uuid.uuid4()),
        resolution=res,
    )
    log = status_write or (lambda _msg: None)
    size_mb = path.stat().st_size / (1024 * 1024)

    client_kwargs: Dict[str, Any] = {
        "service_name": "s3",
        "region_name": cfg["region"] or "us-east-1",
        "aws_access_key_id": cfg["access_key"],
        "aws_secret_access_key": cfg["secret_key"],
    }
    if cfg["endpoint"]:
        client_kwargs["endpoint_url"] = cfg["endpoint"]

    log(f"Uploading to S3 (`{key}`, {size_mb:.2f} MB)…")
    client = boto3.client(**client_kwargs)
    with path.open("rb") as handle:
        client.put_object(
            Bucket=cfg["bucket"],
            Key=key,
            Body=handle,
            ContentType="video/mp4",
            ContentDisposition='inline; filename="seedance_reference.mp4"',
        )

    url = f"{cfg['cdn_base'].rstrip('/')}/{key}"
    if not is_http_url(url):
        raise RuntimeError(f"Invalid CDN URL built: {url}")
    log(f"CDN URL: `{url}`")
    return {"mp4_url": url, "key": key, "size_mb": round(size_mb, 2), "resolution": res}


def upload_video_for_create_asset(
    local_path: str,
    *,
    resolution: Optional[str] = None,
    status_write=None,
) -> tuple[str, str, Dict[str, Any]]:
    details = _upload_via_s3(local_path, resolution=resolution, status_write=status_write)
    return details["mp4_url"], "s3_cdn", details


def prepare_video_public_url_for_asset(
    *,
    local_path: Optional[str],
    url_text: str,
    resolution: Optional[str] = None,
    status_write=None,
    force_h264: bool = False,
) -> tuple[str, Dict[str, Any]]:
    url_text = (url_text or "").strip()
    cleanup: List[str] = []
    stats: Dict[str, Any] = {}
    log = status_write or (lambda _msg: None)

    try:
        source = local_path
        if not source and is_http_url(url_text):
            suffix = Path(url_text.split("?", 1)[0]).suffix.lower() or ".mp4"
            source = download_url_to_temp(url_text, suffix=suffix)
            cleanup.append(source)
        if not source or not os.path.isfile(source):
            raise RuntimeError("No local video to register as asset.")

        if force_h264:
            log("Remuxing to H.264 MP4 for CreateAsset…")
            remuxed, temp = _remux_h264_mp4(source)
            if temp:
                cleanup.append(temp)
                stats["converted_h264"] = True
            source = remuxed

        size_mb = os.path.getsize(source) / (1024 * 1024)
        stats["size_mb"] = round(size_mb, 2)
        if size_mb >= 50:
            stats["size_warning"] = "Video >= 50 MB — CreateAsset may reject it."

        public_url, method, details = upload_video_for_create_asset(
            source, resolution=resolution, status_write=status_write
        )
        stats["upload_method"] = method
        stats["public_url"] = public_url
        stats["mp4_url"] = public_url
        stats["size_mb"] = details.get("size_mb", stats["size_mb"])
        stats["local_source"] = source
        return public_url, stats
    finally:
        for p in cleanup:
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass


def register_video_asset(
    public_url: str,
    *,
    group_name: str = "streamlit-seedance-video",
    group_description: str = "Streamlit uploaded reference video",
    group_type: str = "AIGC",
) -> Dict[str, str]:
    group_resp = create_asset_group(
        name=group_name,
        description=group_description,
        group_type=group_type,
    )
    group_id = extract_field(group_resp, "GroupId", "Id", "GroupID")
    try:
        asset_resp = create_asset(
            group_id=group_id,
            url=public_url,
            asset_type="Video",
            name="seedance_reference.mp4",
        )
    except Exception as e:
        err = str(e)
        if "FormatUnsupported" in err or "Unsupported media format" in err:
            raise RuntimeError(
                "CreateAsset rejected the video format. "
                "BytePlus Video assets require MP4/MOV (H.264), ≤50MB, ~2–15s, 480p/720p. "
                f"URL tried: {public_url}. Original error: {err}"
            ) from e
        raise
    try:
        asset_id = extract_field(asset_resp, "AssetId", "Id", "AssetID")
    except RuntimeError:
        asset_id = (
            (asset_resp.get("Result") or {}).get("Id")
            or (asset_resp.get("Result") or {}).get("AssetId")
            or ""
        )
    if not asset_id:
        raise RuntimeError(f"CreateAsset returned no asset id: {asset_resp}")
    return {
        "public_url": public_url,
        "asset_id": asset_id,
        "group_id": group_id,
        "asset_url": f"asset://{asset_id}",
    }


def _wait_for_asset_warmup(status_box, *, seconds: int = ASSET_WARMUP_S) -> None:
    status_box.write(f"New video asset registered — waiting {seconds}s before Seedance…")
    progress = status_box.progress(0.0)
    for elapsed in range(1, seconds + 1):
        remaining = seconds - elapsed
        if remaining % 5 == 0 or remaining <= 3:
            status_box.write(f"Seedance in {remaining}s…")
        progress.progress(elapsed / seconds)
        time.sleep(1)
    status_box.write("Wait complete — submitting to Seedance.")


def build_content(
    prompt: str,
    *,
    video_ref: str,
    image_urls: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for url in image_urls or []:
        if not url:
            continue
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": url},
                "role": "reference_image",
            }
        )
    content.append(
        {
            "type": "video_url",
            "video_url": {"url": video_ref},
            "role": "reference_video",
        }
    )
    return content


def _elapsed_generation_s(job: Dict[str, Any]) -> Optional[float]:
    """Seconds from Seedance submit → terminal status."""
    if job.get("generation_duration_s") is not None:
        try:
            return float(job["generation_duration_s"])
        except (TypeError, ValueError):
            pass
    start = job.get("submitted_at") or job.get("created_at")
    end = job.get("completed_at") or job.get("updated_at")
    if not start or not end:
        return None
    try:
        from datetime import datetime

        def _parse(ts: str) -> datetime:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))

        return max(0.0, (_parse(end) - _parse(start)).total_seconds())
    except Exception:
        return None


def _format_generation_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(round(seconds)), 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def _format_job_metrics(job: Dict[str, Any]) -> str:
    """Task id + generation duration + tokens (same place in the UI)."""
    parts = [f"Task ID: `{job.get('task_id') or '—'}`"]
    parts.append(f"Gen time: {_format_generation_duration(_elapsed_generation_s(job))}")
    parts.append(format_task_usage(job.get("usage")))
    return " · ".join(parts)


def _upsert_job(job: Dict[str, Any]) -> None:
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    job = {
        **job,
        "username": st.session_state.get("username") or job.get("username") or "",
    }
    job.setdefault("submitted_at", now_iso)
    saved = job_store.upsert(job)
    jobs: List[Dict[str, Any]] = st.session_state.jobs
    tid = saved.get("task_id")
    for i, existing in enumerate(jobs):
        if existing.get("task_id") == tid:
            jobs[i] = saved
            return
    jobs.insert(0, saved)


def _poll_jobs_once() -> bool:
    """Poll in-progress Seedance jobs. Returns True if any job changed."""
    changed = False
    if not ark_api_key():
        return False
    try:
        client = get_ark_client()
    except Exception:
        return False

    username = st.session_state.get("username") or ""
    # Prefer disk as source of truth so refresh / multi-tab stay consistent
    disk_jobs = job_store.load(username) if username else list(st.session_state.jobs)
    if disk_jobs:
        st.session_state.jobs = disk_jobs

    for job in st.session_state.jobs:
        status = str(job.get("status") or "").lower()
        if status in job_store.TERMINAL or not job.get("task_id"):
            continue
        try:
            new_status, raw, video_url, usage = poll_task_once(client, job["task_id"])
            dirty = False
            if new_status != job.get("status"):
                dirty = True
            job["status"] = new_status
            if new_status == "succeeded":
                job["video_url"] = video_url
                job["usage"] = usage or extract_task_usage(raw)
                job["error"] = None
                job["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
                job["generation_duration_s"] = _elapsed_generation_s(
                    {**job, "completed_at": job["completed_at"]}
                )
                dirty = True
            elif new_status == "failed":
                err = getattr(raw, "error", None) or getattr(raw, "message", None) or str(raw)
                job["error"] = str(err)
                job["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
                job["generation_duration_s"] = _elapsed_generation_s(
                    {**job, "completed_at": job["completed_at"]}
                )
                dirty = True
            if dirty:
                job_store.update(
                    job["task_id"],
                    status=job.get("status"),
                    video_url=job.get("video_url"),
                    usage=job.get("usage"),
                    error=job.get("error"),
                    generation_duration_s=job.get("generation_duration_s"),
                    completed_at=job.get("completed_at"),
                )
                changed = True
        except Exception as e:
            job["error"] = str(e)
            job_store.update(job["task_id"], error=str(e))
            changed = True
    return changed


def _materialize_media(
    *,
    uploaded_video,
    video_url_text: str,
    primary_image,
    primary_image_url_text: str,
    extra_images,
) -> tuple[Optional[str], Optional[str], List[tuple[bytes, str]], List[str]]:
    """
    Returns (video_path, primary_image_path, extra_image_bytes_list, cleanup_paths).
    extra_image_bytes_list: list of (bytes, filename) for Seedance extras.
    """
    cleanup: List[str] = []
    video_path = _save_upload(uploaded_video, ".mp4") if uploaded_video else None
    if video_path:
        cleanup.append(video_path)
    elif (video_url_text or "").strip():
        url = video_url_text.strip()
        suffix = Path(url.split("?", 1)[0]).suffix.lower() or ".mp4"
        fd, video_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        urllib.request.urlretrieve(url, video_path)
        cleanup.append(video_path)

    primary_path = _save_upload(primary_image, ".png") if primary_image else None
    if primary_path:
        cleanup.append(primary_path)
    elif (primary_image_url_text or "").strip().startswith(("http://", "https://")):
        url = primary_image_url_text.strip()
        suffix = Path(url.split("?", 1)[0]).suffix.lower() or ".png"
        fd, primary_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        urllib.request.urlretrieve(url, primary_path)
        cleanup.append(primary_path)

    extras: List[tuple[bytes, str]] = []
    for f in (extra_images or [])[:MAX_SEEDANCE_IMAGES]:
        data = f.getvalue()
        if data:
            extras.append((data, getattr(f, "name", None) or "image.png"))

    return video_path, primary_path, extras, cleanup


# --- UI ---
st.title("Seedance — Edit Prompt → Video")
st.caption(
    f"**1 video** · **1 primary image → Gemini** · **up to {MAX_SEEDANCE_IMAGES} images → Seedance** · "
    f"Model `{SEEDANCE_MODEL}` · Asset warmup `{ASSET_WARMUP_S}s` · Seedance runs in background"
)

with st.sidebar:
    st.header("Account")
    col_user, col_logout = st.columns([2, 1])
    col_user.caption(f"Logged in as **{st.session_state.username}**")
    if col_logout.button("Logout"):
        # Jobs stay on disk — only clear the auth session
        st.session_state.logged_in = False
        st.session_state.username = ""
        st.session_state.jobs = []
        st.session_state.jobs_loaded_for = None
        st.rerun()

    st.caption(f"Session idle logout: **{IDLE_LOGOUT_S // 3600}h** · reconnect TTL: **8h**")

    st.divider()
    st.header("API")
    ark_ok = bool(ark_api_key())
    gemini_ok = bool(gemini_api_key())
    seedream_ok = _seedream_ok()
    s3_ok = _s3_ok()
    st.write(f"Gemini (enhance): {'✅' if gemini_ok else '❌'}")
    st.write(f"ARK (Seedance): {'✅' if ark_ok else '❌'}")
    st.write(f"Seedream (asset): {'✅' if seedream_ok else '❌'}")
    st.write(f"S3 + video CDN: {'✅' if s3_ok else '❌'}")
    if not gemini_ok:
        st.caption("Set `GEMINI_API_KEY`.")
    if not ark_ok:
        st.caption("Set `ARK_API_KEY` or `BYTEPLUS_API_KEY`.")
    if not seedream_ok:
        st.caption("Set `SEEDREAM_ACCESS_KEY` and `SEEDREAM_SECRET_KEY`.")
    if not s3_ok:
        st.caption("Missing: " + (", ".join(_s3_missing()) or "S3 config"))
    else:
        cfg = _video_upload_config()
        st.caption(f"CDN `{cfg['cdn_base']}` · bucket `{cfg['bucket']}`")

    cached = st.session_state.cached_video_asset
    if cached and cached.get("asset_id"):
        st.divider()
        st.caption("Cached video asset")
        st.code(cached["asset_id"], language=None)

    st.divider()
    st.subheader("Background jobs")

    def _render_jobs_body() -> None:
        # Poll in-place; fragment already refreshes via run_every — no st.rerun needed.
        has_running = any(
            str(j.get("status") or "").lower() not in job_store.TERMINAL
            for j in st.session_state.jobs
        )
        if has_running:
            _poll_jobs_once()

        jobs = st.session_state.jobs
        running = [
            j for j in jobs if str(j.get("status") or "").lower() not in job_store.TERMINAL
        ]
        if running:
            st.caption(f"🔄 {len(running)} running · auto-poll ~{POLL_INTERVAL_S}s")
        elif not jobs:
            st.caption("No jobs yet.")
            return
        else:
            st.caption(f"{len(jobs)} job(s) · saved across refresh / logout")

        for job in jobs[:12]:
            status = str(job.get("status") or "unknown")
            badge = {
                "succeeded": "✅",
                "failed": "❌",
                "pending": "⏳",
                "running": "🔄",
                "queued": "⏳",
            }.get(status, "⏳")
            tid = job.get("task_id") or ""
            with st.expander(
                f"{badge} {status} · `{tid[-10:] if tid else '—'}`",
                expanded=(status not in job_store.TERMINAL),
            ):
                if job.get("user_prompt"):
                    st.caption(
                        job["user_prompt"][:100]
                        + ("…" if len(job["user_prompt"]) > 100 else "")
                    )
                if job.get("edited_prompt"):
                    with st.expander("edited_prompt"):
                        st.write(job["edited_prompt"])
                if job.get("video_url"):
                    st.video(job["video_url"])
                    st.link_button("Open video", job["video_url"])
                st.caption(_format_job_metrics(job))
                if job.get("usage") or job.get("generation_duration_s") is not None:
                    with st.expander("Task metrics"):
                        st.write(f"**Task ID:** `{job.get('task_id')}`")
                        st.write(
                            f"**Generation duration:** "
                            f"{_format_generation_duration(_elapsed_generation_s(job))}"
                        )
                        if job.get("usage"):
                            st.json(job["usage"])
                if job.get("error"):
                    st.error(job["error"])
                if status not in job_store.TERMINAL:
                    st.caption("Polling in background…")
                    st.caption(_format_job_metrics(job))

    if hasattr(st, "fragment"):
        @st.fragment(run_every=POLL_INTERVAL_S)
        def _jobs_panel() -> None:
            _render_jobs_body()

        _jobs_panel()
    else:
        _render_jobs_body()

# ── Main form ─────────────────────────────────────────────────────────────────
st.subheader("Video")
uploaded_video = st.file_uploader(
    "Video 1 — source (required)",
    type=["mp4", "mov", "webm"],
    key="video_uploader",
)
video_url_text = st.text_input("Or Video 1 URL", placeholder="https://...mp4")
if uploaded_video:
    st.video(uploaded_video)

st.subheader("Images")
col_primary, col_extra = st.columns(2)

with col_primary:
    st.markdown("**Primary image → Gemini enhance**")
    st.caption("Only used when **Use enhance layer** is on (sent to Gemini as Image 1).")
    primary_image = st.file_uploader(
        "Primary image (optional)",
        type=["png", "jpg", "jpeg", "webp"],
        key="primary_image_uploader",
        accept_multiple_files=False,
    )
    primary_image_url_text = st.text_input(
        "Or primary image URL",
        placeholder="https://...",
        key="primary_image_url",
    )
    if primary_image:
        st.image(primary_image, caption="Primary (Gemini)", use_container_width=True)

with col_extra:
    st.markdown(f"**Extra images → Seedance (up to {MAX_SEEDANCE_IMAGES})**")
    st.caption("Sent directly to Seedance as reference images (not to Gemini).")
    extra_images = st.file_uploader(
        f"Seedance images (up to {MAX_SEEDANCE_IMAGES})",
        type=["png", "jpg", "jpeg", "webp"],
        key="extra_images_uploader",
        accept_multiple_files=True,
    )
    if extra_images:
        if len(extra_images) > MAX_SEEDANCE_IMAGES:
            st.warning(f"Only the first {MAX_SEEDANCE_IMAGES} images will be used.")
            extra_images = extra_images[:MAX_SEEDANCE_IMAGES]
        cols = st.columns(min(len(extra_images), 3))
        for i, f in enumerate(extra_images):
            cols[i % 3].image(f, caption=f.name, use_container_width=True)

st.subheader("Prompt")
user_prompt = st.text_area(
    "User prompt",
    height=140,
    placeholder=(
        "e.g. Remove the cup after the person places it on the table, or replace "
        "the product in Video 1 with the primary image."
    ),
    key="user_prompt_input",
)

st.subheader("Parameters")
p1, p2, p3, p4 = st.columns(4)
with p1:
    use_enhance = st.toggle(
        "Use enhance layer",
        value=False,
        help="Off (default): send your user prompt straight to Seedance. "
        "On: Gemini rewrites the prompt first (needs primary image optionally + GEMINI_API_KEY).",
    )
    edit_mode = st.radio(
        "Edit workflow",
        options=["General edit", "Replace item or avatar"],
        help="Used when enhance is on. General: add/remove/modify. Replace: product/prop/avatar swap.",
        disabled=not use_enhance,
    )
with p2:
    st.markdown("**Edit output**")
    st.caption(
        "Seedance video-edit tasks use **ratio=adaptive** and **duration=-1** "
        "(match the input video). Source video must be **4–30 seconds**."
    )
    ratio = "adaptive"
    duration = -1
with p3:
    resolution = st.selectbox("Seedance resolution", ["1080p", "720p", "480p"], index=0)
    gemini_model = st.text_input(
        "Gemini model",
        value=GEMINI_MODEL,
        disabled=not use_enhance,
    )
with p4:
    generate_audio = False
    skip_warmup = st.checkbox(
        f"Skip {ASSET_WARMUP_S}s asset warmup",
        value=False,
        help="Only skip if this video asset was already registered and is ready.",
    )

generate = st.button(
    "Enhance → Queue Seedance" if use_enhance else "Queue Seedance (background)",
    type="primary",
    use_container_width=True,
)

st.divider()
st.subheader("Prompt pipeline")

if generate:
    st.session_state.last_error = None
    st.session_state.edited_prompt = None
    st.session_state.final_prompt = None
    st.session_state.enhance_tokens = None

    has_video = bool(uploaded_video) or bool((video_url_text or "").strip())
    if not (user_prompt or "").strip():
        st.warning("Enter a user prompt.")
    elif not has_video:
        st.warning("Upload a video or paste a video URL.")
    elif use_enhance and not gemini_ok:
        st.error("Missing GEMINI_API_KEY (required when enhance layer is on).")
    elif not ark_ok:
        st.error("Missing ARK_API_KEY or BYTEPLUS_API_KEY.")
    elif not seedream_ok:
        st.error("Missing SEEDREAM_ACCESS_KEY / SEEDREAM_SECRET_KEY.")
    elif not s3_ok:
        st.error("Missing S3 env: " + ", ".join(_s3_missing()))
    else:
        cleanup: List[str] = []
        try:
            video_path, primary_path, extra_bytes, cleanup = _materialize_media(
                uploaded_video=uploaded_video,
                video_url_text=video_url_text or "",
                primary_image=primary_image,
                primary_image_url_text=primary_image_url_text or "",
                extra_images=extra_images,
            )
            if not video_path or not os.path.isfile(video_path):
                raise RuntimeError("Could not prepare Video 1.")

            st.session_state.user_prompt = user_prompt.strip()

            # ── Step A: optional enhance (primary image only → Gemini) ──────
            if use_enhance:
                with st.status(
                    "Step 1 — Enhance: user_prompt → edited_prompt", expanded=True
                ) as s_enh:
                    s_enh.write(f"Model: `{gemini_model}` · workflow: `{edit_mode}`")
                    s_enh.write("Uploading Video 1 to Gemini…")
                    if primary_path:
                        s_enh.write("Including primary image (Gemini only)…")
                    else:
                        s_enh.write("No primary image — enhance from video + prompt only.")
                    edited_prompt, token_info = enhance_user_prompt(
                        video_path=video_path,
                        image_path=primary_path,
                        user_prompt=user_prompt.strip(),
                        edit_mode=edit_mode,
                        model=(gemini_model or GEMINI_MODEL).strip(),
                    )
                    st.session_state.edited_prompt = edited_prompt
                    st.session_state.enhance_tokens = token_info
                    final_prompt = compose_final_prompt(
                        edit_mode=edit_mode,
                        edited_prompt=edited_prompt,
                        user_prompt=user_prompt.strip(),
                    )
                    st.session_state.final_prompt = final_prompt
                    s_enh.update(label="Enhance complete", state="complete")
            else:
                # Skip Gemini — user prompt goes straight to Seedance
                st.session_state.edited_prompt = user_prompt.strip()
                st.session_state.final_prompt = user_prompt.strip()
                st.session_state.enhance_tokens = None
                st.info("Enhance layer off — using user prompt as final_prompt for Seedance.")

            st.markdown("##### user_prompt")
            st.code(st.session_state.user_prompt, language=None)
            if use_enhance:
                st.markdown("##### edited_prompt")
                st.code(st.session_state.edited_prompt, language=None)
            st.markdown("##### final_prompt (queued to Seedance)")
            st.code(st.session_state.final_prompt, language=None)
            if st.session_state.enhance_tokens:
                ti = st.session_state.enhance_tokens
                c1, c2, c3 = st.columns(3)
                c1.metric("Enhance prompt tokens", ti.get("prompt_tokens") or "—")
                c2.metric("Enhance output tokens", ti.get("output_tokens") or "—")
                c3.metric("Enhance total", ti.get("total_tokens") or "—")

            prompt_for_seedance = (st.session_state.final_prompt or "").strip()
            if not prompt_for_seedance:
                raise RuntimeError("final_prompt is empty.")

            video_hash = _video_fingerprint(
                uploaded=uploaded_video,
                url_text=(video_url_text or "").strip(),
                local_path=video_path,
            )
            cached = st.session_state.cached_video_asset
            video_changed = (
                cached is None
                or cached.get("video_hash") != video_hash
                or not cached.get("asset_id")
            )

            # ── Step B: asset ───────────────────────────────────────────────
            with st.status("Step 2 — S3 CDN upload → CreateAsset", expanded=True) as s1:
                if video_changed:
                    s1.write("Uploading video to S3…")
                    public_url, vid_stats = prepare_video_public_url_for_asset(
                        local_path=video_path,
                        url_text=(video_url_text or "").strip(),
                        status_write=s1.write,
                        force_h264=False,
                    )
                    if not public_url:
                        raise RuntimeError("Could not prepare reference video.")
                    s1.write(f"CDN URL: `{public_url}`")
                    if vid_stats.get("size_mb") is not None:
                        s1.write(f"Size: `{vid_stats['size_mb']} MB`")
                    if vid_stats.get("size_warning"):
                        s1.warning(vid_stats["size_warning"])

                    s1.write("CreateAssetGroup + CreateAsset (Video)…")
                    try:
                        asset_info = register_video_asset(public_url)
                    except RuntimeError as e:
                        if "FormatUnsupported" not in str(e) and "Unsupported media format" not in str(e):
                            raise
                        s1.warning("CreateAsset rejected format — remuxing H.264 and retrying…")
                        public_url, vid_stats = prepare_video_public_url_for_asset(
                            local_path=video_path,
                            url_text=(video_url_text or "").strip(),
                            status_write=s1.write,
                            force_h264=True,
                        )
                        s1.write(f"Retry CDN URL: `{public_url}`")
                        asset_info = register_video_asset(public_url)

                    s1.write(f"Asset ID: `{asset_info['asset_id']}`")
                    st.session_state.cached_video_asset = {
                        "video_hash": video_hash,
                        **asset_info,
                    }
                    s1.update(label="Video asset registered", state="complete")
                else:
                    asset_info = {
                        "asset_id": cached["asset_id"],
                        "group_id": cached.get("group_id", ""),
                        "public_url": cached.get("public_url", ""),
                        "asset_url": cached.get("asset_url")
                        or f"asset://{cached['asset_id']}",
                    }
                    s1.write(f"Same video — reusing asset `{asset_info['asset_id']}`")
                    s1.update(label="Cached video asset reused", state="complete")

            if video_changed and not skip_warmup:
                with st.status(
                    f"Step 3 — Wait {ASSET_WARMUP_S}s for asset readiness",
                    expanded=True,
                ) as s_wait:
                    _wait_for_asset_warmup(s_wait)
                    s_wait.update(label="Asset warmup complete", state="complete")
            elif video_changed and skip_warmup:
                st.caption(f"Skipped {ASSET_WARMUP_S}s warmup.")
            else:
                st.caption("Skipped warmup — cached asset reused.")

            # Extra images only → Seedance (primary stays Gemini-only)
            seedance_image_urls: List[str] = []
            if extra_bytes:
                with st.status(
                    f"Prepare {len(extra_bytes)} Seedance reference image(s)",
                    expanded=True,
                ) as s_img:
                    for i, (img_bytes, fname) in enumerate(extra_bytes):
                        url, img_stats = resolve_image_for_api(
                            None,
                            "",
                            uploaded_bytes=img_bytes,
                        )
                        if not url:
                            raise RuntimeError(f"Could not prepare Seedance image {i + 1}: {fname}")
                        seedance_image_urls.append(url)
                        s_img.write(f"Image {i + 1}: `{url[:80]}…`" if len(url) > 80 else f"Image {i + 1}: `{url}`")
                    s_img.update(label="Seedance images ready", state="complete")

            content = build_content(
                prompt_for_seedance,
                video_ref=asset_info["asset_url"],
                image_urls=seedance_image_urls,
            )

            # ── Step C: submit Seedance (background poll in sidebar) ────────
            with st.status("Step 4 — Submit Seedance (background)", expanded=True) as s2:
                s2.write(f"Video ref: `{asset_info['asset_url']}`")
                s2.write(f"Seedance images: {len(seedance_image_urls)}")
                s2.write("Edit params: `ratio=adaptive`, `duration=-1` (match input video)")
                create_result = create_seedance_task(
                    content,
                    ratio="adaptive",
                    duration=-1,
                    resolution=resolution,
                    generate_audio=generate_audio,
                )
                task_id = _extract_task_id(create_result)
                if not task_id:
                    raise RuntimeError(f"No task id: {create_result!r}")

                _upsert_job(
                    {
                        "task_id": task_id,
                        "status": "running",
                        "user_prompt": st.session_state.user_prompt,
                        "edited_prompt": st.session_state.edited_prompt,
                        "final_prompt": prompt_for_seedance,
                        "asset_id": asset_info.get("asset_id"),
                        "image_count": len(seedance_image_urls),
                        "ratio": ratio,
                        "duration": duration,
                        "resolution": resolution,
                        "video_url": None,
                        "usage": extract_task_usage(create_result),
                        "generation_duration_s": None,
                        "error": None,
                    }
                )
                st.session_state.result_task_id = task_id
                s2.write(f"Task ID: `{task_id}` — polling in sidebar")
                s2.update(label="Queued in background", state="complete")

            st.success(
                (
                    "Enhance done · Seedance queued. "
                    if use_enhance
                    else "Seedance queued (no enhance). "
                )
                + "Watch **Background jobs** in the sidebar — you can start another generation now."
            )

        except Exception as e:
            st.session_state.last_error = str(e)
            st.error(str(e))
        finally:
            for p in cleanup:
                try:
                    if p and os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass

if not generate and st.session_state.edited_prompt:
    st.markdown("##### user_prompt")
    st.code(st.session_state.user_prompt or "", language=None)
    st.markdown("##### edited_prompt")
    st.code(st.session_state.edited_prompt, language=None)
    st.markdown("##### final_prompt")
    st.code(st.session_state.final_prompt or "", language=None)

if st.session_state.last_error and not generate:
    st.error(st.session_state.last_error)
