"""OpenAI image edit for storyboard frames (gpt-image-2)."""

import base64
from io import BytesIO

import requests
from openai import OpenAI

DEFAULT_STORYBOARD_PROMPT = """
place exactly 4 ornaments onto a christmas tree. place into festive modern living room decorated for christmas. do not change appearance or finish of ornaments. use cheerful red and green theme. show full width of tree. Place evenly onto tree. ornaments should appear small scaled to fit on tree. Blend it properly into the scene. Blend the red color properly into the scene.
""".strip()


def _file_like(image_bytes: bytes, filename: str) -> BytesIO:
    buf = BytesIO(image_bytes)
    name = filename if filename and "." in filename else "image.png"
    buf.name = name
    return buf


def load_storyboard_upload(uploaded_file) -> tuple[bytes, str] | None:
    """Read a Streamlit UploadedFile (or similar) for images.edit. Returns None if missing/empty."""
    if uploaded_file is None:
        return None
    uploaded_file.seek(0)
    data = uploaded_file.read()
    if not data:
        return None
    name = getattr(uploaded_file, "name", None) or "storyboard.png"
    return data, name


def generate_storyboard_image(
    api_key: str,
    image_bytes: bytes,
    filename: str,
    prompt: str,
    *,
    model: str = "gpt-image-2",
    quality: str = "medium",
    size: str = "1024x1024",
) -> bytes:
    client = OpenAI(api_key=api_key)
    img = _file_like(image_bytes, filename)
    try:
        result = client.images.edit(
            model=model,
            image=[img],
            prompt=prompt.strip(),
            quality=quality,
            size=size,
        )
    finally:
        img.close()

    if not result.data:
        raise RuntimeError("OpenAI returned no image data")

    item = result.data[0]
    b64 = getattr(item, "b64_json", None)
    if b64:
        return base64.b64decode(b64)
    url = getattr(item, "url", None)
    if url:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        return r.content
    raise RuntimeError("OpenAI image response had neither b64_json nor url")
