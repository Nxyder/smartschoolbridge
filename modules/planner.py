"""
Planner endpoints — webversie.

API structuur:
  GET  /api/planner                     → compact: uren + items
  GET  /api/planner/week?offset=0       → deze/vorige/volgende week
  GET  /api/planner/item/<id>?type=...  → detail van 1 item
  POST /api/planner/<pid>/<eid>/<action>?type=...  → resolve | unresolve | trash
  POST /api/planner/todo                → nieuwe to-do
  GET  /api/planner/debug-userid        → diagnose user_id probleem

Query params (GET /api/planner):
  from=YYYY-MM-DD        startdatum (default: vandaag - 14d)
  to=YYYY-MM-DD          einddatum (default: vandaag + 30d)
  types=lessons|tasks|all  filter (default: all)
  limit=N                max items (default: 500, hard cap: 2000)
  compact=1              1 = enkel essentiële velden (default), 0 = volledig
  hours=1                1 = uren meesturen (default: 1)
  placeholders=0         1 = placeholders meesturen (default: 0 = verbergen)

Belangrijke fix t.o.v. eerdere versies:
  - URL-encoding van +02:00 in from/to ISO strings (quote)
    Zonder encoding wordt + een spatie → Smartschool negeert de range
  - Unicode unescape van Homepage HTML (\\u0022 → ")
  - Placeholders standaard verborgen
"""

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import Blueprint, jsonify, request

from modules.common import with_session, strip_html, base_url


bp = Blueprint("planner", __name__, url_prefix="/api/planner")


# ============================================================
# CACHE — in-memory per user, korte TTL
# ============================================================
_CACHE = {}
_CACHE_TTL = 30  # seconden


def _cache_get(uid, range_key):
    entry = _CACHE.get(uid)
    if not entry:
        return None
    if entry["range_key"] != range_key:
        return None
    if time.time() - entry["timestamp"] > _CACHE_TTL:
        _CACHE.pop(uid, None)
        return None
    return entry["items"]


def _cache_set(uid, range_key, items):
    _CACHE[uid] = {
        "timestamp": time.time(),
        "range_key": range_key,
        "items": items,
    }


def _cache_invalidate(uid):
    _CACHE.pop(uid, None)


# ============================================================
# HTML UNESCAPE — Smartschool stuurt \uXXXX escapes
# ============================================================
def _unescape_html(html):
    """
    Smartschool stuurt JSON-encoded HTML met \\uXXXX escapes.
    Zet die om naar echte karakters voor regex matching.
    """
    if not html:
        return html
    return re.sub(
        r'\\u([0-9a-fA-F]{4})',
        lambda m: chr(int(m.group(1), 16)),
        html,
    )


# ============================================================
# USER ID
# ============================================================
def _find_user_id(session, base):
    """
    Zoek user_id via Homepage HTML.
    Werkt met zowel escaped als unescaped HTML.
    """
    try:
        r = session.request("GET", "/?module=Homepage",
                            headers={"Referer": base + "/"})
        html = r.text

        if r.status_code in (301, 302, 303, 307, 308):
            print("[planner] Homepage redirect — sessie verlopen?",
                  file=sys.stderr)
            return None

        if not html:
            print("[planner] Homepage HTML is leeg", file=sys.stderr)
            return None

        # Probeer zowel unescaped als raw
        html_clean = _unescape_html(html)

        patterns = [
            r'"authenticatedUser"\s*:\s*\{[^}]*"id"\s*:\s*"(\d+)_(\d+)_',
            r'"userIdentifier"\s*:\s*"(\d+)_(\d+)_',
            r'"userId"\s*:\s*"(\d+)_(\d+)_(\d+)"',
        ]

        for source in (html_clean, html):
            for pat in patterns:
                m = re.search(pat, source)
                if m:
                    return int(m.group(2))

        # Fallback met platform_id
        platform = getattr(session, "platform_id", None)
        if platform:
            for source in (html_clean, html):
                m = re.search(rf'{platform}_(\d+)_0', source)
                if m:
                    return int(m.group(1))

        print(f"[planner] geen user_id in Homepage HTML "
              f"(len={len(html)})", file=sys.stderr)
        return None

    except Exception as e:
        print(f"[planner] _find_user_id fout: {type(e).__name__}: {e}",
              file=sys.stderr)
        return None


def _get_uid(session, base):
    """Bouw volledige uid: 'platform_userid_0'."""
    platform = getattr(session, "platform_id", None)
    user_num = _find_user_id(session, base)
    if not user_num or not platform:
        return None
    return f"{platform}_{user_num}_0"


# ============================================================
# HOURS
# ============================================================
def _get_hours(session):
    """Haal lesuren op (7 slots: 08:25 - 15:30)."""
    try:
        from smartschool import SmartschoolHours
        return [
            {"id": h.hour_id, "s": h.start, "e": h.end, "t": h.title}
            for h in SmartschoolHours(session)
        ]
    except Exception as e:
        print(f"[planner] hours fout: {type(e).__name__}: {e}",
              file=sys.stderr)
        return []


# ============================================================
# PLANNER ITEMS — via JSON API
# ⭐ URL-ENCODING IS CRUCIAAL VOOR from/to
# ============================================================
def _fetch_planned_elements(session, base, uid, from_iso, to_iso):
    """
    Haal alle planner items op (lessen + taken + activiteiten).

    ⭐ CRUCIAAL: URL-encode de + in +02:00.
    Zonder encoding wordt + een spatie → Smartschool negeert de range
    en geeft altijd dezelfde (huidige) week terug.
    """
    from_enc = quote(from_iso, safe='')
    to_enc = quote(to_iso, safe='')

    path = (
        f"/planner/api/v1/planned-elements/user/{uid}"
        f"?from={from_enc}&to={to_enc}"
        f"&includes=icon,courses,locations,upload-folders"
    )

    try:
        r = session.request("GET", path, headers={
            "Accept": "*/*",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": base,
            "Referer": base + "/",
        })
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[planner] fetch fout: {type(e).__name__}: {e}",
              file=sys.stderr)
        return []


# ============================================================
# COMPACT FORMAT
# ============================================================
def _to_ms(iso):
    """ISO string → unix ms (compact)."""
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(
            iso.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def _compact_item(it):
    """Volledig item (~2KB) → compact item (~250 bytes)."""
    period = it.get("period") or {}
    courses = it.get("courses") or []
    locations = it.get("locations") or []
    organisers = (it.get("organisers") or {}).get("users") or []
    atype = it.get("assignmentType") or {}

    teacher = ""
    if organisers:
        t = organisers[0].get("name") or {}
        teacher = (t.get("startingWithLastName")
                   or t.get("startingWithFirstName")
                   or "")

    course = courses[0] if courses else {}
    course_name = course.get("name", "")

    location = ""
    if locations:
        location = (locations[0].get("title")
                    or locations[0].get("number")
                    or "")

    return {
        "i": str(it.get("id", "") or ""),
        "n": (it.get("name", "") or "")[:200],
        "t": it.get("plannedElementType", ""),
        "df": _to_ms(period.get("dateTimeFrom")),
        "dt": _to_ms(period.get("dateTimeTo")),
        "wd": 1 if period.get("wholeDay") else 0,
        "dl": 1 if period.get("deadline") else 0,
        "c": course_name,
        "l": location,
        "te": teacher,
        "ab": atype.get("abbreviation", ""),
        "col": it.get("color", ""),
        "st": it.get("resolvedStatus", "") or "",
    }


# ============================================================
# RANGE PARSING
# ============================================================
def _parse_range():
    """Parse from/to uit query params. Default: 14d terug, 30d vooruit."""
    tz = timezone(timedelta(hours=2))
    now = datetime.now(tz)

    from_s = request.args.get("from")
    to_s = request.args.get("to")

    if from_s:
        try:
            from_dt = datetime.fromisoformat(from_s).replace(tzinfo=tz)
        except Exception:
            from_dt = (now - timedelta(days=14)).replace(
                hour=0, minute=0, second=0, microsecond=0)
    else:
        from_dt = (now - timedelta(days=14)).replace(
            hour=0, minute=0, second=0, microsecond=0)

    if to_s:
        try:
            to_dt = datetime.fromisoformat(to_s).replace(tzinfo=tz)
        except Exception:
            to_dt = (now + timedelta(days=30)).replace(
                hour=23, minute=59, second=59, microsecond=0)
    else:
        to_dt = (now + timedelta(days=30)).replace(
            hour=23, minute=59, second=59, microsecond=0)

    if (to_dt - from_dt).days > 366:
        to_dt = from_dt + timedelta(days=366)

    return from_dt, to_dt


# ============================================================
# FILTERS
# ============================================================
def _filter_placeholders(raw, include=False):
    """Verberg planned-placeholders (lege tijdsloten) standaard."""
    if include:
        return raw
    return [it for it in raw
            if it.get("plannedElementType") != "planned-placeholders"]


# ============================================================
# ROUTES
# ============================================================
@bp.get("")
@with_session
def planner_list(_session=None, _creds=None):
    """
    Planner items, compact formaat.

    Query params:
      from=YYYY-MM-DD      (default: vandaag - 14d)
      to=YYYY-MM-DD        (default: vandaag + 30d)
      types=lessons|tasks|all  (default: all)
      limit=N              (default: 500, max: 2000)
      compact=0|1          (default: 1)
      hours=0|1            (default: 1)
      placeholders=0|1     (default: 0 = verbergen)
    """
    base = base_url(_creds)
    uid = _get_uid(_session, base)
    if not uid:
        return jsonify({
            "error": "kon user_id niet vinden",
            "hint": "check /api/planner/debug-userid voor diagnose",
        }), 500

    from_dt, to_dt = _parse_range()
    types_filter = request.args.get("types", "all")
    try:
        limit = min(int(request.args.get("limit", "500")), 2000)
    except ValueError:
        limit = 500
    compact = request.args.get("compact", "1") == "1"
    include_hours = request.args.get("hours", "1") == "1"
    include_placeholders = request.args.get("placeholders", "0") == "1"

    from_iso = from_dt.strftime("%Y-%m-%dT%H:%M:%S+02:00")
    to_iso = to_dt.strftime("%Y-%m-%dT%H:%M:%S+02:00")

    # Cache raw (pre-filter)
    raw_key = f"{from_iso}|{to_iso}|raw"
    raw = _cache_get(uid, raw_key)
    if raw is None:
        raw = _fetch_planned_elements(_session, base, uid,
                                      from_iso, to_iso)
        _cache_set(uid, raw_key, raw)

    # Filter placeholders
    raw = _filter_placeholders(raw, include_placeholders)

    # Filter op type
    if types_filter == "lessons":
        raw = [it for it in raw
               if it.get("plannedElementType") == "planned-lessons"]
    elif types_filter == "tasks":
        raw = [it for it in raw
               if it.get("plannedElementType") != "planned-lessons"]

    raw.sort(key=lambda x: (x.get("period") or {}).get("dateTimeFrom") or "")

    total_before_limit = len(raw)
    raw = raw[:limit]

    if compact:
        items = [_compact_item(it) for it in raw]
    else:
        items = raw

    result = {
        "meta": {
            "count": len(items),
            "total": total_before_limit,
            "from": from_iso,
            "to": to_iso,
            "compact": compact,
            "user_id": uid,
        },
        "items": items,
    }

    if include_hours:
        result["hours"] = _get_hours(_session)

    return jsonify(result)


@bp.get("/week")
@with_session
def planner_week(_session=None, _creds=None):
    """
    Alleen deze week (maandag t/m zondag). Extra compact.

    Query params:
      offset=0            0 = deze week, -1 = vorige, +1 = volgende
      placeholders=0|1    (default: 0 = verbergen)
    """
    base = base_url(_creds)
    uid = _get_uid(_session, base)
    if not uid:
        return jsonify({"error": "kon user_id niet vinden"}), 500

    try:
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        offset = 0

    include_placeholders = request.args.get("placeholders", "0") == "1"

    tz = timezone(timedelta(hours=2))
    now = datetime.now(tz)
    day = now.weekday()  # 0 = maandag
    monday = (now - timedelta(days=day) + timedelta(weeks=offset)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    sunday = (monday + timedelta(days=6)).replace(
        hour=23, minute=59, second=59, microsecond=0)

    from_iso = monday.strftime("%Y-%m-%dT%H:%M:%S+02:00")
    to_iso = sunday.strftime("%Y-%m-%dT%H:%M:%S+02:00")

    raw_key = f"{from_iso}|{to_iso}|raw"
    raw = _cache_get(uid, raw_key)
    if raw is None:
        raw = _fetch_planned_elements(_session, base, uid,
                                      from_iso, to_iso)
        _cache_set(uid, raw_key, raw)

    # Filter placeholders
    raw = _filter_placeholders(raw, include_placeholders)

    raw.sort(key=lambda x: (x.get("period") or {}).get("dateTimeFrom") or "")
    items = [_compact_item(it) for it in raw]

    return jsonify({
        "week_start": monday.strftime("%Y-%m-%d"),
        "week_end": sunday.strftime("%Y-%m-%d"),
        "hours": _get_hours(_session),
        "items": items,
        "meta": {
            "user_id": uid,
            "count": len(items),
        },
    })


@bp.get("/item/<element_id>")
@with_session
def planner_item_detail(element_id, _session=None, _creds=None):
    """
    Detail van 1 item. Lazy loaded.

    Query params:
      type=planned-to-dos|planned-assignments|planned-lessons
    """
    element_type = request.args.get("type", "")
    platform_id = _session.platform_id

    if element_type == "planned-to-dos":
        prefix = "planned-to-dos"
    elif element_type == "planned-assignments":
        prefix = "planned-assignments"
    else:
        return jsonify({
            "error": f"detail niet beschikbaar voor type '{element_type}'"
        }), 400

    path = f"/planner/api/v1/{prefix}/{platform_id}/{element_id}"
    try:
        data = _session.json(path)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    return jsonify({
        "id": element_id,
        "type": element_type,
        "public_info": strip_html(data.get("publicInfo") or ""),
        "attachments": [
            {"name": a.get("name", ""),
             "file_id": a.get("fileID") or a.get("id")}
            for a in (data.get("attachments") or [])
        ],
        "weblinks": [
            l.get("url", l) if isinstance(l, dict) else l
            for l in (data.get("weblinks") or [])
        ],
        "raw": data,
    })


@bp.post("/<platform_id>/<element_id>/<action>")
@with_session
def planner_item_action(platform_id, element_id, action,
                        _session=None, _creds=None):
    """
    Actie op een planner item.
    Actions: resolve | unresolve | trash

    Query params:
      type=planned-to-dos|planned-assignments (verplicht)
    """
    if action not in ("resolve", "unresolve", "trash"):
        return jsonify({"error": "ongeldige actie"}), 400

    element_type = request.args.get("type", "")
    if not element_type:
        return jsonify({"error": "type query param verplicht"}), 400

    if element_type == "planned-to-dos":
        prefix = "planned-to-dos"
    elif element_type == "planned-assignments":
        prefix = "planned-assignments"
    else:
        return jsonify({
            "error": f"actie niet ondersteund voor {element_type}"
        }), 400

    path = (f"/planner/api/v1/{prefix}/{platform_id}/"
            f"{element_id}/{action}")
    try:
        r = _session.request("POST", path)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    ok = r.status_code in (200, 204)

    base = base_url(_creds)
    uid = _get_uid(_session, base)
    if uid:
        _cache_invalidate(uid)

    if not ok:
        return jsonify({
            "error": f"Smartschool weigerde actie '{action}'",
            "status": r.status_code,
            "ok": False,
        }), 502

    return jsonify({"ok": True, "action": action})


@bp.post("/todo")
@with_session
def planner_create_todo(_session=None, _creds=None):
    """
    Maak een nieuwe to-do aan.

    Body:
      name            (verplicht)
      description?    tekst
      color?          bv "tangerine-200"
      icon?           bv "icon_fill_flag"
      date_from?      YYYY-MM-DD
      date_to?        YYYY-MM-DD
      whole_day?      bool (default true)
    """
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name verplicht"}), 400

    description = data.get("description", "")
    color = data.get("color", "tangerine-200")
    icon = data.get("icon", "icon_fill_flag")

    tz = timezone(timedelta(hours=2))

    dt_from_s = data.get("date_from")
    dt_to_s = data.get("date_to")

    if dt_from_s:
        try:
            dt_from = datetime.fromisoformat(dt_from_s).replace(tzinfo=tz)
        except Exception:
            dt_from = datetime.now(tz).replace(
                hour=0, minute=0, second=0, microsecond=0)
    else:
        dt_from = datetime.now(tz).replace(
            hour=0, minute=0, second=0, microsecond=0)

    if dt_to_s:
        try:
            dt_to = datetime.fromisoformat(dt_to_s).replace(
                tzinfo=tz, hour=23, minute=59, second=59)
        except Exception:
            dt_to = dt_from.replace(hour=23, minute=59, second=59)
    else:
        dt_to = dt_from.replace(hour=23, minute=59, second=59)

    body = {
        "name": name,
        "publicInfo": f"<p>{description}</p>" if description else "",
        "color": color,
        "icon": icon,
        "period": {
            "dateTimeFrom": dt_from.strftime("%Y-%m-%dT%H:%M:%S+02:00"),
            "dateTimeTo": dt_to.strftime("%Y-%m-%dT%H:%M:%S+02:00"),
            "wholeDay": data.get("whole_day", True),
        },
    }

    try:
        r = _session.request("POST", "/planner/api/v1/planned-to-dos/",
                             json=body)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    base = base_url(_creds)
    uid = _get_uid(_session, base)
    if uid:
        _cache_invalidate(uid)

    return jsonify({
        "ok": r.status_code in (200, 201),
        "status": r.status_code,
        "response": r.text[:500],
    })


# ============================================================
# DEBUG
# ============================================================
@bp.get("/debug-userid")
@with_session
def debug_userid(_session=None, _creds=None):
    """Diagnose: toon Homepage HTML + user_id patronen."""
    base = base_url(_creds)
    platform = getattr(_session, "platform_id", None)

    result = {
        "platform_id": platform,
        "base_url": base,
    }

    try:
        r = _session.request("GET", "/?module=Homepage",
                             headers={"Referer": base + "/"})
        html = r.text
        result["status_code"] = r.status_code
        result["html_length"] = len(html)
        result["final_url"] = getattr(r, "url", None)

        html_clean = _unescape_html(html)

        hits = {}
        patterns = {
            "authenticatedUser_id":
                r'"authenticatedUser"\s*:\s*\{[^}]*"id"\s*:\s*"([^"]+)"',
            "userIdentifier": r'"userIdentifier"\s*:\s*"([^"]+)"',
            "userId_full": r'"userId"\s*:\s*"(\d+_\d+_\d+)"',
            "userId_simple": r'"userId"\s*:\s*"?(\d+)"?',
            "platformId": r'"platformId"\s*:\s*"?(\d+)"?',
        }

        for name, pat in patterns.items():
            m_clean = re.search(pat, html_clean, re.IGNORECASE | re.DOTALL)
            m_raw = re.search(pat, html, re.IGNORECASE | re.DOTALL)
            hits[name] = {
                "unescaped": m_clean.group(1) if m_clean else None,
                "raw": m_raw.group(1) if m_raw else None,
            }

        result["hits"] = hits
        result["_find_user_id_result"] = _find_user_id(_session, base)
        result["_get_uid_result"] = _get_uid(_session, base)

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return jsonify(result)


@bp.get("/debug-fetch")
@with_session
def debug_fetch(_session=None, _creds=None):
    """
    Test of from/to werkt bij Smartschool.
    Vergelijkt raw vs URL-encoded variant.
    """
    base = base_url(_creds)
    uid = _get_uid(_session, base)
    if not uid:
        return jsonify({"error": "geen uid"}), 500

    tz = timezone(timedelta(hours=2))
    now = datetime.now(tz)

    results = {}
    for label, offset_days in [("verleden", -30), ("vandaag", 0),
                                ("toekomst", 30)]:
        from_dt = (now + timedelta(days=offset_days)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        to_dt = (now + timedelta(days=offset_days + 7)).replace(
            hour=23, minute=59, second=59)
        from_iso = from_dt.strftime("%Y-%m-%dT%H:%M:%S+02:00")
        to_iso = to_dt.strftime("%Y-%m-%dT%H:%M:%S+02:00")

        # Variant A: raw (met +)
        path_a = (
            f"/planner/api/v1/planned-elements/user/{uid}"
            f"?from={from_iso}&to={to_iso}"
            f"&includes=icon,courses,locations,upload-folders"
        )
        # Variant B: encoded (met %2B)
        from_enc = quote(from_iso, safe='')
        to_enc = quote(to_iso, safe='')
        path_b = (
            f"/planner/api/v1/planned-elements/user/{uid}"
            f"?from={from_enc}&to={to_enc}"
            f"&includes=icon,courses,locations,upload-folders"
        )

        variants = {}
        for variant_label, path in [("raw", path_a),
                                     ("encoded", path_b)]:
            try:
                r = _session.request("GET", path, headers={
                    "Accept": "*/*",
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": base,
                    "Referer": base + "/",
                })
                data = r.json() if r.status_code == 200 else []
                datums = {}
                if isinstance(data, list):
                    for it in data:
                        p = (it.get("period") or {}).get(
                            "dateTimeFrom") or ""
                        d = p[:10]
                        if d:
                            datums[d] = datums.get(d, 0) + 1
                variants[variant_label] = {
                    "status": r.status_code,
                    "count": len(data) if isinstance(data, list) else 0,
                    "datums": datums,
                }
            except Exception as e:
                variants[variant_label] = {"error": str(e)}

        results[label] = variants

    return jsonify(results)
