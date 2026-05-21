import json
import os
from datetime import datetime

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "history.json")


def load(username: str | None = None) -> list[dict]:
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE) as f:
        tasks = json.load(f)
    if username:
        tasks = [t for t in tasks if t.get("username") == username]
    return tasks


def save(tasks: list[dict]) -> None:
    with open(HISTORY_FILE, "w") as f:
        json.dump(tasks, f, indent=2)


def add_task(
    task_id: str,
    prompt: str,
    image_urls: list[str],
    image_paths: list[str],
    video_url: str | None,
    audio_urls: list[str],
    audio_paths: list[str],
    settings: dict,
    username: str = "",
) -> None:
    tasks = load()  # load all, not filtered
    safe_image_urls = [u for u in image_urls if u.startswith("http")]
    safe_audio_urls = [u for u in audio_urls if u.startswith("http")]
    tasks.insert(
        0,
        {
            "id": task_id,
            "username": username,
            "prompt": prompt,
            "image_urls": safe_image_urls,
            "image_paths": image_paths,
            "video_url_ref": video_url if video_url and video_url.startswith("http") else None,
            "audio_urls_ref": safe_audio_urls,
            "audio_paths": audio_paths,
            "settings": settings,
            "status": "pending",
            "video_url": None,
            "created_at": datetime.now().isoformat(),
        },
    )
    save(tasks)


def update_task(
    task_id: str,
    status: str,
    video_url: str | None = None,
    tokens: int | None = None,
    cost: float | None = None,
    error_message: str | None = None,
) -> None:
    tasks = load()
    for t in tasks:
        if t["id"] == task_id:
            t["status"] = status
            if video_url:
                t["video_url"] = video_url
            if tokens is not None:
                t["tokens"] = tokens
            if cost is not None:
                t["cost"] = cost
            if error_message is not None:
                t["error_message"] = error_message
            break
    save(tasks)
