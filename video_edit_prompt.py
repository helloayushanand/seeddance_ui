"""
Gemini enhance layer: user edit intent + Video 1 (+ optional Image 1)
→ Seedance-ready edited prompt paragraph.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, List, Optional

_ROOT = Path(__file__).resolve().parent
GEMINI_MODEL = os.environ.get("GEMINI_EDIT_PROMPT_MODEL", "gemini-3.5-flash")

GENERAL_EDIT_TASK = """
TASK:
You are an expert video-editing prompt engineer working for an AI video-edit model. Your job is to convert a user's plain-language edit request into one precise, execution-ready video-edit instruction. You have Video 1 (the source video), an optional Image 1 (a visual reference), and the user's request describing an addition, removal, modification, dialogue/music change, subject/product action change, background change, or specific item replacement as inputs. Now you have to analyse Video 1 to decide whether the change applies to the complete video or only a specific moment, and then translate the user's intent into a single clear instruction that names the change itself, what must stay untouched, and a timeline only when the change is frame-specific.
""".strip()

GENERAL_EDIT_GUIDELINES = """
## EDIT GUIDELINES
- Decide the scope first: if the change applies to the complete video, do not include any timestamp, time range, or start/middle/end label.
- Include a timeline only when the change is limited to a specific moment or frame — then use the timestamp/range if clear, or start/middle/end anchored to a visible action.
- Identify which subject, object, background, or region of the frame is being changed.
- For action or subject changes: first describe the original action sequence exactly as it appears in Video 1 step by step, then describe the new action sequence step by step in the same level of detail.
- For background changes: state the original background briefly, then the new background; use Image 1 when provided as the background reference.
- For item replacement: state what item in Video 1 is replaced and what replaces it; use Image 1 when provided as the replacement reference.
- For dialogue or music: specify the original line/track, the new line/track, and the speaker or source; add start/end timing only if the audio change is partial, not full-video.
- State only the requested change; do not add or infer anything beyond what the user specifies.
""".strip()

GENERAL_EDIT_CONSTRAINTS = """
## EDIT CONSTRAINTS
- Do not invent details that are not requested or not visible in Video 1 / Image 1.
- Do not invent a timestamp or timeline when the change applies to the complete video.
- Do not change any frame, subject, motion, camera work, lighting, reflection, shadow, or audio other than the one requested change.
- For frame-specific edits, preserve continuity immediately before and after the edit window.
- If the request only implies a partial change, apply exactly that scope and leave the remainder of the shot untouched.
- End the instruction with: "Keep everything else exactly the same; do not change anything else."
""".strip()

REPLACEMENT_TASK = """
TASK:
You are an expert video-editing prompt engineer working for an AI video-edit model. Your job is to convert a user's plain-language request into one precise, execution-ready replacement instruction. You have Video 1 (the source video), an optional Image 1 (the replacement reference), and the user's request naming the product, prop, or avatar to swap as inputs. Now you have to analyse Video 1 to locate the item being replaced along with its position, motion, and lighting, and then translate the user's intent into a single clear instruction that names what is replaced, what replaces it, and what must stay untouched. Include a timeline only when the replacement is frame-specific; omit it for a complete-video replacement.
""".strip()

REPLACEMENT_GUIDELINES = """
## REPLACEMENT GUIDELINES
- State what in Video 1 is replaced (item, prop, product, or avatar) and what replaces it, using Image 1 as the identity reference when provided.
- If the replacement applies throughout Video 1, do not include any timestamp or timeline.
- Include a timestamp or start/middle/end only when the replacement happens at a specific moment in Video 1.
- Mention only the visible identifying traits (shape, color, material) needed to distinguish the replacement — do not add unrequested descriptive detail.
- If replacing a person/avatar, keep the original pose, action, and scene interaction; only the identity/appearance changes.
""".strip()

REPLACEMENT_CONSTRAINTS = """
## REPLACEMENT CONSTRAINTS
- Do not over-describe the replacement item; do not add unrequested detail about it.
- Do not invent a timeline when the replacement applies to the complete video.
- Match the original position, scale, motion, interaction, perspective, shadows, and lighting.
- Do not change any other prop, avatar, frame, action, camera movement, background, audio, or duration.
- End the instruction with: "Keep everything else exactly the same; do not change anything else."
""".strip()

SEEDANCE_GENERAL_PATTERN = """
## MODEL PATTERN (Seedance 2.0)
Use one formula below as inspiration. Write action keywords in CAPITALS.
Timeline: include AT [timestamp] only for frame-specific edits; omit it for full-video changes.

--- ADD ---
Formula: ADD [element] [AT timestamp if needed] IN [location] of Video 1.
Example: ADD a coffee cup ON the kitchen counter of Video 1.

--- REMOVE ---
Formula: REMOVE [element] FROM [location] IN Video 1 [AT timestamp if needed], keeping the rest unchanged.
Example: REMOVE the water bottle FROM the table IN Video 1, keeping the rest unchanged.

--- MODIFY (action) ---
Formula: [AT timestamp if needed] IN Video 1, [brief scene before]. CHANGE [subject]'s action FROM [original steps] TO [new steps].
Example: AT 00:04 IN Video 1, as the man sits down, CHANGE his action FROM drinking coffee TO placing the cup back and walking away.

--- MODIFY (attribute) ---
Formula: [AT timestamp if needed] IN Video 1, CHANGE [element] FROM [original] TO [new].
Example: IN Video 1, CHANGE the wall color FROM white TO pale blue.

--- CHANGE BACKGROUND ---
Formula: [AT timestamp if needed] IN Video 1, REPLACE the [existing background] WITH [new background or Image 1], KEEPING subject, motion, and camera work unchanged.
Example: IN Video 1, REPLACE the existing background WITH a garden scene FROM Image 1, KEEPING subject and camera work unchanged.

--- REPLACE ITEM ---
Formula: REPLACE [item in Video 1] WITH [replacement or Image 1] [AT timestamp if needed], KEEPING motion, position, lighting, and camera work unchanged.
Example: REPLACE the perfume bottle IN Video 1 WITH the face cream FROM Image 1, KEEPING hand motions and camera work unchanged.

--- DIALOGUE / AUDIO ---
Formula: [AT timestamp if needed] IN Video 1, CHANGE [dialogue/music] FROM [original] TO [new], keeping other audio unchanged.
Example: IN Video 1, CHANGE the background music TO an acoustic guitar track, keeping the dialogue unchanged.
""".strip()

SEEDANCE_GENERAL_CONSTRAINTS = """
## MODEL CONSTRAINTS
- Parts of Video 1 not mentioned in the chosen template remain unchanged by default.
- Use only one template; do not combine multiple change types in one instruction.
- Never force a timestamp into the prompt when the change covers the complete video.
""".strip()

SEEDANCE_REPLACEMENT_PATTERN = """
## MODEL PATTERN (Seedance 2.0)
Below is the formula for a replacement. Use it as inspiration — write the prompt in proper sentence structure using the formula's parts. Write every action keyword in CAPITALS.

Timeline rule: include AT [timestamp] only when the replacement is frame-specific. If the replacement applies to the complete video, omit the timeline.

--- REPLACE ---
Formula (full video):  REPLACE [element being swapped] IN Video 1 WITH [replacement element or Image 1], KEEPING the original motion, position, perspective, lighting, and camera work unchanged.
Formula (specific moment):  REPLACE [element being swapped] IN Video 1 WITH [replacement element or Image 1] AT [timestamp], KEEPING the original motion, position, perspective, lighting, and camera work unchanged.
Example (full video):  REPLACE the perfume bottle held by the model IN Video 1 WITH the face cream FROM Image 1 throughout the video, KEEPING all original hand motions, camera angles, and lighting unchanged.
Example (specific moment):  REPLACE the perfume bottle held by the model IN Video 1 WITH the face cream FROM Image 1 AT 00:05, KEEPING all original hand motions, camera angles, and lighting unchanged.
""".strip()

SEEDANCE_REPLACEMENT_CONSTRAINTS = """
## MODEL CONSTRAINTS
- Parts of Video 1 not mentioned in the template remain unchanged by default.
- Include timing only for frame-specific replacements; otherwise preserve motion and lighting without inventing a timestamp.
""".strip()

FINAL_OUTPUT_RULES = """
## FINAL CONSTRAINTS
- Do not expand the user's intent or invent visual details beyond what is requested or visible.
- Do not add alternatives, explanations, headings, labels, lists, or Markdown.
- Do not invent a timeline. Include WHEN only if the change is frame-specific; omit WHEN if the change applies to the complete video.
- Fold every requested change into one cohesive instruction.
- Before answering, silently verify: scope is clear (full video vs specific moment), only the requested change is stated, and the preservation statement is present; do not show this check in the output.

## OUTPUT
Return only one plain English paragraph. Write every action keyword in CAPITALS (ADD, REMOVE, CHANGE, REPLACE, KEEP, FROM, TO, AT, IN, WITH).

The paragraph must make these things immediately clear:
1. WHAT changes (the subject, item, background, or element being changed)
2. WHAT the original sequence/state is (describe it step by step as it appears in Video 1)
3. WHAT the new sequence/state is (describe it step by step as it should appear after the change)
4. WHEN it happens — include this only for a frame-specific change; skip it for a complete-video change

End every paragraph with: KEEP everything else exactly the same; do not change anything else.
""".strip()

GENERAL_UI_GUIDELINES = """
- Do apply the edit only to the requested scope of Video 1.
- Do include a timeline only for frame-specific changes; omit timestamps when the change covers the complete video.
- Do preserve continuity for frame-specific edits (motion, occlusion, perspective, camera work) right before and after the edit.
- Do preserve all unmentioned frames, subjects, lighting, audio, and background.
- Don't change any other frames like start, end, etc., unless explicitly specified in the prompt by the user.
- Don't add details that are not requested or not visible in Video 1 / Image 1.
""".strip()

REPLACEMENT_UI_GUIDELINES = """
- Do replace only the requested item, product, prop, or avatar in Video 1.
- Do include a timeline only when the replacement is frame-specific; omit it for full-video replacements.
- Do preserve its original placement, motion, scale, perspective, shadows, and lighting.
- Don't alter unrelated frames, subjects, background, camera work, audio, or timing.
- Don't change any other frames like start, end, etc., unless explicitly specified in the prompt by the user.
- Don't redesign the replacement or add detail beyond the supplied reference.
""".strip()


def system_prompt_for_inputs(*, has_image: bool, edit_mode: str) -> str:
    if edit_mode == "Replace item or avatar":
        task = REPLACEMENT_TASK
        guidelines = REPLACEMENT_GUIDELINES
        edit_constraints = REPLACEMENT_CONSTRAINTS
        model_pattern = SEEDANCE_REPLACEMENT_PATTERN
        model_constraints = SEEDANCE_REPLACEMENT_CONSTRAINTS
    else:
        task = GENERAL_EDIT_TASK
        guidelines = GENERAL_EDIT_GUIDELINES
        edit_constraints = GENERAL_EDIT_CONSTRAINTS
        model_pattern = SEEDANCE_GENERAL_PATTERN
        model_constraints = SEEDANCE_GENERAL_CONSTRAINTS

    reference_media = (
        "Image 1 is provided — use it only as the reference the user's request depends on."
        if has_image
        else "Image 1 is not provided — do not mention or invent Image 1."
    )
    return "\n\n".join(
        [
            task,
            guidelines,
            edit_constraints,
            model_pattern,
            model_constraints,
            f"## REFERENCE MEDIA\n{reference_media}",
            FINAL_OUTPUT_RULES,
        ]
    )


def compose_final_prompt(*, edit_mode: str, edited_prompt: str, user_prompt: str) -> str:
    """user_prompt + edited_prompt → final_prompt sent to Seedance."""
    guidelines = (
        REPLACEMENT_UI_GUIDELINES
        if edit_mode == "Replace item or avatar"
        else GENERAL_UI_GUIDELINES
    )
    return (
        f"## Guidelines (Do/Don't)\n{guidelines}\n\n"
        f"## Edited Prompt\n{edited_prompt.strip()}\n\n"
        f"## User Prompt\n{user_prompt.strip()}"
    )


def gemini_api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def _guess_mime(path: str, kind: str) -> str:
    ext = Path(path).suffix.lower()
    if kind == "video":
        return {
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
        }.get(ext, "video/mp4")
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/jpeg")


def _wait_file_active(client: Any, uploaded: Any, timeout_s: int = 300) -> Any:
    name = getattr(uploaded, "name", None) or (
        uploaded.get("name") if isinstance(uploaded, dict) else None
    )
    if not name:
        return uploaded
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        meta = client.files.get(name=name)
        state = getattr(meta, "state", None) or (
            meta.get("state") if isinstance(meta, dict) else None
        )
        state_s = str(state or "")
        if state_s.endswith("ACTIVE") or state_s == "ACTIVE":
            return meta
        if "FAILED" in state_s:
            raise RuntimeError(f"Gemini file processing failed: {meta}")
        time.sleep(2)
    raise TimeoutError("Gemini file did not become ACTIVE in time.")


def _extract_text(response: Any) -> str:
    text = (getattr(response, "text", None) or "").strip()
    if text:
        return text
    for cand in getattr(response, "candidates", None) or []:
        for part in getattr(getattr(cand, "content", None), "parts", None) or []:
            t = getattr(part, "text", None)
            if t:
                return str(t).strip()
    return ""


def enhance_user_prompt(
    *,
    video_path: str,
    image_path: Optional[str] = None,
    user_prompt: str = "",
    edit_mode: str = "General edit",
    model: str = GEMINI_MODEL,
) -> tuple[str, dict]:
    """
    Enhance layer: user_prompt → edited_prompt (Gemini paragraph).
    Returns (edited_prompt, token_info).
    """
    from google import genai
    from google.genai import types

    if not user_prompt.strip():
        raise ValueError("User prompt is required.")

    key = gemini_api_key()
    if not key:
        raise RuntimeError("Missing GEMINI_API_KEY (set in env or env.json).")

    client = genai.Client(api_key=key)

    uploaded_video = client.files.upload(file=video_path)
    video_meta = _wait_file_active(client, uploaded_video)

    has_image = bool(image_path and os.path.isfile(image_path))
    system = system_prompt_for_inputs(has_image=has_image, edit_mode=edit_mode)

    parts: List[Any] = [
        types.Part.from_text(text=system),
        types.Part.from_text(
            text=(
                "## REFERENCE: VIDEO 1 (required)\n"
                "Inspect it only to locate the requested edit and preserve its existing content."
            )
        ),
        types.Part.from_uri(
            file_uri=video_meta.uri,
            mime_type=getattr(video_meta, "mime_type", None) or _guess_mime(video_path, "video"),
        ),
    ]

    if has_image:
        uploaded_img = client.files.upload(file=image_path)
        img_meta = _wait_file_active(client, uploaded_img)
        parts.append(
            types.Part.from_text(
                text=(
                    "## REFERENCE: IMAGE 1 (optional)\n"
                    "Use it only as the visual reference requested by the user."
                )
            )
        )
        parts.append(
            types.Part.from_uri(
                file_uri=img_meta.uri,
                mime_type=getattr(img_meta, "mime_type", None)
                or _guess_mime(image_path, "image"),
            )
        )

    parts.append(
        types.Part.from_text(
            text=(
                f"## SELECTED WORKFLOW\n{edit_mode}\n\n"
                f"## USER PROMPT\n{user_prompt.strip()}"
            )
        )
    )

    response = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
    )
    text = _extract_text(response)
    if not text:
        raise RuntimeError("Gemini returned an empty response.")

    usage = getattr(response, "usage_metadata", None)
    token_info = {
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
    }
    return text.strip(), token_info
