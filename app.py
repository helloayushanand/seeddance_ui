import streamlit as st
import api
import auth
import history
import dino_game
import storyboard

MAX_IMAGES = 9
MAX_AUDIO = 9
COST_PER_M_STD = 7.0
COST_PER_M_HD = 7.7

# LangGraph / Gemini chat (“Refine with AI”) — off by default
ENABLE_PROMPT_BOT = False


# ── Helpers ───────────────────────────────────────────────────────────────────
def _extract_video_url(result) -> str | None:
    try:
        return result.content.video_url
    except Exception:
        pass
    try:
        return result.content["video_url"]
    except Exception:
        pass
    try:
        raw = result.model_dump() if hasattr(result, "model_dump") else {}
        return raw.get("content", {}).get("video_url")
    except Exception:
        pass
    return None


def _calculate_cost(tokens: int, resolution: str) -> float:
    rate = COST_PER_M_HD if resolution == "1080p" else COST_PER_M_STD
    return (tokens / 1_000_000) * rate


# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Seedance 2.0 Demo", page_icon="🎬", layout="wide")

st.markdown("""
<style>
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
</style>
""", unsafe_allow_html=True)


# ── Session state defaults ────────────────────────────────────────────────────
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "active_task" not in st.session_state:
    st.session_state.active_task = None
if "show_game" not in st.session_state:
    st.session_state.show_game = False

# ─────────────────────────────────────────────────────────────────────────────
# LOGIN GATE
# ─────────────────────────────────────────────────────────────────────────────
if not st.session_state.logged_in:
    st.markdown("<br><br>", unsafe_allow_html=True)
    col = st.columns([1, 1, 1])[1]
    with col:
        st.title("🎬 Seedance 2.0")
        st.caption("BytePlus ModelArk · Video Generation")
        st.divider()
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.button("Login", type="primary", width="stretch"):
            if auth.verify(username, password):
                st.session_state.logged_in = True
                st.session_state.username = username
                st.rerun()
            else:
                st.error("Invalid username or password")
    st.stop()

# API key loaded from env (never exposed in UI)
API_KEY = auth.get_api_key()

# ── Polling fragment ──────────────────────────────────────────────────────────
@st.fragment(run_every=30)
def _poll():
    task = st.session_state.get("active_task")
    if not task or task["status"] in ("succeeded", "failed"):
        return
    import datetime
    print(f"[POLL] {datetime.datetime.now().strftime('%H:%M:%S')} checking task {task['id']} | current status: {task['status']}")
    try:
        client = api.get_client(API_KEY)
        result = api.get_task(client, task["id"])
        s = result.status
        print(f"[POLL] API response status: {s} | raw result: {result}")
        if s == "succeeded":
            video_url = _extract_video_url(result)
            print(f"[POLL] succeeded — video_url: {video_url}")
            tokens = getattr(getattr(result, "usage", None), "total_tokens", None) or 0
            cost = _calculate_cost(tokens, task.get("resolution", "720p"))
            st.session_state.active_task.update({"status": "succeeded", "video_url": video_url,
                                                  "tokens": tokens, "cost": cost})
            history.update_task(task["id"], "succeeded", video_url, tokens=tokens, cost=cost)
            st.rerun()
        elif s == "failed":
            print(f"[POLL] task failed | full result: {result}")
            st.session_state.active_task["status"] = "failed"
            history.update_task(task["id"], "failed")
            st.rerun()
        else:
            print(f"[POLL] still in progress: {s}")
            st.session_state.active_task["status"] = s
    except Exception as e:
        import traceback
        print(f"[POLL] exception: {e}")
        traceback.print_exc()

_poll()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🎬 Seedance 2.0")
    col1, col2 = st.columns([2, 1])
    col1.caption(f"Logged in as **{st.session_state.username}**")
    if col2.button("Logout"):
        st.session_state.logged_in = False
        st.session_state.active_task = None
        st.rerun()
    st.divider()

    # ── Active task ───────────────────────────────────────────────────────────
    st.subheader("Tasks")
    active = st.session_state.active_task
    if active:
        status_badge = {"succeeded": "✅", "failed": "❌", "pending": "⏳", "running": "🔄", "queued": "⏳"}.get(active["status"], "⏳")
        with st.container(border=True):
            st.caption(f"Latest · {active['id'][-8:]}")
            st.markdown(f"**{status_badge} {active['status'].capitalize()}**")
            if active.get("prompt"):
                preview = active["prompt"][:80] + ("…" if len(active["prompt"]) > 80 else "")
                st.caption(f"_{preview}_")
            if active["status"] == "succeeded":
                if active.get("video_url"):
                    st.video(active["video_url"])
                    st.markdown(f"[Download]({active['video_url']})")
                    st.code(active["video_url"], language=None)
                if active.get("cost") is not None:
                    st.caption(f"Cost: ${active['cost']:.4f} · {active.get('tokens', 0):,} tokens")
            elif active["status"] == "failed":
                st.error("Generation failed")
            else:
                st.caption("Polling every 30s…")

    # ── History (auto-polls every 30s) ───────────────────────────────────────
    @st.fragment(run_every=30)
    def _sidebar_history():
        import datetime
        tasks = history.load(username=st.session_state.username)
        if not tasks:
            st.caption("No tasks yet.")
            return

        # Auto-poll any pending/running tasks
        in_progress = [t for t in tasks if t["status"] not in ("succeeded", "failed")]
        if in_progress:
            st.caption(f"🔄 Polling {len(in_progress)} task(s) · {datetime.datetime.now().strftime('%H:%M:%S')}")
            client = api.get_client(API_KEY)
            changed = False
            for t in in_progress:
                try:
                    result = api.get_task(client, t["id"])
                    new_status = result.status
                    t["status"] = new_status
                    if new_status == "succeeded":
                        vurl = _extract_video_url(result)
                        tokens = getattr(getattr(result, "usage", None), "total_tokens", None) or 0
                        resolution = t.get("settings", {}).get("resolution", "720p")
                        cost = _calculate_cost(tokens, resolution)
                        history.update_task(t["id"], new_status, vurl, tokens=tokens, cost=cost)
                        t["video_url"] = vurl
                        t["tokens"] = tokens
                        t["cost"] = cost
                        changed = True
                    elif new_status == "failed":
                        history.update_task(t["id"], new_status)
                        changed = True
                    else:
                        history.update_task(t["id"], new_status)
                except Exception as e:
                    import traceback
                    print(f"[SIDEBAR_POLL] exception for task {t['id']}: {e}")
                    traceback.print_exc()
            if changed:
                st.rerun()
        else:
            st.caption("Recent")

        for t in tasks[:10]:
            s_badge = {"succeeded": "✅", "failed": "❌", "pending": "⏳", "running": "🔄", "queued": "⏳"}.get(t["status"], "❓")
            with st.expander(f"{s_badge} {t['id'][-12:]}"):
                st.caption(t["created_at"][:16].replace("T", " "))
                prompt_preview = t["prompt"][:60] + ("…" if len(t["prompt"]) > 60 else "")
                st.caption(f"_{prompt_preview}_")
                if len(t["prompt"]) > 60:
                    with st.expander("Full prompt"):
                        st.write(t["prompt"])

                # Input images
                all_images = t.get("image_paths", []) + [u for u in t.get("image_urls", []) if u.startswith("http")]
                if all_images:
                    img_cols = st.columns(min(len(all_images), 3))
                    for i, src in enumerate(all_images):
                        try:
                            img_cols[i % 3].image(src, width="stretch")
                        except Exception:
                            pass

                all_audios = list(t.get("audio_paths", []))
                all_audios += [
                    u
                    for u in (t.get("audio_urls_ref") or [])
                    if isinstance(u, str) and u.startswith("http")
                ]
                legacy_audio = t.get("audio_url_ref")
                if legacy_audio and isinstance(legacy_audio, str) and legacy_audio.startswith("http"):
                    all_audios.insert(0, legacy_audio)
                if all_audios:
                    st.caption("Reference audio")
                    for src in all_audios:
                        try:
                            st.audio(src)
                        except Exception:
                            pass

                # Load video if succeeded but URL missing
                if t["status"] == "succeeded" and not t.get("video_url"):
                    st.caption("⚠️ Video URL missing — will retry next poll")

                if t.get("video_url"):
                    st.video(t["video_url"])
                    st.markdown(f"[Download]({t['video_url']})")
                    st.code(t["video_url"], language=None)
                    if t.get("cost") is not None:
                        st.caption(f"Cost: ${t['cost']:.4f} · {t.get('tokens', 0):,} tokens")
                    elif t.get("tokens"):
                        cost = _calculate_cost(t["tokens"], t.get("settings", {}).get("resolution", "720p"))
                        st.caption(f"Cost: ${cost:.4f} · {t['tokens']:,} tokens")

                if t["status"] not in ("succeeded", "failed"):
                    st.caption(f"Status: **{t['status']}** · next check in ~30s")

    _sidebar_history()

# ─────────────────────────────────────────────────────────────────────────────
# GENERATE TAB
# ─────────────────────────────────────────────────────────────────────────────
GEMINI_KEY = auth.get_gemini_key()

st.header("Generate Video")

# ── Session state for prompt (optional AI-refined value when bot is on) ───────
if "_final_prompt" not in st.session_state:
    st.session_state._final_prompt = None

layout_main, layout_story = st.columns([2.25, 1], gap="large")

# ── Main column: reference images ───────────────────────────────────────────
with layout_main:
    st.subheader(f"Reference Images (up to {MAX_IMAGES})")
    image_input_mode = st.radio("Image input mode", ["Upload files", "Paste URLs"], horizontal=True, key="img_mode")
    image_urls: list[str] = []
    image_paths: list[str] = []
    uploaded_images = None

    if image_input_mode == "Upload files":
        uploaded_images = st.file_uploader("Upload images", type=["jpg", "jpeg", "png", "webp"],
                                           accept_multiple_files=True, label_visibility="collapsed")
        if uploaded_images:
            if len(uploaded_images) > MAX_IMAGES:
                st.warning(f"Only the first {MAX_IMAGES} images will be used.")
                uploaded_images = uploaded_images[:MAX_IMAGES]
            cols = st.columns(min(len(uploaded_images), 3))
            for i, f in enumerate(uploaded_images):
                cols[i % 3].image(f, width="stretch", caption=f.name)
            for f in uploaded_images:
                file_bytes = f.read()
                image_urls.append(api.file_to_data_uri(file_bytes, f.name))
                image_paths.append(api.save_upload(file_bytes, f.name))
    else:
        st.caption("One URL per line, up to 9")
        raw_urls = st.text_area("Image URLs", height=100, label_visibility="collapsed",
                                placeholder="https://example.com/image1.jpg", key="raw_urls")
        if raw_urls.strip():
            image_urls = [u.strip() for u in raw_urls.strip().splitlines() if u.strip()][:MAX_IMAGES]
            if image_urls:
                cols = st.columns(min(len(image_urls), 3))
                for i, url in enumerate(image_urls):
                    try:
                        cols[i % 3].image(url, width="stretch")
                    except Exception:
                        cols[i % 3].caption(f"Image {i+1} (preview unavailable)")

# ── Side panel: Storyboard (OpenAI gpt-image-2) ───────────────────────────────
with layout_story:
    with st.container(border=True):
        st.subheader("Storyboard")
        st.caption(
            "OpenAI **images.edit** (`gpt-image-2`) using the **storyboard source image** below "
            "(separate from video reference images). Set `OPENAI_API_KEY` in `env.json`."
        )
        OPENAI_KEY = auth.get_openai_key()
        sb_image = st.file_uploader(
            "Storyboard source image",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=False,
            help="This image is sent to OpenAI for editing — not the same as Reference Images for video.",
            key="sb_source_image",
            label_visibility="visible",
        )
        if sb_image:
            st.image(sb_image, caption=sb_image.name, width="stretch")
        if "sb_prompt" not in st.session_state:
            st.session_state.sb_prompt = storyboard.DEFAULT_STORYBOARD_PROMPT
        sb_prompt = st.text_area("Storyboard prompt", height=160, key="sb_prompt")
        sb_quality = st.selectbox("Quality", ["low", "medium", "high"], index=1, key="sb_quality")
        sb_size = st.selectbox("Size", ["1024x1024", "1024x1536", "1536x1024"], index=0, key="sb_size")
        create_sb = st.button("Create story board", type="secondary", width="stretch", key="create_storyboard")

        if create_sb:
            if not OPENAI_KEY or OPENAI_KEY == "your-openai-api-key-here":
                st.error("Add a valid `OPENAI_API_KEY` to `env.json`.")
            elif not sb_prompt.strip():
                st.error("Enter a storyboard prompt.")
            else:
                loaded = storyboard.load_storyboard_upload(sb_image)
                if not loaded:
                    st.error("Upload a **Storyboard source image** (separate from video reference images).")
                else:
                    img_bytes, fname = loaded
                    with st.spinner("Creating storyboard image…"):
                        try:
                            out = storyboard.generate_storyboard_image(
                                OPENAI_KEY,
                                img_bytes,
                                fname,
                                sb_prompt,
                                quality=sb_quality,
                                size=sb_size,
                            )
                            st.session_state["_storyboard_png"] = out
                        except Exception as e:
                            st.session_state["_storyboard_png"] = None
                            st.error(f"Storyboard failed: {e}")

        if st.session_state.get("_storyboard_png"):
            st.image(st.session_state["_storyboard_png"], caption="Storyboard result", width="stretch")
            st.download_button(
                label="Download storyboard PNG",
                data=st.session_state["_storyboard_png"],
                file_name="storyboard.png",
                mime="image/png",
                key="download_storyboard",
            )

# ── Main column (continued): bot, prompt, generate ───────────────────────────
with layout_main:
    st.divider()

    # ── Chat interface (Gemini / LangGraph) — optional ─────────────────────────
    if ENABLE_PROMPT_BOT:
        import re

        import bot as bot_module

        if "_bot_thread_suffix" not in st.session_state:
            st.session_state._bot_thread_suffix = "0"
        thread_id = f"{st.session_state.username}_prompt_{st.session_state._bot_thread_suffix}"

        st.subheader("Refine with AI")
        st.caption("Describe your video idea — the bot will help craft a production-ready Seedance prompt.")

        if not GEMINI_KEY or GEMINI_KEY == "your-gemini-api-key-here":
            st.warning("Add your `GEMINI_API_KEY` to `env.json` to use the bot.")
            st.stop()

        history_msgs = bot_module.get_history(GEMINI_KEY, thread_id)

        chat_container = st.container(height=450)
        with chat_container:
            if not history_msgs:
                st.markdown(
                    """
                    <div style='text-align:center; padding: 60px 20px; color: #888;'>
                        <div style='font-size: 40px'>🎬</div>
                        <div style='font-size: 16px; margin-top: 12px'>Tell me what you want to create.</div>
                        <div style='font-size: 13px; margin-top: 6px'>Share a product, mood, brand reference — anything.</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            for msg in history_msgs:
                with st.chat_message(msg["role"]):
                    content = msg["content"]
                    prompts = bot_module.extract_prompts(content)
                    if prompts and msg["role"] == "assistant":
                        display = re.sub(r"<PROMPT>.*?</PROMPT>", "", content, flags=re.DOTALL).strip()
                        if display:
                            st.markdown(display)
                        for i, p in enumerate(prompts):
                            st.markdown("**Generated Prompt:**")
                            st.code(p, language=None)
                            btn_key = f"use_prompt_{hash(p)}_{i}"
                            if st.button("→ Use this prompt", key=btn_key, type="primary"):
                                st.session_state._final_prompt = p
                                st.rerun()
                    else:
                        st.markdown(content)

            user_input = st.chat_input("Describe your video idea…")

        if user_input:
            print(f"[APP] user_input received: {user_input[:50]}...")
            print(f"[APP] image_input_mode: {image_input_mode}")
            print(f"[APP] uploaded_images: {uploaded_images}")

            img_bytes_list = []
            if image_input_mode == "Upload files" and uploaded_images:
                for f in uploaded_images:
                    f.seek(0)
                    img_bytes_list.append(f.read())
            print(f"[APP] img_bytes_list length: {len(img_bytes_list)}")
            print(f"[APP] thread_id: {thread_id}")

            with st.spinner("Thinking…"):
                try:
                    print("[APP] calling bot_module.chat...")
                    bot_module.chat(GEMINI_KEY, user_input, img_bytes_list, thread_id)
                    print("[APP] bot_module.chat completed successfully")
                    st.rerun()
                except Exception as e:
                    print(f"[APP] bot error: {e}")
                    import traceback

                    traceback.print_exc()
                    st.error(f"Bot error: {e}")

        if history_msgs:
            if st.button("🗑 Clear conversation", type="secondary", width="stretch"):
                import time as _time

                st.session_state._bot_thread_suffix = str(int(_time.time()))
                st.rerun()

        st.divider()

    # ── Prompt display and generation settings ────────────────────────────────
    col_left, col_right = st.columns([1, 1], gap="large")

    with col_left:
        st.subheader("Prompt")
        if st.session_state._final_prompt:
            st.info("✨ Using AI-refined prompt")
            st.code(st.session_state._final_prompt, language=None)
            st.caption("Or override with manual prompt below:")
        final_prompt = st.text_area(
            "Manual prompt",
            height=80,
            label_visibility="collapsed",
            value=st.session_state._final_prompt or "Create a cinematic video using this scene",
            key="_manual_prompt",
        )
        if final_prompt != st.session_state._final_prompt:
            final_prompt_to_use = final_prompt
        else:
            final_prompt_to_use = st.session_state._final_prompt or final_prompt

        st.subheader("Reference Video (optional)")
        video_input_mode = st.radio(
            "Video input mode", ["Upload file", "Paste URL"], horizontal=True, key="vid_mode"
        )
        ref_video_url: str | None = None

        if video_input_mode == "Upload file":
            uploaded_video = st.file_uploader(
                "Upload video", type=["mp4", "mov", "avi", "webm"], label_visibility="collapsed"
            )
            if uploaded_video:
                st.video(uploaded_video)
                ref_video_url = api.file_to_data_uri(uploaded_video.read(), uploaded_video.name)
        else:
            v_url = st.text_input(
                "Video URL",
                placeholder="https://example.com/video.mp4",
                label_visibility="collapsed",
            )
            if v_url.strip():
                ref_video_url = v_url.strip()

        st.subheader(f"Reference Audio (optional, up to {MAX_AUDIO})")
        audio_input_mode = st.radio(
            "Audio input mode", ["Upload files", "Paste URLs"], horizontal=True, key="aud_mode"
        )
        ref_audio_urls: list[str] = []
        audio_paths: list[str] = []

        if audio_input_mode == "Upload files":
            uploaded_audios = st.file_uploader(
                "Upload audio",
                type=["mp3", "wav", "aac", "m4a"],
                accept_multiple_files=True,
                label_visibility="collapsed",
            )
            if uploaded_audios:
                if len(uploaded_audios) > MAX_AUDIO:
                    st.warning(f"Only the first {MAX_AUDIO} audio files will be used.")
                    uploaded_audios = uploaded_audios[:MAX_AUDIO]
                for f in uploaded_audios:
                    st.caption(f.name)
                    st.audio(f)
                for f in uploaded_audios:
                    f.seek(0)
                    file_bytes = f.read()
                    ref_audio_urls.append(api.file_to_data_uri(file_bytes, f.name))
                    audio_paths.append(api.save_upload(file_bytes, f.name))
        else:
            st.caption("One URL per line")
            raw_audio_urls = st.text_area(
                "Audio URLs",
                height=80,
                label_visibility="collapsed",
                placeholder="https://example.com/track1.mp3",
                key="raw_audio_urls",
            )
            if raw_audio_urls.strip():
                ref_audio_urls = [
                    u.strip()
                    for u in raw_audio_urls.strip().splitlines()
                    if u.strip()
                ][:MAX_AUDIO]

    with col_right:
        st.subheader("Settings")
        ratio = st.selectbox("Aspect ratio", ["16:9", "9:16", "1:1", "4:3", "3:4"])
        resolution = st.selectbox("Resolution", ["1080p", "720p", "480p"])
        duration = st.number_input("Duration (seconds)", min_value=3, max_value=15, value=5, step=1)
        generate_audio = st.toggle("Generate audio", value=True)
        watermark = st.toggle("Watermark", value=False)

        st.divider()
        st.caption(f"Images: {len(image_urls)} / {MAX_IMAGES}")
        st.caption(f"Video ref: {'yes' if ref_video_url else 'none'}")
        st.caption(f"Audio ref: {len(ref_audio_urls)} / {MAX_AUDIO}")
        rate = COST_PER_M_HD if resolution == "1080p" else COST_PER_M_STD
        st.caption(f"Billing rate: ${rate}/M tokens")
        st.divider()

        submit = st.button("🚀 Generate", type="primary", width="stretch")

    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))

    active_check = st.session_state.get("active_task")
    if (
        active_check
        and active_check["status"] not in ("succeeded", "failed")
        and not st.session_state.show_game
    ):
        if st.button("🦕 Open dino game", width="stretch"):
            st.session_state.show_game = True
            st.rerun()

    active = st.session_state.get("active_task")
    if st.session_state.show_game and active and active["status"] not in ("succeeded", "failed"):
        st.markdown("<div id='dino-section'></div>", unsafe_allow_html=True)
        st.markdown("---")
        col_title, col_close = st.columns([4, 1])
        col_title.subheader("🦕 Dino Game — task is still running in the background")
        if col_close.button("✕ Back to app", type="primary"):
            st.session_state.show_game = False
            st.rerun()
        dino_game.render(height=230)
        st.caption("Space / ↑ to jump · ↓ to duck · Your video is being generated in the sidebar")
        import streamlit.components.v1 as components

        components.html(
            "<script>window.parent.document.getElementById('dino-section')?.scrollIntoView({behavior:'smooth'});</script>",
            height=0,
        )
    elif st.session_state.show_game:
        st.session_state.show_game = False

    if submit:
        if not image_urls:
            st.error("Please provide at least one reference image.")
        elif not final_prompt_to_use.strip():
            st.error("Please enter or select a prompt.")
        else:
            settings = {
                "ratio": ratio,
                "resolution": resolution,
                "duration": int(duration),
                "generate_audio": generate_audio,
                "watermark": watermark,
            }
            client = api.get_client(API_KEY)
            try:
                task = api.create_task(
                    client,
                    prompt=final_prompt_to_use,
                    image_urls=image_urls,
                    video_url=ref_video_url,
                    audio_urls=ref_audio_urls,
                    **settings,
                )
                task_id = task["id"]
                st.session_state.active_task = {
                    "id": task_id,
                    "status": "pending",
                    "video_url": None,
                    "cost": None,
                    "tokens": None,
                    "resolution": resolution,
                    "prompt": final_prompt_to_use,
                }
                history.add_task(
                    task_id,
                    final_prompt_to_use,
                    image_urls,
                    image_paths,
                    ref_video_url,
                    ref_audio_urls,
                    audio_paths,
                    settings,
                    username=st.session_state.username,
                )
                st.session_state["flash"] = f"Task submitted: `{task_id}`"
                st.session_state.show_game = True
                st.rerun()
            except Exception as e:
                st.error(f"Failed to create task: {e}")

