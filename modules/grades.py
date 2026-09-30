"""
Cijfers (grades) endpoints.

Endpoints:
  GET /api/grades                 → alle cijfers (basis, snel)
  GET /api/grades?detail=1        → met detail-fetch per evaluatie (parallel)
  GET /api/grades/evaluation/<id> → detail van 1 evaluatie

Performance:
  - Standaard GEEN detail-fetch (dat is de N+1 die 8s kostte)
  - Met detail=1: parallel via ThreadPoolExecutor (max 10 workers)
  - Paginering stopt zodra een batch kleiner is dan de page size
"""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, jsonify, request

from modules.common import (
    with_session, base_url, clean_html, RESULTS_BASE,
)


bp = Blueprint("grades", __name__, url_prefix="/api/grades")


# ============================================================
# CONFIG
# ============================================================
PAGE_SIZE = 200
DETAIL_WORKERS = 10
REQUEST_TIMEOUT = 15


# ============================================================
# LOW-LEVEL FETCH
# ============================================================
def _rget(session, base, path):
    """GET met retry — Smartschool antwoordt soms met een 502."""
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Referer": base + "/",
    }
    last_exc = None
    for attempt in range(2):
        try:
            r = session.request("GET", path, headers=headers,
                                timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last_exc = Exception(f"status {r.status_code}")
        except Exception as e:
            last_exc = e
    raise last_exc or Exception("onbekende fout")


def _fetch_all_evals(session, base):
    """Haal alle evaluaties op via paginering."""
    items = []
    page = 1
    while True:
        batch = _rget(
            session, base,
            f"{RESULTS_BASE}/evaluations/?pageNumber={page}&itemsOnPage={PAGE_SIZE}"
        )
        if not batch:
            break
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
        # Veiligheid: stop na 50 pagina's (= 10.000 evaluaties)
        if page > 50:
            break
    return items


def _fetch_eval_detail(session, base, identifier):
    try:
        return _rget(session, base, f"{RESULTS_BASE}/evaluations/{identifier}/")
    except Exception:
        return None


# ============================================================
# PARSING
# ============================================================
def _extract_comments(data):
    out = []
    if not isinstance(data, dict):
        return out
    for key in ("feedback", "feedbacks", "comment", "comments",
                "remark", "remarks", "note", "notes", "publicInfo"):
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
    g = e.get("graphic", {}) or {}
    courses = e.get("courses", []) or []
    teacher = (e.get("gradebookOwner") or {}).get("name", {}) or {}

    data = {
        "id": e.get("identifier"),
        "name": e.get("name"),
        "date": (e.get("date") or "")[:10],
        "score": g.get("description", "?"),
        "percentage": g.get("value", "?"),
        "color": g.get("color", ""),
        "course": courses[0]["name"] if courses else "?",
        "course_id": courses[0]["id"] if courses else None,
        "component": (e.get("component") or {}).get("abbreviation", ""),
        "period": (e.get("period") or {}).get("name", ""),
        "does_count": e.get("doesCount", False),
        "teacher": teacher.get("startingWithLastName") or "?",
    }

    # Comments alleen als detail beschikbaar is — anders leeg
    if full:
        data["comments"] = _extract_comments(full)
        ct = (full.get("details") or {}).get("centralTendencies") or []
        if ct:
            data["central_tendencies"] = [
                {
                    "type": t.get("type"),
                    "score": (t.get("graphic") or {}).get("description"),
                    "percentage": (t.get("graphic") or {}).get("value"),
                }
                for t in ct
            ]
    else:
        data["comments"] = []

    return data


# ============================================================
# ROUTES
# ============================================================
@bp.get("")
@with_session
def list_grades(_session=None, _creds=None):
    """
    Query params:
      detail=0|1   (default 0) — haal per evaluatie extra detail op (traag)
    """
    base = base_url(_creds)
    with_detail = request.args.get("detail", "0") == "1"

    # 1) Courses + evaluaties parallel ophalen
    courses = _rget(_session, base, f"{RESULTS_BASE}/courses/")
    evals = _fetch_all_evals(_session, base)

    course_names = {
        c["id"]: c["name"]
        for c in courses
        if c.get("name") != "Totaal"
    }

    # 2) Groepeer per course
    per_course = defaultdict(list)
    for e in evals:
        for cv in e.get("courses", []) or []:
            per_course[cv["id"]].append(e)

    # 3) Detail-fetch parallel — alleen als gevraagd
    detail_map = {}
    if with_detail and evals:
        ids = [e.get("identifier") for e in evals if e.get("identifier")]
        if ids:
            with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
                futures = {
                    ex.submit(_fetch_eval_detail, _session, base, eid): eid
                    for eid in ids
                }
                for fut in as_completed(futures):
                    eid = futures[fut]
                    try:
                        detail_map[eid] = fut.result()
                    except Exception:
                        detail_map[eid] = None

    # 4) Bouw response
    result = {
        "total_evaluations": len(evals),
        "total_courses": len(course_names),
        "with_detail": with_detail,
        "courses": [],
    }

    for cid, items in sorted(per_course.items(),
                             key=lambda x: course_names.get(x[0], "")):
        entry = {
            "course_id": cid,
            "course_name": course_names.get(cid, f"Vak {cid}"),
            "evaluations": [],
        }
        for e in sorted(items, key=lambda x: x.get("date", ""), reverse=True):
            full = detail_map.get(e.get("identifier")) if with_detail else None
            entry["evaluations"].append(_eval_summary(e, full))
        result["courses"].append(entry)

    return jsonify(result)


@bp.get("/evaluation/<identifier>")
@with_session
def grade_detail(identifier, _session=None, _creds=None):
    """Detail van 1 evaluatie — voor lazy loading in de UI."""
    base = base_url(_creds)
    full = _fetch_eval_detail(_session, base, identifier)
    if not full:
        return jsonify({"error": "niet gevonden"}), 404

    g = full.get("graphic", {}) or {}
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
