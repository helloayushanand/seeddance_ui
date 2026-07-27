#!/usr/bin/env python3
"""
Thin wrapper — full pipeline lives in streamlit_seedance.py:

  user_prompt → edited_prompt → final_prompt → video

Run the merged app:
  streamlit run streamlit_seedance.py

This file still exposes enhance-only UI for prompt debugging.
"""
from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import List, Optional

import streamlit as st

from video_edit_prompt import (
    GEMINI_MODEL,
    compose_final_prompt,
    enhance_user_prompt,
    gemini_api_key,
)

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

st.set_page_config(page_title="Video Edit Prompt (Gemini)", page_icon="✏️", layout="wide")

st.title("Video edit prompt generator")
st.info(
    "Full flow (enhance → Seedance video) is in `streamlit_seedance.py`. "
    "This page only runs the enhance layer."
)
st.caption(f"Upload Video 1 (+ optional Image 1) → `{GEMINI_MODEL}` → edited + final prompt.")

with st.sidebar:
    st.header("API")
    key_ok = bool(gemini_api_key())
    st.write(f"GEMINI_API_KEY: {'✅' if key_ok else '❌'}")
    model_name = st.text_input("Gemini model", value=GEMINI_MODEL)

col_in, col_out = st.columns(2)

with col_in:
    st.subheader("Input")
    uploaded_video = st.file_uploader("Video 1", type=["mp4", "mov", "webm"], key="video_uploader")
    video_url = st.text_input("Or Video 1 URL", placeholder="https://...mp4")
    if uploaded_video:
        st.video(uploaded_video)

    uploaded_image = st.file_uploader(
        "Image 1 (optional)", type=["png", "jpg", "jpeg", "webp"], key="image_uploader"
    )
    image_url = st.text_input("Or Image 1 URL", placeholder="https://...")
    if uploaded_image:
        st.image(uploaded_image, caption="Image 1", use_container_width=True)

    edit_mode = st.radio(
        "Edit workflow",
        options=["General edit", "Replace item or avatar"],
        key="edit_mode",
    )
    user_intent = st.text_area("User prompt", height=120, key="user_intent")
    generate = st.button("Generate edit prompt", type="primary", use_container_width=True)

with col_out:
    st.subheader("Output")
    for key in ("edit_prompt_full", "edit_prompt_final", "edit_prompt_error", "token_info"):
        if key not in st.session_state:
            st.session_state[key] = None

    if generate:
        st.session_state.edit_prompt_error = None
        st.session_state.edit_prompt_full = None
        st.session_state.edit_prompt_final = None
        st.session_state.token_info = None

        cleanup: List[str] = []
        video_path: Optional[str] = None
        image_path: Optional[str] = None
        try:
            if not key_ok:
                raise RuntimeError("Missing GEMINI_API_KEY.")
            if not (user_intent or "").strip():
                raise RuntimeError("Enter a user prompt.")

            if uploaded_video is not None:
                fd, video_path = tempfile.mkstemp(suffix=Path(uploaded_video.name).suffix or ".mp4")
                os.close(fd)
                Path(video_path).write_bytes(uploaded_video.getvalue())
                cleanup.append(video_path)
            elif (video_url or "").strip():
                url = video_url.strip()
                suffix = Path(url.split("?", 1)[0]).suffix.lower() or ".mp4"
                fd, video_path = tempfile.mkstemp(suffix=suffix)
                os.close(fd)
                urllib.request.urlretrieve(url, video_path)
                cleanup.append(video_path)
            else:
                raise RuntimeError("Upload a video or paste a video URL.")

            if uploaded_image is not None:
                fd, image_path = tempfile.mkstemp(suffix=Path(uploaded_image.name).suffix or ".png")
                os.close(fd)
                Path(image_path).write_bytes(uploaded_image.getvalue())
                cleanup.append(image_path)
            elif (image_url or "").strip().startswith(("http://", "https://")):
                url = image_url.strip()
                suffix = Path(url.split("?", 1)[0]).suffix.lower() or ".png"
                fd, image_path = tempfile.mkstemp(suffix=suffix)
                os.close(fd)
                urllib.request.urlretrieve(url, image_path)
                cleanup.append(image_path)

            with st.status("Calling Gemini…", expanded=True) as status:
                status.write(f"Model: `{model_name}`")
                edited_prompt, token_info = enhance_user_prompt(
                    video_path=video_path,
                    image_path=image_path,
                    user_prompt=user_intent or "",
                    edit_mode=edit_mode,
                    model=(model_name or GEMINI_MODEL).strip(),
                )
                st.session_state.token_info = token_info
                st.session_state.edit_prompt_final = edited_prompt
                st.session_state.edit_prompt_full = compose_final_prompt(
                    edit_mode=edit_mode,
                    edited_prompt=edited_prompt,
                    user_prompt=user_intent or "",
                )
                status.update(label="Prompt ready", state="complete")
            st.success("Edit prompt generated.")
        except Exception as e:
            st.session_state.edit_prompt_error = str(e)
            st.error(str(e))
        finally:
            for p in cleanup:
                try:
                    if p and os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass

    if st.session_state.edit_prompt_error and not generate:
        st.error(st.session_state.edit_prompt_error)

    if st.session_state.token_info:
        ti = st.session_state.token_info
        c1, c2, c3 = st.columns(3)
        c1.metric("Prompt tokens", ti.get("prompt_tokens") or "—")
        c2.metric("Output tokens", ti.get("output_tokens") or "—")
        c3.metric("Total tokens", ti.get("total_tokens") or "—")

    if st.session_state.edit_prompt_full:
        st.markdown("### edited_prompt")
        st.code(st.session_state.edit_prompt_final or "", language=None)
        st.markdown("### final_prompt")
        st.text_area(
            "final_prompt",
            value=st.session_state.edit_prompt_full,
            height=360,
            key="final_edit_prompt_box",
        )
        st.download_button(
            "Download final prompt (.md)",
            data=st.session_state.edit_prompt_full.encode("utf-8"),
            file_name="video_edit_prompt.md",
            mime="text/markdown",
            use_container_width=True,
        )
