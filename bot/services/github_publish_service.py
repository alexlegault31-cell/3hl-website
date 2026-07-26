"""Pushes the exported season data to the website's GitHub repo via
GitHub's REST API, so the static site rebuilds with fresh data. Reads
GITHUB_TOKEN and WEBSITE_REPO directly from the environment (not the
Settings object, since this is an optional integration -- if either is
unset, this silently does nothing, so a bot without the website feature
configured is completely unaffected).

Never raises -- any failure here is logged and swallowed, since a failed
website publish should never take down a Discord command that otherwise
succeeded.
"""
from __future__ import annotations

import base64
import json
import logging
import os

import aiohttp

log = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
DATA_FILE_PATH = "data.json"


async def publish_to_website(export_data: dict) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("WEBSITE_REPO")
    if not token or not repo or not export_data:
        return  # website integration not configured, or nothing to export -- nothing to do

    try:
        content_str = json.dumps(export_data, indent=2)
        content_b64 = base64.b64encode(content_str.encode()).decode()

        url = f"{GITHUB_API_BASE}/repos/{repo}/contents/{DATA_FILE_PATH}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

        async with aiohttp.ClientSession() as session:
            # GitHub's API requires the current file's SHA to update it
            # (not needed the very first time, when the file doesn't exist yet).
            sha = None
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    existing = await resp.json()
                    sha = existing.get("sha")

            payload = {"message": "Update league data", "content": content_b64, "branch": "main"}
            if sha:
                payload["sha"] = sha

            async with session.put(url, headers=headers, json=payload) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    log.warning("Failed to publish website data: HTTP %s - %s", resp.status, body[:300])
    except Exception:  # noqa: BLE001
        log.exception("Website publish failed, continuing without it")
