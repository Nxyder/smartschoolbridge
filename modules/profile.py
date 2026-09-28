"""
Profiel endpoints.
"""

import re

from flask import Blueprint, jsonify, request

from modules.common import with_session, base_url


bp = Blueprint("profile", __name__, url_prefix="/api/profile")


PROFILE_PATH = "/?module=Profile&file=personalia&function=personalia"


def _parse_inputs(html):
    fields = {}
    for tag in re.findall(r"<input\b[^>]*>", html, re.IGNORECASE):
        attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', tag))
        name = attrs.get("name")
        if not name:
            continue
        fields[name] = {
            "value": attrs.get("value", ""),
            "readonly": "readonly" in tag.lower(),
            "type": attrs.get("type", "text"),
        }
    return fields


def _get_profile(session):
    r = session.request("GET", PROFILE_PATH)
    return _parse_inputs(r.text)


@bp.get("")
@with_session
def read_profile(_session=None, _creds=None):
    fields = _get_profile(_session)
    return jsonify({
        "fields": {k: {"value": v["value"],
                       "readonly": v["readonly"],
                       "type": v["type"]}
                   for k, v in fields.items() if not k.startswith("_")}
    })


@bp.patch("")
@with_session
def update_profile(_session=None, _creds=None):
    data = request.get_json(force=True, silent=True) or {}
    changes = data.get("changes") or {}
    if not changes:
        return jsonify({"error": "changes verplicht"}), 400

    base = base_url(_creds)
    fields = _get_profile(_session)

    form = {"action": "store"}
    for name, info in fields.items():
        if name == "action":
            continue
        form[name] = info["value"]

    skipped = []
    for k, v in changes.items():
        if k in fields and fields[k]["readonly"]:
            skipped.append(k)
            continue
        form[k] = v

    r = _session.request(
        "POST", PROFILE_PATH, data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Origin": base, "Referer": base + PROFILE_PATH},
        allow_redirects=True,
    )

    new_fields = _get_profile(_session)
    applied = {k: v for k, v in changes.items()
               if new_fields.get(k, {}).get("value") == v}

    return jsonify({
        "status": r.status_code,
        "applied": applied,
        "skipped_readonly": skipped,
        "ok": len(applied) == len([k for k in changes if k not in skipped]),
    })
