"""
Planner endpoints.

API structuur:
  GET  /api/planner                 → compact: uren + items, standaard range = 14d terug / 30d vooruit
  GET  /api/planner/week            → enkel deze week (ma-zo), compact, gecached
  GET  /api/planner/item/<id>       → detail van 1 item (lazy loaded)
  POST /api/planner/<pid>/<eid>/<action>  → resolve | unresolve | trash
  POST /api/planner/todo            → nieuwe to-do

Query params (alle optioneel):
  from=YYYY-MM-DD        startdatum (default: vandaag - 14d)
  to=YYYY-MM-DD          einddatum (default: vandaag + 30d)
  types=lessons|tasks|all  filter (default: all)
  limit=N                max items (default: 500, hard cap: 2000)
  compact=1              1 = enkel de essentiële velden (default), 0 = volledig

Bandwidth optimalisaties:
  - Geen includes=icon,courses,locations,upload-folders tenzij nodig
  - Enkel velden die frontend gebruikt
  - Standaard korte range (14d terug, 30d vooruit)
  - Server-side filter op type
  - 'compact' formaat scheelt 60-70% bytes
"""

import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from modules.common import with_session, strip_html, base_url


bp = Blueprint("planner", __name__, url_prefix="/api/planner")


# ============================================================
# CACHE — in-memory per user, korte TTL
# ============================================================
# Key: user_id → {timestamp, range_key, items}
_CACHE = {}
_CACHE_TTL = 30   # seconden — kort genoeg voor actualiteit, lang genoeg voor pagina-loads


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
# USER ID
# ============================================================
def _get_user_id(session, base):
    """Haal het user_id op via Homepage HTML (SMSC.authenticatedUser)."""
    import re
    try:
        r = session.request("GET", "/?module=Homepage",
                            headers={"Referer": base + "/"})
        for pat in [
            r'"authenticatedUser"\s*:\s*\{[^}]*"id"\s*:\s*"(\d+)_(\d+)_',
            r'"userIdentifier"\s*:\s*"(\d+)_(\d+)_',
        ]:
            m = re.search(pat, r.text)
            if m:
                return f"{m.group(1)}_{m.group(2)}_0"
    except Exception:
        pass
    return None


# ============================================================
# HOURS
# ============================================================
def _get_hours(session):
    """Haal lesuren op (08:25 - 15:30 etc.). 7 items, klein."""
    try:
        from smartschool import SmartschoolHours
        return [
            {"id": h.hour_id, "s": h.start, "e": h.end, "t": h.title}
            for h in SmartschoolHours(session)
        ]
    except Exception:
        return []


# ============================================================
# PLANNER ITEMS — via JSON API
# ============================================================
def _fetch_planned_elements(session, base, uid, from_iso, to_iso):
    """Haal alle planner items op (lessen + taken)."""
    path = (
        f"/planner/api/v1/planned-elements/user/{uid}"
        f"?from={from_iso}&to={to_iso}"
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
    except Exception:
        return []


# ============================================================
# COMPACT FORMAT
# ============================================================
def _compact_item(it):
    """
    Zet een volledig item om naar een compacte dict.
    Volledig: ~2KB per item
    Compact:  ~250 bytes per item (8x kleiner)
    """
    period = it.get("period") or {}
    courses = it.get("courses") or []
    locations = it.get("locations") or []
    organisers = (it.get("organisers") or {}).get("users") or []
    atype = it.get("assignmentType") or {}

    # Eén teacher naam (compact)
    teacher = ""
    if organisers:
        t = organisers[0].get("name") or {}
        teacher = t.get("startingWithLastName") or t.get("startingWithFirstName") or ""

    # Één course
    course = courses[0] if courses else {}
    course_name = course.get("name", "")

    # Één location
    location = ""
    if locations:
        location = locations[0].get("title") or locations[0].get("number") or ""

    # Tijden als unix-achtige ms (compact) i.p.v. ISO strings
    def to_ms(iso):
        if not iso:
            return None
        try:
            return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return None

    return {
        "i": it.get("id", ""),
        "n": it.get("name", "")[:200],     # afkappen op 200 chars
        "t": it.get("plannedElementType", ""),
        "df": to_ms(period.get("dateTimeFrom")),
        "dt": to_ms(period.get("dateTimeTo")),
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
    """
    Parse from/to uit query params.
    Default: 14 dagen terug, 30 dagen vooruit.
    """
    tz = timezone(timedelta(hours=2))
    now = datetime.now(tz)

    from_s = request.args.get("from")
    to_s = request.args.get("to")

    if from_s:
        try:
            from_dt = datetime.fromisoformat(from_s).replace(tzinfo=tz)
        except Exception:
            from_dt = (now - timedelta(days=14)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        from_dt = (now - timedelta(days=14)).replace(hour=0, minute=0, second=0, microsecond=0)

    if to_s:
        try:
            to_dt = datetime.fromisoformat(to_s).replace(tzinfo=tz)
        except Exception:
            to_dt = (now + timedelta(days=30)).replace(hour=23, minute=59, second=59, microsecond=0)
    else:
        to_dt = (now + timedelta(days=30)).replace(hour=23, minute=59, second=59, microsecond=0)

    # Hard cap: max 1 jaar om misbruik te voorkomen
    if (to_dt - from_dt).days > 366:
        to_dt = from_dt + timedelta(days=366)

    return from_dt, to_dt


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
      hours=0|1            (default: 1) — uren meesturen
    """
    base = base_url(_creds)
    uid = _get_user_id(_session, base)
    if not uid:
        return jsonify({"error": "kon user_id niet vinden"}), 500

    from_dt, to_dt = _parse_range()
    types_filter = request.args.get("types", "all")
    limit = min(int(request.args.get("limit", "500")), 2000)
    compact = request.args.get("compact", "1") == "1"
    include_hours = request.args.get("hours", "1") == "1"

    from_iso = from_dt.strftime("%Y-%m-%dT%H:%M:%S+02:00")
    to_iso = to_dt.strftime("%Y-%m-%dT%H:%M:%S+02:00")

    range_key = f"{from_iso}|{to_iso}"

    # Cache check
    raw = _cache_get(uid, range_key)
    if raw is None:
        raw = _fetch_planned_elements(_session, base, uid, from_iso, to_iso)
        _cache_set(uid, range_key, raw)

    # Filter op type
    if types_filter == "lessons":
        raw = [it for it in raw if it.get("plannedElementType") == "planned-lessons"]
    elif types_filter == "tasks":
        raw = [it for it in raw if it.get("plannedElementType") != "planned-lessons"]

    # Sort op starttijd
    raw.sort(key=lambda x: (x.get("period") or {}).get("dateTimeFrom") or "")

    # Limit
    total_before_limit = len(raw)
    raw = raw[:limit]

    # Compact of volledig
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
    Alleen deze week (maandag t/m zondag).
    Extra compact — geen meta, geen opties.

    Query params:
      offset=0            0 = deze week, -1 = vorige week, +1 = volgende week
    """
    base = base_url(_creds)
    uid = _get_user_id(_session, base)
    if not uid:
        return jsonify({"error": "kon user_id niet vinden"}), 500

    offset = int(request.args.get("offset", "0"))
    tz = timezone(timedelta(hours=2))
    now = datetime.now(tz)
    day = now.weekday()  # 0=ma
    monday = (now - timedelta(days=day) + timedelta(weeks=offset)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    sunday = (monday + timedelta(days=6)).replace(hour=23, minute=59, second=59, microsecond=0)

    from_iso = monday.strftime("%Y-%m-%dT%H:%M:%S+02:00")
    to_iso = sunday.strftime("%Y-%m-%dT%H:%M:%S+02:00")

    range_key = f"{from_iso}|{to_iso}"
    raw = _cache_get(uid, range_key)
    if raw is None:
        raw = _fetch_planned_elements(_session, base, uid, from_iso, to_iso)
        _cache_set(uid, range_key, raw)

    raw.sort(key=lambda x: (x.get("period") or {}).get("dateTimeFrom") or "")
    items = [_compact_item(it) for it in raw]

    return jsonify({
        "week_start": monday.strftime("%Y-%m-%d"),
        "week_end": sunday.strftime("%Y-%m-%d"),
        "hours": _get_hours(_session),
        "items": items,
    })


@bp.get("/item/<element_id>")
@with_session
def planner_item_detail(element_id, _session=None, _creds=None):
    """
    Detail van 1 item. Lazy loaded.
    Gebruikt het juiste sub-endpoint op basis van het element type.

    Query params:
      type=planned-to-dos|planned-assignments|planned-lessons
    """
    element_type = request.args.get("type", "")
    platform_id = _session.platform_id

    # Bepaal sub-endpoint
    if element_type == "planned-to-dos":
        prefix = "planned-to-dos"
    elif element_type == "planned-assignments":
        prefix = "planned-assignments"
    else:
        # Voor planned-lessons: haal uit volledige lijst
        return jsonify({"error": "detail niet beschikbaar voor dit type"}), 400

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
            {"name": a.get("name", ""), "file_id": a.get("fileID") or a.get("id")}
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
    Voer een actie uit op een planner item.

    Actions: resolve | unresolve | trash

    Query params:
      type=planned-to-dos|planned-assignments (verplicht, of 'auto')
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
        return jsonify({"error": f"actie niet ondersteund voor {element_type}"}), 400

    path = f"/planner/api/v1/{prefix}/{platform_id}/{element_id}/{action}"
    try:
        r = _session.request("POST", path)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    ok = r.status_code in (200, 204)

    # Invalideer cache voor deze user (data is gewijzigd)
    base = base_url(_creds)
    uid = _get_user_id(_session, base)
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

    dt_from_s = data.get("date_from")
    dt_to_s = data.get("date_to")

    tz = timezone(timedelta(hours=2))

    if dt_from_s:
        dt_from = datetime.fromisoformat(dt_from_s).replace(tzinfo=tz)
    else:
        dt_from = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    if dt_to_s:
        dt_to = datetime.fromisoformat(dt_to_s).replace(tzinfo=tz, hour=23, minute=59, second=59)
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
        r = _session.request("POST", "/planner/api/v1/planned-to-dos/", json=body)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Invalideer cache
    base = base_url(_creds)
    uid = _get_user_id(_session, base)
    if uid:
        _cache_invalidate(uid)

    return jsonify({
        "ok": r.status_code in (200, 201),
        "status": r.status_code,
        "response": r.text[:500],
    })
