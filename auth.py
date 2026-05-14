import json
import os

ENV_FILE = os.path.join(os.path.dirname(__file__), "env.json")


def _load() -> dict:
    """Load config from st.secrets (cloud) or env.json (local)."""
    try:
        import streamlit as st
        # Streamlit Cloud: secrets are available
        if hasattr(st, "secrets") and st.secrets:
            return {
                "BYTEPLUS_API_KEY": st.secrets.get("BYTEPLUS_API_KEY", ""),
                "GEMINI_API_KEY": st.secrets.get("GEMINI_API_KEY", ""),
                "OPENAI_API_KEY": st.secrets.get("OPENAI_API_KEY", ""),
                "USERS": [dict(u) for u in st.secrets.get("USERS", [])],
            }
    except Exception:
        pass
    with open(ENV_FILE) as f:
        return json.load(f)


def get_api_key() -> str:
    return _load().get("BYTEPLUS_API_KEY", "")


def get_gemini_key() -> str:
    return _load().get("GEMINI_API_KEY", "")


def get_openai_key() -> str:
    return _load().get("OPENAI_API_KEY", "")


def verify(username: str, password: str) -> bool:
    users = _load().get("USERS", [])
    return any(u["username"] == username and u["password"] == password for u in users)
