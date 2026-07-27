"""BytePlus CreateAssetGroup / CreateAsset (signed API)."""
from __future__ import annotations

import json
import os
from typing import Optional

import requests
from byteplussdkcore.signv4 import SignerV4

HOST = "ark.ap-southeast-1.byteplusapi.com"
REGION = "ap-southeast-1"
SERVICE = "ark"
VERSION = "2024-01-01"


def _credentials() -> tuple[str, str]:
    ak = os.getenv("SEEDREAM_ACCESS_KEY", "").strip()
    sk = os.getenv("SEEDREAM_SECRET_KEY", "").strip()
    if not ak or not sk:
        raise RuntimeError("Set SEEDREAM_ACCESS_KEY and SEEDREAM_SECRET_KEY.")
    return ak, sk


def _signed_post(*, action: str, body: dict) -> dict:
    ak, sk = _credentials()
    body_str = json.dumps(body, separators=(",", ":"))
    headers = {"Host": HOST, "Content-Type": "application/json"}
    query = {"Action": action, "Version": VERSION}

    SignerV4.sign(
        path="/",
        method="POST",
        headers=headers,
        body=body_str,
        post_params=None,
        query=query,
        ak=ak,
        sk=sk,
        region=REGION,
        service=SERVICE,
    )

    response = requests.post(
        f"https://{HOST}/",
        params=query,
        headers=headers,
        data=body_str.encode("utf-8"),
        timeout=120,
    )

    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text}

    if not response.ok:
        raise RuntimeError(
            f"{action} HTTP {response.status_code}: {json.dumps(payload, indent=2)}"
        )

    err = payload.get("ResponseMetadata", {}).get("Error")
    if err:
        raise RuntimeError(f"{action} API error: {json.dumps(err, indent=2)}")

    return payload


def extract_field(payload: dict, *keys: str) -> str:
    result = payload.get("Result") or payload.get("result") or {}
    if not isinstance(result, dict):
        result = {}

    for key in keys:
        val = result.get(key) or payload.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()

    for container in (result, payload):
        if not isinstance(container, dict):
            continue
        for key, val in container.items():
            if val is None:
                continue
            if any(k.lower() in key.lower() for k in keys) and str(val).strip():
                return str(val).strip()

    raise RuntimeError(f"Could not find {keys!r} in response: {json.dumps(payload)[:500]}")


def create_asset_group(*, name: str, description: str, group_type: str) -> dict:
    return _signed_post(
        action="CreateAssetGroup",
        body={
            "Name": name,
            "Description": description,
            "GroupType": group_type,
        },
    )


def create_asset(
    *,
    group_id: str,
    url: str,
    asset_type: str,
    moderation_strategy: str = "Skip",
    name: Optional[str] = None,
) -> dict:
    body: dict = {
        "GroupId": group_id,
        "URL": url,
        "AssetType": asset_type,
        "Moderation": {"Strategy": moderation_strategy},
    }
    if name:
        body["Name"] = name
    return _signed_post(action="CreateAsset", body=body)
