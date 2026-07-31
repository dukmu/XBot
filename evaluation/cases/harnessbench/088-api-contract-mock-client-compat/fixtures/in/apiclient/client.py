from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _get_json(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, str(exc))


def normalize_user(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "plan": row.get("plan", "free"),
    }


def list_users(base_url: str | None = None, version: str = "v2") -> list[dict]:
    base = (base_url or os.environ["MOCK_API_BASE"]).rstrip("/")
    users = []
    if version == "v1":
        page = 1
        while page:
            payload = _get_json(f"{base}/v1/users?page={page}")
            users.extend(normalize_user(row) for row in payload["items"])
            page = payload.get("next")
        return users

    cursor = ""
    while True:
        suffix = f"?cursor={cursor}" if cursor else ""
        payload = _get_json(f"{base}/v2/users{suffix}")
        users.extend(normalize_user(row) for row in payload["items"])
        cursor = payload.get("next")
        if not cursor:
            return users
