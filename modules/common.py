"""
Gedeelde helpers voor alle modules.
"""

import base64
import json
import os
import re
from functools import wraps
from pathlib import Path

from flask import jsonify, request
from smartschool import Smartschool, Credentials
from smartschool import (
    EnvCredentials,
    PlannedElements,
    MarkMessageUnread, MessageMoveToArchive, MessageMoveToTrash,
    AdjustMessageLabel, MessageLabel,
)


DEFAULT_MAIN_URL = os.environ.get("SMARTSCHOOL_MAIN_URL", "")
RESULTS_BASE = "/results/api/v1"

# Lokale opslag voor fallback uploads
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/tmp/smartschool-uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_TTL = 24 * 60 * 60  # 24 uur


# ============================================================
# CREDENTIALS
# ============================================================
def _decode_creds(header_value: str) -> dict:
    try:
        raw = base64.b64decode(header_value).decode("utf-8")
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"Ongeldige X-SS-Creds header: {e}")

    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    main_url = (data.get("main_url") or DEFAULT_MAIN_URL).strip()
    mfa = (data.get("mfa") or "").strip()

    if not all([username, password, main_url, mfa]):
        raise ValueError("username, password, main_url en mfa zijn verplicht")

    return {"username": username, "password": password,
            "main_url": main_url, "mfa": mfa}


class _Creds(Credentials):
    def __init__(self, u, p, m, mfa):
        self._u, self._p, self._m, self._mfa = u, p, m, mfa

    @property
    def username(self): return self._u
    @username.setter
    def username(self, v): self._u = v

    @property
    def password(self): return self._p
    @password.setter
    def password(self, v): self._p = v

    @property
    def main_url(self): return self._m
    @main_url.setter
    def main_url(self, v): self._m = v

    @property
    def mfa(self): return self._mfa
    @mfa.setter
    def mfa(self, v): self._mfa = v


def session_from_creds(creds: dict) -> Smartschool:
    s = Smartschool(_Creds(creds["username"], creds["password"],
                           creds["main_url"], creds["mfa"]))
    _ = s.platform_id
    return s


def with_session(fn):
    """Decorator: leest X-SS-Creds header, injecteert `_session` en `_creds` kwargs."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("X-SS-Creds")
        if not header:
            return jsonify({"error": "X-SS-Creds header ontbreekt"}), 401
        try:
            creds = _decode_creds(header)
            session = session_from_creds(creds)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Login mislukt: {type(e).__name__}: {e}"}), 401

        kwargs["_session"] = session
        kwargs["_creds"] = creds
        return fn(*args, **kwargs)
    return wrapper


def base_url(creds: dict) -> str:
    mu = creds["main_url"]
    return mu if mu.startswith("http") else f"https://{mu}"


# ============================================================
# HTML HELPERS
# ============================================================
def strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li)>", "\n", text, flags=re.IGNORECASE)
    for _ in range(3):
        before = text
        text = re.sub(r"<[^>]+>", "", text)
        for a, b in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                     ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " "),
                     ("&#8364;", "€"), ("&apos;", "'")]:
            text = text.replace(a, b)
        if text == before:
            break
    text = re.sub(r"searchDivHighlight", "", text, flags=re.IGNORECASE)
    return text.strip()


def clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.DOTALL)
    for _ in range(3):
        before = text
        text = re.sub(r"<[^>]+>", "", text)
        text = (text.replace("&lt;", "<").replace("&gt;", ">")
                    .replace("&quot;", '"').replace("&#39;", "'")
                    .replace("&amp;", "&").replace("&nbsp;", " ")
                    .replace("&apos;", "'").replace("&#8211;", "-")
                    .replace("&#8364;", "€"))
        if text == before:
            break
    text = re.sub(r"searchDivHighlight", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()
