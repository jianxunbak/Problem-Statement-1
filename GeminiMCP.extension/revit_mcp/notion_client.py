# -*- coding: utf-8 -*-
"""
notion_client.py — Thin Notion API wrapper for uploading design options.

Uses the already-bundled httpx.Client (same pattern as gemini_client.py).
Reads NOTION_API_KEY and NOTION_DATABASE_ID from environment (loaded by config.py).

NOTE: NOTION_URL in .env is a workspace page link and is NOT used here.
      The Notion API endpoint is hardcoded as https://api.notion.com/v1/pages.
"""
import os
import json

NOTION_API_BASE = "https://api.notion.com/v1"


def _log(msg):
    """Write a timestamped log line to fastmcp_server.log. Fails silently."""
    try:
        from revit_mcp.utils import get_log_path
        import time as _t
        ts = _t.strftime("%Y-%m-%d %H:%M:%S")
        line = "[{}] [NotionClient] {}\n".format(ts, msg)
        with open(get_log_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


class NotionClient:
    def __init__(self):
        self._api_key = None
        self._database_id = None
        self._session = None
        _log("NotionClient instance created")

    def _ensure_init(self):
        if self._session:
            return
        _log("_ensure_init: reading NOTION_API_KEY and NOTION_DATABASE_ID from environment")
        self._api_key = os.environ.get("NOTION_API_KEY", "").strip()
        self._database_id = os.environ.get("NOTION_DATABASE_ID", "").strip()

        if not self._api_key:
            _log("_ensure_init: ERROR — NOTION_API_KEY is missing from environment")
            raise RuntimeError("NOTION_API_KEY is not set in .env")
        if not self._database_id:
            _log("_ensure_init: ERROR — NOTION_DATABASE_ID is missing from environment")
            raise RuntimeError("NOTION_DATABASE_ID is not set in .env")

        # Mask key in logs: show first 8 chars only
        masked_key = self._api_key[:8] + "****"
        _log("_ensure_init: API key loaded ({}), database_id={}".format(masked_key, self._database_id))

        import httpx
        self._session = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=15.0, pool=5.0),
            headers={
                "Authorization": "Bearer {}".format(self._api_key),
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            follow_redirects=True,
            verify=False,
        )
        _log("_ensure_init: httpx.Client session created successfully")

    # Required schema: property-name → Notion property type object
    _REQUIRED_PROPERTIES = {
        "Description": {"rich_text": {}},
        "Typology":    {"select":    {}},
        "Created":     {"date":      {}},
        "Duration (s)":{"number":    {"format": "number"}},
        "Option ID":   {"rich_text": {}},
        "Type":        {"select":    {}},
    }

    def _ensure_database_schema(self):
        """Create any missing columns in the Notion database (runs once per session)."""
        if getattr(self, "_schema_verified", False):
            return
        _log("_ensure_database_schema: querying database schema...")
        try:
            resp = self._session.get("{}/databases/{}".format(NOTION_API_BASE, self._database_id))
            resp.raise_for_status()
            existing = set(resp.json().get("properties", {}).keys())
            missing = {k: v for k, v in self._REQUIRED_PROPERTIES.items() if k not in existing}
            if missing:
                _log("_ensure_database_schema: adding missing properties: {}".format(list(missing.keys())))
                patch_resp = self._session.patch(
                    "{}/databases/{}".format(NOTION_API_BASE, self._database_id),
                    json={"properties": missing},
                )
                patch_resp.raise_for_status()
                _log("_ensure_database_schema: schema updated OK")
            else:
                _log("_ensure_database_schema: all properties present")
            self._schema_verified = True
        except Exception as exc:
            _log("_ensure_database_schema: WARNING — {}".format(exc))
            # Don't block upload; proceed and let Notion surface any remaining error

    def upload_option(self, option):
        """
        Create a Notion page in the configured database for a saved option or revision.

        Required Notion database properties (must be set up manually once):
          - Name        (Title)
          - Description (Text / Rich Text)
          - Typology    (Select)
          - Created     (Date)
          - Duration (s)(Number)
          - Option ID   (Text / Rich Text)
          - Type        (Select)  — values: "Option" or "Revision"

        The full manifest JSON is stored as a JSON code block in the page body.
        Returns the Notion API response dict (includes "url" key with the page URL).
        """
        _log("upload_option: START — id='{}', name='{}'".format(
            option.get("id", "?"), option.get("name", "?")[:60]))

        self._ensure_init()
        self._ensure_database_schema()

        manifest_json = json.dumps(option.get("manifest", {}), indent=2)
        manifest_chars = len(manifest_json)
        # Notion rich_text blocks have a 2000-char limit per element
        manifest_preview = manifest_json[:1999]

        is_revision = "-Rev" in option.get("id", "")
        page_type = "Revision" if is_revision else "Option"

        # Safely clip strings to Notion limits
        name = option.get("name", "Untitled")[:2000]
        description = option.get("description", "")[:2000]
        opt_id = option.get("id", "")[:200]
        typology = option.get("typology", "default")[:100]
        created_raw = option.get("created_at", "")
        # Notion date expects "YYYY-MM-DD" format
        created_date = created_raw[:10] if created_raw else None
        duration = option.get("duration_s") or 0

        _log("upload_option: payload — type={}, typology={}, created={}, duration={}s, manifest={} chars".format(
            page_type, typology, created_date, duration, manifest_chars))

        payload = {
            "parent": {"database_id": self._database_id},
            "properties": {
                "Name": {
                    "title": [{"text": {"content": name}}]
                },
                "Description": {
                    "rich_text": [{"text": {"content": description}}]
                },
                "Typology": {
                    "select": {"name": typology}
                },
                "Duration (s)": {
                    "number": duration
                },
                "Option ID": {
                    "rich_text": [{"text": {"content": opt_id}}]
                },
                "Type": {
                    "select": {"name": page_type}
                },
            },
            "children": [
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [{"text": {"content": "Manifest JSON"}}]
                    }
                },
                {
                    "object": "block",
                    "type": "code",
                    "code": {
                        "rich_text": [{"text": {"content": manifest_preview}}],
                        "language": "json",
                    },
                },
            ],
        }

        # Only add Created date if we have a valid date string
        if created_date:
            payload["properties"]["Created"] = {"date": {"start": created_date}}
            _log("upload_option: Created date property set to {}".format(created_date))
        else:
            _log("upload_option: no created_at — skipping Created property")

        url = "{}/pages".format(NOTION_API_BASE)
        _log("upload_option: POST {} ...".format(url))

        try:
            resp = self._session.post(url, json=payload)
            status_code = resp.status_code
            _log("upload_option: HTTP {} received".format(status_code))

            if status_code != 200:
                # Log response body to help diagnose 400/401/403 errors
                try:
                    err_body = resp.json()
                    _log("upload_option: ERROR response body — {}".format(json.dumps(err_body)[:500]))
                except Exception:
                    _log("upload_option: ERROR response body (raw) — {}".format(resp.text[:500]))

            resp.raise_for_status()
            result = resp.json()
            page_id = result.get("id", "?")
            page_url = result.get("url", "")
            _log("upload_option: SUCCESS — Notion page_id={}, url={}".format(page_id, page_url))
            return result

        except Exception as e:
            import traceback
            _log("upload_option: EXCEPTION — {}\n{}".format(e, traceback.format_exc()))
            raise

    def close(self):
        if self._session:
            _log("close: closing httpx session")
            self._session.close()
            self._session = None
            _log("close: session closed")


# ── Module-level singleton ────────────────────────────────────────────────────

_notion_client = None


def get_notion_client():
    global _notion_client
    if _notion_client is None:
        _log("get_notion_client: creating singleton NotionClient")
        _notion_client = NotionClient()
    return _notion_client
