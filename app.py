"""
Seedance video edit UI (merged).

Flow:
  user_prompt → edited_prompt (Gemini enhance) → final_prompt → Seedance video

Steps:
  1. Upload video (+ optional image) and enter user_prompt
  2. Gemini enhance layer produces edited_prompt, then compose final_prompt
  3. Upload video → S3 CDN → BytePlus CreateAsset
  4. Wait 30s for new assets (reuse cached asset_id if same video)
  5. Submit Seedance with final_prompt and poll until done

Run:
  streamlit run streamlit_seedance.py

Loads keys from env.json (project root), then process env / .env.
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
    download_video_to_temp,
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
ASSET_WARMUP_S = 30  # was 120 — new video assets wait 30s before Seedance
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
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


_init_session()


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
    image_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image_url:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image_url},
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


def poll_until_done(task_id: str, status_box) -> Dict[str, Any]:
    client = get_ark_client()
    while True:
        status, raw, video_url, usage = poll_task_once(client, task_id)
        status_box.write(f"Status: `{status}`")
        if status == "succeeded":
            return {
                "status": status,
                "raw": raw,
                "video_url": video_url,
                "usage": usage or extract_task_usage(raw),
            }
        if status == "failed":
            err = getattr(raw, "error", None) or getattr(raw, "message", None) or str(raw)
            raise RuntimeError(f"Seedance task failed: {err}")
        if status not in ACTIVE_STATUSES and status != "unknown":
            status_box.write(f"Unexpected status `{status}` — still polling…")
        time.sleep(POLL_INTERVAL_S)


def _materialize_media(
    *,
    uploaded_video,
    video_url_text: str,
    uploaded_image,
    image_url_text: str,
) -> tuple[Optional[str], Optional[str], List[str]]:
    """Save uploads / download URLs to temp files. Returns (video_path, image_path, cleanup)."""
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

    image_path = _save_upload(uploaded_image, ".png") if uploaded_image else None
    if image_path:
        cleanup.append(image_path)
    elif (image_url_text or "").strip().startswith(("http://", "https://")):
        url = image_url_text.strip()
        suffix = Path(url.split("?", 1)[0]).suffix.lower() or ".png"
        fd, image_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        urllib.request.urlretrieve(url, image_path)
        cleanup.append(image_path)

    return video_path, image_path, cleanup


# --- UI ---
st.title("Seedance — Edit Prompt → Video")
st.caption(
    f"Flow: **user_prompt → edited_prompt → final_prompt → video** · "
    f"Model `{SEEDANCE_MODEL}` · Enhance `{GEMINI_MODEL}` · Asset warmup `{ASSET_WARMUP_S}s`"
)

with st.sidebar:
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

    st.divider()
    st.header("Parameters")
    edit_mode = st.radio(
        "Edit workflow",
        options=["General edit", "Replace item or avatar"],
        help="General edit: add/remove/modify/dialogue/background. Replace: product/prop/avatar swap.",
    )
    gemini_model = st.text_input("Gemini enhance model", value=GEMINI_MODEL)
    ratio = st.selectbox("Aspect ratio", ["16:9", "9:16", "1:1", "4:3", "3:4"], index=0)
    duration = st.slider("Duration (seconds)", 4, 15, 10)
    resolution = st.selectbox("Seedance resolution", ["1080p", "720p", "480p"], index=0)
    generate_audio = False
    skip_warmup = st.checkbox(
        f"Skip {ASSET_WARMUP_S}s asset warmup",
        value=False,
        help="Only skip if this video asset was already registered and is ready.",
    )

    cached = st.session_state.cached_video_asset
    if cached and cached.get("asset_id"):
        st.divider()
        st.caption("Cached video asset (same video → reuse, no warmup)")
        st.code(cached["asset_id"], language=None)

col_in, col_out = st.columns(2)

with col_in:
    st.subheader("1 · Input")
    uploaded_video = st.file_uploader(
        "Video 1 — source video (required)",
        type=["mp4", "mov", "webm"],
        key="video_uploader",
    )
    video_url_text = st.text_input("Or Video 1 URL", placeholder="https://...mp4")
    if uploaded_video:
        st.video(uploaded_video)

    uploaded_image = st.file_uploader(
        "Image 1 — reference (optional)",
        type=["png", "jpg", "jpeg", "webp"],
        key="image_uploader",
    )
    image_url_text = st.text_input("Or Image 1 URL (optional)", placeholder="https://...")
    if uploaded_image:
        st.image(uploaded_image, caption="Image 1", use_container_width=True)

    user_prompt = st.text_area(
        "User prompt",
        height=140,
        placeholder=(
            "e.g. Remove the cup after the person places it on the table, or replace "
            "the product in Video 1 with Image 1."
        ),
        key="user_prompt_input",
    )

    generate = st.button(
        "Enhance prompt → Generate video",
        type="primary",
        use_container_width=True,
    )

with col_out:
    st.subheader("2 · Prompt pipeline & output")

    if generate:
        st.session_state.last_error = None
        st.session_state.result_video_url = None
        st.session_state.result_local_path = None
        st.session_state.result_usage = None
        st.session_state.result_task_id = None
        st.session_state.edited_prompt = None
        st.session_state.final_prompt = None
        st.session_state.enhance_tokens = None

        has_video = bool(uploaded_video) or bool((video_url_text or "").strip())
        if not (user_prompt or "").strip():
            st.warning("Enter a user prompt.")
        elif not has_video:
            st.warning("Upload a video or paste a video URL.")
        elif not gemini_ok:
            st.error("Missing GEMINI_API_KEY.")
        elif not ark_ok:
            st.error("Missing ARK_API_KEY or BYTEPLUS_API_KEY.")
        elif not seedream_ok:
            st.error("Missing SEEDREAM_ACCESS_KEY / SEEDREAM_SECRET_KEY.")
        elif not s3_ok:
            st.error("Missing S3 env: " + ", ".join(_s3_missing()))
        else:
            video_path = None
            image_path = None
            cleanup: List[str] = []
            try:
                video_path, image_path, cleanup = _materialize_media(
                    uploaded_video=uploaded_video,
                    video_url_text=video_url_text or "",
                    uploaded_image=uploaded_image,
                    image_url_text=image_url_text or "",
                )
                if not video_path or not os.path.isfile(video_path):
                    raise RuntimeError("Could not prepare Video 1.")

                st.session_state.user_prompt = user_prompt.strip()

                # ── Step A: enhance ──────────────────────────────────────────
                with st.status("Step 1 — Enhance: user_prompt → edited_prompt", expanded=True) as s_enh:
                    s_enh.write(f"Model: `{gemini_model}` · workflow: `{edit_mode}`")
                    s_enh.write("Uploading Video 1 to Gemini…")
                    if image_path:
                        s_enh.write("Including Image 1…")
                    edited_prompt, token_info = enhance_user_prompt(
                        video_path=video_path,
                        image_path=image_path,
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
                    s_enh.write("edited_prompt ready → composed final_prompt")
                    s_enh.update(label="Enhance complete", state="complete")

                st.markdown("##### user_prompt")
                st.code(st.session_state.user_prompt, language=None)
                st.markdown("##### edited_prompt")
                st.code(st.session_state.edited_prompt, language=None)
                st.markdown("##### final_prompt (sent to Seedance)")
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

                # ── Step B: asset ────────────────────────────────────────────
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

                image_url = None
                if uploaded_image or (image_url_text or "").strip():
                    with st.status("Prepare optional reference image", expanded=True) as s_img:
                        image_url, img_stats = resolve_image_for_api(
                            image_path,
                            (image_url_text or "").strip(),
                            uploaded_bytes=uploaded_image.getvalue() if uploaded_image else None,
                        )
                        if not image_url:
                            raise RuntimeError("Could not prepare reference image.")
                        s_img.write(f"Image URL: `{image_url}`")
                        s_img.update(label="Reference image ready", state="complete")

                content = build_content(
                    prompt_for_seedance,
                    video_ref=asset_info["asset_url"],
                    image_url=image_url,
                )

                # ── Step C: Seedance ─────────────────────────────────────────
                with st.status("Step 4 — Seedance: final_prompt → video", expanded=True) as s2:
                    s2.write(f"Video ref: `{asset_info['asset_url']}`")
                    if image_url:
                        s2.write(f"Image ref: `{image_url}`")
                    create_result = create_seedance_task(
                        content,
                        ratio=ratio,
                        duration=duration,
                        resolution=resolution,
                        generate_audio=generate_audio,
                    )
                    task_id = _extract_task_id(create_result)
                    if not task_id:
                        raise RuntimeError(f"No task id: {create_result!r}")
                    st.session_state.result_task_id = task_id
                    s2.write(f"Task ID: `{task_id}` — polling…")

                    done = poll_until_done(task_id, s2)
                    video_url = done.get("video_url")
                    usage = done.get("usage")
                    if not video_url:
                        raise RuntimeError("Task succeeded but no video URL returned.")
                    st.session_state.result_video_url = video_url
                    st.session_state.result_usage = usage
                    try:
                        st.session_state.result_local_path = download_video_to_temp(video_url)
                    except Exception:
                        st.session_state.result_local_path = None
                    s2.update(label="Video ready", state="complete")

                st.success("Pipeline complete: user_prompt → edited → final → video.")

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

    # Persist pipeline display after generation
    if not generate and st.session_state.edited_prompt:
        st.markdown("##### user_prompt")
        st.code(st.session_state.user_prompt or "", language=None)
        st.markdown("##### edited_prompt")
        st.code(st.session_state.edited_prompt, language=None)
        st.markdown("##### final_prompt")
        st.code(st.session_state.final_prompt or "", language=None)

    if st.session_state.last_error and not generate:
        st.error(st.session_state.last_error)

    if st.session_state.result_video_url:
        st.subheader("Video")
        st.video(st.session_state.result_video_url)
        usage = st.session_state.result_usage
        st.caption(format_task_usage(usage))
        if usage:
            with st.expander("Seedance token details"):
                st.json(usage)

        local = st.session_state.result_local_path
        if local and os.path.isfile(local):
            with open(local, "rb") as f:
                st.download_button(
                    "Download MP4",
                    data=f.read(),
                    file_name=f"seedance_{st.session_state.result_task_id or 'out'}.mp4",
                    mime="video/mp4",
                    use_container_width=True,
                )
        else:
            st.link_button(
                "Open video URL",
                st.session_state.result_video_url,
                use_container_width=True,
            )

        if st.session_state.result_task_id:
            st.caption(f"Task ID: `{st.session_state.result_task_id}`")
