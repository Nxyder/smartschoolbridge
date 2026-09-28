"""
Cijfers (grades) endpoints.
"""

from collections import defaultdict

from flask import Blueprint, jsonify, request

from modules.common import (
    with_session, base_url, clean_html, RESULTS_BASE,
)


bp = Blueprint("grades", __name__, url_prefix="/api/grades")


def _rget(session, base, path):
    r = session.request(
        "GET", path,
        headers={"Accept": "*/*", "Content-Type": "application/json",
                 "Referer": base + "/"},
    )
    return r.json()


def _fetch_all_evals(session, base):
    items, page = [], 1
    while True:
        batch = _rget(session, base,
                      f"{RESULTS_BASE}/evaluations/?pageNumber={page}&itemsOnPage=200")
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return items


def _fetch_eval_detail(session, base, identifier):
    try:
        return _rget(session, base, f"{RESULTS_BASE}/evaluations/{identifier}/")
    except Exception:
        return None


def _extract_comments(data):
    out = []
    if not isinstance(data, dict):
        return out
    for key in ["feedback", "feedbacks", "comment", "comments",
                "remark", "remarks", "note", "notes", "publicInfo"]:
        val = data.get(key)
        if not val:
            continue
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    txt = (item.get("text") or item.get("comment")
                           or item.get("value") or item.get("description") or "")
                    if txt:
                        out.append(clean_html(str(txt)))
                elif isinstance(item, str):
                    out.append(clean_html(item))
        elif isinstance(val, str):
            out.append(clean_html(val))
    return [c for c in dict.fromkeys(out) if c]


def _eval_summary(e, full=None):
    g = e.get("graphic", {})
    courses = e.get("courses", [])
    teacher = e.get("gradebookOwner", {}).get("name", {})
    data = {
        "id": e.get("identifier"),
        "name": e.get("name"),
        "date": (e.get("date") or "")[:10],
        "score": g.get("description", "?"),
        "percentage": g.get("value", "?"),
        "color": g.get("color", ""),
        "course": courses[0]["name"] if courses else "?",
        "course_id": courses[0]["id"] if courses else None,
        "component": e.get("component", {}).get("abbreviation", ""),
        "period": e.get("period", {}).get("name", ""),
        "does_count": e.get("doesCount", False),
        "teacher": teacher.get("startingWithLastName") or "?",
        "comments": _extract_comments(e),
    }
    if full:
        ct = (full.get("details") or {}).get("centralTendencies") or []
        data["central_tendencies"] = [
            {"type": t.get("type"),
             "score": t.get("graphic", {}).get("description"),
             "percentage": t.get("graphic", {}).get("value")}
            for t in ct
        ]
        extra = [c for c in _extract_comments(full) if c not in data["comments"]]
        data["comments"].extend(extra)
    return data


@bp.get("")
@with_session
def list_grades(_session=None, _creds=None):
    base = base_url(_creds)
    with_detail = request.args.get("detail", "0") == "1"

    courses = _rget(_session, base, f"{RESULTS_BASE}/courses/")
    evals = _fetch_all_evals(_session, base)
    course_names = {c["id"]: c["name"] for c in courses if c.get("name") != "Totaal"}

    per_course = defaultdict(list)
    for e in evals:
        for cv in e.get("courses", []):
            per_course[cv["id"]].append(e)

    result = {
        "total_evaluations": len(evals),
        "total_courses": len(course_names),
        "courses": [],
    }

    for cid, items in sorted(per_course.items(),
                             key=lambda x: course_names.get(x[0], "")):
        entry = {"course_id": cid,
                 "course_name": course_names.get(cid, f"Vak {cid}"),
                 "evaluations": []}
        for e in sorted(items, key=lambda x: x.get("date", ""), reverse=True):
            full = _fetch_eval_detail(_session, base, e["identifier"]) if with_detail else None
            entry["evaluations"].append(_eval_summary(e, full))
        result["courses"].append(entry)

    return jsonify(result)


@bp.get("/evaluation/<identifier>")
@with_session
def grade_detail(identifier, _session=None, _creds=None):
    base = base_url(_creds)
    full = _fetch_eval_detail(_session, base, identifier)
    if not full:
        return jsonify({"error": "niet gevonden"}), 404
    g = full.get("graphic", {})
    return jsonify({
        "id": full.get("identifier"),
        "name": full.get("name"),
        "date": (full.get("date") or "")[:10],
        "score": g.get("description"),
        "percentage": g.get("value"),
        "comments": _extract_comments(full),
        "central_tendencies": (full.get("details") or {}).get("centralTendencies", []),
        "raw": full,
    })
