"""
Cijfers (grades) endpoints.

Endpoints:
  GET /api/grades                 → cijfers + comments (snel, uit bulk, gecached)
  GET /api/grades?detail=1        → + central_tendencies + extra comments (parallel)
  GET /api/grades?refresh=1       → cache negeren en opnieuw ophalen
  GET /api/grades/evaluation/<id> → detail van 1 evaluatie

Caching:
  - In-memory, per (creds-hash, with_detail) key
  - TTL: 5 minuten
  - Bewaart ALLEEN de JSON-output, geen sessies of credentials
  - ?refresh=1 forceert een verse fetch

Performance:
  - Comments komen uit de bulk-response (geen extra request)
  - Detail-fetch (alleen met detail=1) is parallel via ThreadPoolExecutor
  - Cache hit = <5ms response
"""

import hashlib
import hmac
import os
import threading
import time
from collections import defaultdict, OrderedDict
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

CACHE_TTL = 300          # 5 minuten
CACHE_MAX_ENTRIES = 200  # max aantal entries (LRU eviction)


# ============================================================
# CACHE — alleen JSON-output, geen sessies of credentials
# ============================================================
_CACHE = OrderedDict()      # key -> {"data": dict, "ts": float}
_CACHE_LOCK = threading.Lock()

# Server-side secret voor HMAC van de cache-key
# Zet deze in je environment: SESSION_SECRET=<random 32+ bytes hex>
_SERVER_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me").encode()


def _cache_key(creds, with_detail):
    """HMAC-based key — niemand kan de key reproduceren zonder SESSION_SECRET."""
    msg = "\x00".join([
        creds.get("username", ""),
        creds.get("password", ""),
        creds.get("main_url", ""),
        creds.get("mfa", ""),
        "detail=1" if with_detail else "detail=0",
    ]).encode()
    return hmac.new(_SERVER_SECRET, msg, hashlib.sha256).hexdigest()


def _cache_get(key):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        if time.time() - entry["ts"] > CACHE_TTL:
            _CACHE.pop(key, None)
            return None
        # LRU: verplaats naar einde
        _CACHE.move_to_end(key)
        return entry["data"]


def _cache_set(key, data):
    with _CACHE_LOCK:
        _CACHE[key] = {"data": data, "ts": time.time()}
        _CACHE.move_to_end(key)
        # Evict oudste als te groot
        while len(_CACHE) > CACHE_MAX_ENTRIES:
            _CACHE.popitem(last=False)


def _cache_invalidate(creds):
    """Verwijder alle cache-entries voor deze credentials."""
    with _CACHE_LOCK:
        to_delete = []
        for key in _CACHE:
            # We kunnen de key niet reverse-engineeren, dus we
            # berekenen de 2 varianten (detail=0 en detail=1)
            pass
        # Simpeler: verwijder beide varianten
        for wd in (False, True):
            _CACHE.pop(_cache_key(creds, wd), None)


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

    # Comments uit bulk — altijd, is gratis
    comments = _extract_comments(e)

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
        "comments": comments,
    }

    # Extra detail als we het opgehaald hebben
    if full:
        extra = [c for c in _extract_comments(full) if c not in comments]
        if extra:
            data["comments"] = comments + extra

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

    return data


# ============================================================
# CORE — fetch + build
# ============================================================
def _build_grades_payload(session, base, with_detail):
    """Haal alles op en bouw de response dict. Wordt gecached."""
    courses = _rget(session, base, f"{RESULTS_BASE}/courses/")
    evals = _fetch_all_evals(session, base)

    course_names = {
        c["id"]: c["name"]
        for c in courses
        if c.get("name") != "Totaal"
    }

    per_course = defaultdict(list)
    for e in evals:
        for cv in e.get("courses", []) or []:
            per_course[cv["id"]].append(e)

    # Detail parallel — alleen als gevraagd
    detail_map = {}
    if with_detail and evals:
        ids = [e.get("identifier") for e in evals if e.get("identifier")]
        if ids:
            with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
                futures = {
                    ex.submit(_fetch_eval_detail, session, base, eid): eid
                    for eid in ids
                }
                for fut in as_completed(futures):
                    eid = futures[fut]
                    try:
                        detail_map[eid] = fut.result()
                    except Exception:
                        detail_map[eid] = None

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

    return result


# ============================================================
# ROUTES
# ============================================================
@bp.get("")
@with_session
def list_grades(_session=None, _creds=None):
    """
    Query params:
      detail=0|1   (default 0)
      refresh=0|1  (default 0) — forceer verse fetch, negeer cache
    """
    base = base_url(_creds)
    with_detail = request.args.get("detail", "0") == "1"
    force_refresh = request.args.get("refresh", "0") == "1"

    key = _cache_key(_creds, with_detail)

    # Cache hit?
    if not force_refresh:
        cached = _cache_get(key)
        if cached is not None:
            # Voeg cache-metadata toe (zonder de data zelf te wijzigen)
            resp = dict(cached)
            resp["_cached"] = True
            return jsonify(resp)

    # Verse fetch
    payload = _build_grades_payload(_session, base, with_detail)
    _cache_set(key, payload)

    resp = dict(payload)
    resp["_cached"] = False
    return jsonify(resp)


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


@bp.post("/cache/clear")
@with_session
def clear_cache(_session=None, _creds=None):
    """Wis de cache voor deze credentials (beide varianten)."""
    _cache_invalidate(_creds)
    return jsonify({"ok": True})


@bp.get("/cache/stats")
def cache_stats():
    """Debug endpoint — aantal entries en TTL info."""
    with _CACHE_LOCK:
        return jsonify({
            "entries": len(_CACHE),
            "max": CACHE_MAX_ENTRIES,
            "ttl_seconds": CACHE_TTL,
        })
