"""
Planner endpoints — webversie.

API structuur:
  GET  /api/planner                     → compact: uren + items
  GET  /api/planner/week?offset=0       → week + aanvulling lege uren
  GET  /api/planner/todos?scope=open    → enkel taken (snel)
  GET  /api/planner/item/<id>?type=...  → detail van 1 item
  POST /api/planner/<pid>/<eid>/<action>?type=...  → resolve | unresolve | trash
  POST /api/planner/todo                → nieuwe to-do
  GET  /api/planner/my-class            → detecteer klas

Query params (/api/planner):
  from=YYYY-MM-DD        startdatum (default: vandaag - 14d)
  to=YYYY-MM-DD          einddatum (default: vandaag + 30d)
  types=lessons|tasks|all
  limit=N                (default: 500, max: 2000)
  compact=0|1            (default: 1)
  hours=0|1              (default: 1)
  placeholders=0|1       (default: 0)

Query params (/api/planner/week):
  offset=0               0 = deze week, -1 = vorige, +1 = volgende
  placeholders=0|1
  fill=0|1               (default: 1)

Query params (/api/planner/todos):
  scope=open|today|week|month|all   (default: open = 7d terug, 7d vooruit)
  limit=N                            (default: 100, max: 500)

Features:
  - URL-encoding van +02:00 in from/to ISO strings
  - Unicode unescape van Homepage HTML (\\u0022 → ")
  - Placeholders standaard verborgen
  - Klas-detectie via schoolrooster (gecached 1u)
  - Aanvulling lege uren uit schoolrooster (geen blokkade)
  - Examen-detectie
"""

import json
import random
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import Blueprint, jsonify, request

from modules.common import with_session, strip_html, base_url


bp = Blueprint("planner", __name__, url_prefix="/api/planner")


# ============================================================
# CONFIG
# ============================================================
ROOSTER_URL = ("https://lesroosters.stjozefasoessen.be/json/"
               "Rooster_actueel.json")
EXAMEN_URLS = [
    "https://lesroosters.stjozefasoessen.be/json/Rooster_examens.json",
    "https://lesroosters.stjozefasoessen.be/json/Examens_actueel.json",
    "https://lesroosters.stjozefasoessen.be/json/Rooster_examen.json",
]

DAG_MAP = {0: "Maandag", 1: "Dinsdag", 2: "Woensdag",
           3: "Donderdag", 4: "Vrijdag", 5: "Zaterdag", 6: "Zondag"}
UUR_MAP = {
    "08:25": "1", "09:15": "2", "10:20": "3", "11:10": "4",
    "12:50": "5", "13:40": "6", "14:40": "7"
}
UUR_TIJDEN = {
    "1": ("08:25", "09:15"), "2": ("09:15", "10:05"),
    "3": ("10:20", "11:10"), "4": ("11:10", "12:00"),
    "5": ("12:50", "13:40"), "6": ("13:40", "14:30"),
    "7": ("14:40", "15:30"),
}


# ============================================================
# CACHE — items per user, korte TTL
# ============================================================
_CACHE = {}
_CACHE_TTL = 30


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
# KLAS CACHE — 1x detecteren per user
# ============================================================
_KLAS_CACHE = {}
_KLAS_TTL = 3600  # 1 uur


def _get_cached_class(session, base, uid):
    """Detecteer klas op basis van huidige week (gecached)."""
    now_ts = time.time()
    entry = _KLAS_CACHE.get(uid)
    if entry and now_ts - entry["ts"] < _KLAS_TTL:
        return entry["class"], entry["score"]

    tz = timezone(timedelta(hours=2))
    now = datetime.now(tz)
    cur_monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    cur_sunday = cur_monday + timedelta(days=6)

    from_iso = cur_monday.strftime("%Y-%m-%dT%H:%M:%S+02:00")
    to_iso = cur_sunday.strftime("%Y-%m-%dT%H:%M:%S+02:00")

    items = _fetch_planned_elements(session, base, uid, from_iso, to_iso)
    items = _filter_placeholders(items, False)

    rooster = _fetch_school_rooster()
    if not rooster:
        return None, 0

    klas, score = _detect_class(items, rooster)

    if klas:
        _KLAS_CACHE[uid] = {
            "class": klas,
            "score": score,
            "ts": now_ts,
        }
    return klas, score


# ============================================================
# ROOSTER CACHE — school-breed, 5 min TTL
# ============================================================
_ROOSTER_CACHE = {"data": None, "ts": 0}
_ROOSTER_TTL = 300


def _fetch_json_url(url):
    """Haal JSON op met BOM fix + random cache-buster."""
    try:
        req = urllib.request.Request(
            f"{url}?_={random.randint(100000, 999999)}",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            if raw[:1] not in (b"{", b"\xef"):
                return None
            return json.loads(raw.decode("utf-8-sig"))
    except Exception as e:
        print(f"[planner] fetch {url} fout: {type(e).__name__}: {e}",
              file=sys.stderr)
        return None


def _fetch_school_rooster():
    now = time.time()
    if (_ROOSTER_CACHE["data"]
            and now - _ROOSTER_CACHE["ts"] < _ROOSTER_TTL):
        return _ROOSTER_CACHE["data"]

    data = _fetch_json_url(ROOSTER_URL)
    if data and "Klassen" in data:
        _ROOSTER_CACHE["data"] = data
        _ROOSTER_CACHE["ts"] = now
        return data
    return None


def _fetch_examen_rooster():
    for url in EXAMEN_URLS:
        data = _fetch_json_url(url)
        if data and "Klassen" in data:
            return data
    return None


# ============================================================
# HTML UNESCAPE
# ============================================================
def _unescape_html(html):
    if not html:
        return html
    return re.sub(r'\\u([0-9a-fA-F]{4})',
                  lambda m: chr(int(m.group(1), 16)), html)


# ============================================================
# USER ID
# ============================================================
def _find_user_id(session, base):
    try:
        r = session.request("GET", "/?module=Homepage",
                            headers={"Referer": base + "/"})
        html = r.text
        if r.status_code in (301, 302, 303, 307, 308):
            return None
        if not html:
            return None
        html_clean = _unescape_html(html)
        for source in (html_clean, html):
            for pat in [
                r'"authenticatedUser"\s*:\s*\{[^}]*"id"\s*:\s*"(\d+)_(\d+)_',
                r'"userIdentifier"\s*:\s*"(\d+)_(\d+)_',
            ]:
                m = re.search(pat, source)
                if m:
                    return int(m.group(2))
        platform = getattr(session, "platform_id", None)
        if platform:
            for source in (html_clean, html):
                m = re.search(rf'{platform}_(\d+)_0', source)
                if m:
                    return int(m.group(1))
        return None
    except Exception as e:
        print(f"[planner] _find_user_id fout: {e}", file=sys.stderr)
        return None


def _get_uid(session, base):
    platform = getattr(session, "platform_id", None)
    user_num = _find_user_id(session, base)
    if not user_num or not platform:
        return None
    return f"{platform}_{user_num}_0"


# ============================================================
# HOURS
# ============================================================
def _get_hours(session):
    try:
        from smartschool import SmartschoolHours
        return [
            {"id": h.hour_id, "s": h.start, "e": h.end, "t": h.title}
            for h in SmartschoolHours(session)
        ]
    except Exception as e:
        print(f"[planner] hours fout: {e}", file=sys.stderr)
        return []


# ============================================================
# PLANNER ITEMS
# ============================================================
def _fetch_planned_elements(session, base, uid, from_iso, to_iso):
    """URL-encoding van +02:00 is cruciaal!"""
    from_enc = quote(from_iso, safe='')
    to_enc = quote(to_iso, safe='')

    path = (f"/planner/api/v1/planned-elements/user/{uid}"
            f"?from={from_enc}&to={to_enc}"
            f"&includes=icon,courses,locations,upload-folders")
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
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(
            iso.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def _compact_item(it):
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
    if include:
        return raw
    return [it for it in raw
            if it.get("plannedElementType") != "planned-placeholders"]


# ============================================================
# KLAS DETECTIE
# ============================================================
def _detect_class(items, rooster):
    if not rooster or not items:
        return None, 0

    lkr_map = {}
    for lkr in rooster.get("Leerkrachten", []):
        code = lkr.get("Code", "")
        naam = lkr.get("DisplayName", "")
        if naam:
            lkr_map[naam.lower()] = code
            parts = naam.split()
            if len(parts) == 2:
                lkr_map[f"{parts[1]} {parts[0]}".lower()] = code

    vak_map = {}
    for v in rooster.get("Vakken", []):
        naam = v.get("DisplayName", "")
        if naam:
            vak_map[naam.lower()] = v.get("Code", "")

    jouw_lessen = []
    for it in items:
        if it.get("plannedElementType") != "planned-lessons":
            continue
        period = it.get("period") or {}
        df = period.get("dateTimeFrom") or ""
        try:
            dt = datetime.fromisoformat(df.replace("Z", "+00:00"))
            dag = DAG_MAP[dt.weekday()]
            uur = UUR_MAP.get(dt.strftime("%H:%M"))
            if not uur:
                continue
            courses = it.get("courses") or []
            locations = it.get("locations") or []
            organisers = (it.get("organisers") or {}).get("users") or []

            vak_naam = courses[0].get("name", "") if courses else ""
            vak_code = vak_map.get(vak_naam.lower(), vak_naam)
            lokaal = locations[0].get("title", "") if locations else ""

            lkr_naam = ""
            if organisers:
                n = organisers[0].get("name") or {}
                lkr_naam = (n.get("startingWithLastName")
                            or n.get("startingWithFirstName") or "")
            lkr_code = lkr_map.get(lkr_naam.lower(), "")

            jouw_lessen.append({
                "dag": dag, "uur": uur,
                "vak_code": vak_code, "lokaal": lokaal,
                "lkr_code": lkr_code,
            })
        except Exception:
            continue

    if not jouw_lessen:
        return None, 0

    scores = {}
    for klas in rooster.get("Klassen", []):
        klas_naam = klas.get("Class")
        klas_lessen = klas.get("Lessons", [])
        vak_m = lokaal_m = lkr_m = 0
        for jl in jouw_lessen:
            for kl in klas_lessen:
                if (jl["dag"] == kl.get("Dag")
                        and jl["uur"] == kl.get("Uur")):
                    if jl["vak_code"] == kl.get("Vak"):
                        vak_m += 1
                    if jl["lokaal"] == kl.get("Lokaal"):
                        lokaal_m += 1
                    if jl["lkr_code"] == kl.get("Leerkracht"):
                        lkr_m += 1
                    break
        score = (vak_m + lokaal_m + lkr_m) / (3 * len(jouw_lessen))
        scores[klas_naam] = score

    if not scores:
        return None, 0
    beste = max(scores, key=scores.get)
    return beste, scores[beste]


def _planner_matches_rooster(items, klas_naam, rooster):
    if not rooster or not klas_naam:
        return 0
    klas_data = None
    for k in rooster.get("Klassen", []):
        if k.get("Class") == klas_naam:
            klas_data = k
            break
    if not klas_data:
        return 0

    rooster_set = set()
    for les in klas_data.get("Lessons", []):
        rooster_set.add((les.get("Dag"), les.get("Uur")))

    match = totaal = 0
    for it in items:
        if it.get("plannedElementType") != "planned-lessons":
            continue
        period = it.get("period") or {}
        df = period.get("dateTimeFrom") or ""
        try:
            dt = datetime.fromisoformat(df.replace("Z", "+00:00"))
            dag = DAG_MAP[dt.weekday()]
            uur = UUR_MAP.get(dt.strftime("%H:%M"))
            if not uur:
                continue
            totaal += 1
            if (dag, uur) in rooster_set:
                match += 1
        except Exception:
            pass

    return match / max(totaal, 1)


# ============================================================
# EXAMEN-DETECTIE
# ============================================================
def _is_exam_week(items, klas_score, match_ratio):
    if klas_score < 0.5:
        return True, "klas_score laag"
    if match_ratio < 0.5:
        return True, "rooster-match laag"

    onbekend = totaal = 0
    for it in items:
        if it.get("plannedElementType") != "planned-lessons":
            continue
        totaal += 1
        courses = it.get("courses") or []
        if not courses:
            onbekend += 1
            continue
        naam = (courses[0].get("name") or "").upper()
        if naam in ("SEM", "SES", "SEMINARIE", "EXAMEN"):
            onbekend += 1

    if totaal and onbekend / totaal > 0.5:
        return True, f"{onbekend}/{totaal} examen-vakken"
    return False, ""


# ============================================================
# LEGE UREN OP VULLEN
# ============================================================
def _fill_empty_slots(items, klas_naam, rooster, week_start):
    """
    Vul lege uren op basis van schoolrooster.
    Hele-dag activiteiten (sportdag, uitstap) blokkeren NIET.
    """
    if not rooster or not klas_naam:
        return []

    klas_data = None
    for k in rooster.get("Klassen", []):
        if k.get("Class") == klas_naam:
            klas_data = k
            break
    if not klas_data:
        return []

    rooster_lessen = {}
    for les in klas_data.get("Lessons", []):
        dag, uur = les.get("Dag"), les.get("Uur")
        if dag and uur:
            rooster_lessen[(dag, uur)] = {
                "vak": les.get("Vak", ""),
                "lokaal": les.get("Lokaal", ""),
                "lkr_code": les.get("Leerkracht", ""),
            }

    vak_code_naar_naam = {v.get("Code", ""): v.get("DisplayName", "")
                          for v in rooster.get("Vakken", [])}
    lkr_code_naar_naam = {l.get("Code", ""): l.get("DisplayName", "")
                          for l in rooster.get("Leerkrachten", [])}

    bezet = set()
    for it in items:
        period = it.get("period") or {}
        df = period.get("dateTimeFrom") or ""
        try:
            dt = datetime.fromisoformat(df.replace("Z", "+00:00"))
            dag = DAG_MAP[dt.weekday()]
            uur = UUR_MAP.get(dt.strftime("%H:%M"))
            if dag and uur:
                bezet.add((dag, uur))
        except Exception:
            pass

    filled = []
    alle_dagen = ["Maandag", "Dinsdag", "Woensdag", "Donderdag",
                  "Vrijdag", "Zaterdag", "Zondag"]

    for i, dag in enumerate(alle_dagen):
        d = week_start + timedelta(days=i)
        for uur_num in ["1", "2", "3", "4", "5", "6", "7"]:
            if (dag, uur_num) in bezet:
                continue
            les = rooster_lessen.get((dag, uur_num))
            if not les:
                continue

            start, end = UUR_TIJDEN[uur_num]
            df_iso = f"{d.strftime('%Y-%m-%d')}T{start}:00+02:00"
            dt_iso = f"{d.strftime('%Y-%m-%d')}T{end}:00+02:00"

            filled.append({
                "i": f"filled_{d.strftime('%Y%m%d')}_{uur_num}",
                "n": "",
                "t": "planned-lessons",
                "df": int(datetime.fromisoformat(df_iso).timestamp() * 1000),
                "dt": int(datetime.fromisoformat(dt_iso).timestamp() * 1000),
                "wd": 0,
                "dl": 0,
                "c": vak_code_naar_naam.get(les["vak"], les["vak"]),
                "l": les["lokaal"],
                "te": lkr_code_naar_naam.get(les["lkr_code"],
                                              les["lkr_code"]),
                "ab": "",
                "col": "blue-200",
                "st": "",
                "_filled": True,
            })

    return filled


# ============================================================
# WEEK RESPONSE BUILDER
# ============================================================
def _build_week_response(session, base, uid, monday, include_hours=True,
                          include_placeholders=False, do_fill=True):
    sunday = monday + timedelta(days=6)
    from_iso = monday.strftime("%Y-%m-%dT%H:%M:%S+02:00")
    to_iso = sunday.strftime("%Y-%m-%dT%H:%M:%S+02:00")

    raw_key = f"{from_iso}|{to_iso}|raw"
    raw = _cache_get(uid, raw_key)
    if raw is None:
        raw = _fetch_planned_elements(session, base, uid,
                                      from_iso, to_iso)
        _cache_set(uid, raw_key, raw)

    filtered = _filter_placeholders(raw, include_placeholders)

    rooster = _fetch_school_rooster()
    klas = None
    score = 0.0
    match_ratio = 0.0
    examen = False
    examen_reden = ""

    if rooster:
        klas, score = _get_cached_class(session, base, uid)
        if klas:
            match_ratio = _planner_matches_rooster(filtered, klas, rooster)
        examen, examen_reden = _is_exam_week(filtered, score, match_ratio)

        if examen and do_fill:
            ex_rooster = _fetch_examen_rooster()
            if ex_rooster:
                rooster = ex_rooster
            else:
                do_fill = False

    items = [_compact_item(it) for it in filtered]

    filled = []
    if do_fill and klas:
        filled = _fill_empty_slots(filtered, klas, rooster, monday)

    all_items = items + filled

    result = {
        "week_start": monday.strftime("%Y-%m-%d"),
        "week_end": sunday.strftime("%Y-%m-%d"),
        "items": all_items,
        "meta": {
            "user_id": uid,
            "count": len(all_items),
            "original_count": len(items),
            "filled_count": len(filled),
            "class": klas,
            "class_score": round(score, 3),
            "match_ratio": round(match_ratio, 3),
            "exam_week": examen,
            "exam_reason": examen_reden,
        },
    }

    if include_hours:
        result["hours"] = _get_hours(session)

    return result


# ============================================================
# ROUTES — LIST
# ============================================================
@bp.get("")
@with_session
def planner_list(_session=None, _creds=None):
    """Planner items voor een range (default: 14d terug, 30d vooruit)."""
    base = base_url(_creds)
    uid = _get_uid(_session, base)
    if not uid:
        return jsonify({"error": "kon user_id niet vinden"}), 500

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

    raw_key = f"{from_iso}|{to_iso}|raw"
    raw = _cache_get(uid, raw_key)
    if raw is None:
        raw = _fetch_planned_elements(_session, base, uid,
                                      from_iso, to_iso)
        _cache_set(uid, raw_key, raw)

    raw = _filter_placeholders(raw, include_placeholders)

    if types_filter == "lessons":
        raw = [it for it in raw
               if it.get("plannedElementType") == "planned-lessons"]
    elif types_filter == "tasks":
        raw = [it for it in raw
               if it.get("plannedElementType") != "planned-lessons"]

    raw.sort(key=lambda x: (x.get("period") or {}).get("dateTimeFrom") or "")
    total_before_limit = len(raw)
    raw = raw[:limit]

    items = [_compact_item(it) for it in raw] if compact else raw

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
    """Deze/vorige/volgende week met aanvulling."""
    base = base_url(_creds)
    uid = _get_uid(_session, base)
    if not uid:
        return jsonify({"error": "kon user_id niet vinden"}), 500

    try:
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        offset = 0

    include_placeholders = request.args.get("placeholders", "0") == "1"
    do_fill = request.args.get("fill", "1") == "1"

    tz = timezone(timedelta(hours=2))
    now = datetime.now(tz)
    day = now.weekday()
    monday = (now - timedelta(days=day) + timedelta(weeks=offset)).replace(
        hour=0, minute=0, second=0, microsecond=0)

    result = _build_week_response(
        _session, base, uid, monday,
        include_hours=True,
        include_placeholders=include_placeholders,
        do_fill=do_fill,
    )
    return jsonify(result)


# ============================================================
# ROUTE — TODOS (snel)
# ============================================================
@bp.get("/todos")
@with_session
def planner_todos(_session=None, _creds=None):
    """
    Enkel taken/opdrachten, snel en compact.

    Query params:
      scope=open|today|week|month|all
        open  = 7 dagen terug tot 7 dagen vooruit (default)
        today = vandaag
        week  = deze week (ma-zo)
        month = deze maand
        all   = 30 dagen terug tot 180 dagen vooruit
      limit=N (default 100, max 500)
    """
    base = base_url(_creds)
    uid = _get_uid(_session, base)
    if not uid:
        return jsonify({"error": "kon user_id niet vinden"}), 500

    scope = request.args.get("scope", "open")
    try:
        limit = min(int(request.args.get("limit", "100")), 500)
    except ValueError:
        limit = 100

    tz = timezone(timedelta(hours=2))
    now = datetime.now(tz)

    if scope == "today":
        from_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        to_dt = from_dt + timedelta(days=1)
    elif scope == "week":
        from_dt = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        to_dt = from_dt + timedelta(days=7)
    elif scope == "month":
        from_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        to_dt = from_dt + timedelta(days=31)
    elif scope == "all":
        from_dt = (now - timedelta(days=30)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        to_dt = (now + timedelta(days=180)).replace(
            hour=23, minute=59, second=59, microsecond=0)
    else:  # open (default) — 7 dagen terug, 7 dagen vooruit
        from_dt = (now - timedelta(days=7)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        to_dt = (now + timedelta(days=7)).replace(
            hour=23, minute=59, second=59, microsecond=0)

    from_iso = from_dt.strftime("%Y-%m-%dT%H:%M:%S+02:00")
    to_iso = to_dt.strftime("%Y-%m-%dT%H:%M:%S+02:00")

    raw_key = f"{from_iso}|{to_iso}|raw"
    raw = _cache_get(uid, raw_key)
    if raw is None:
        raw = _fetch_planned_elements(_session, base, uid,
                                      from_iso, to_iso)
        _cache_set(uid, raw_key, raw)

    # Enkel taken (geen lessen/placeholders)
    tasks = [it for it in raw
             if it.get("plannedElementType") in
             ("planned-to-dos", "planned-assignments")]

    items = [_compact_item(it) for it in tasks]

    # Sorteer: open eerst, dan op datum
    items.sort(key=lambda x: (
        1 if x["st"] == "resolved" else 0,
        x["df"] or 0,
    ))

    items = items[:limit]

    open_count = sum(1 for it in items if it["st"] != "resolved")

    return jsonify({
        "meta": {
            "count": len(items),
            "open_count": open_count,
            "scope": scope,
            "from": from_iso,
            "to": to_iso,
            "user_id": uid,
        },
        "items": items,
    })


# ============================================================
# ROUTES — DETAIL / ACTIONS / TODO
# ============================================================
@bp.get("/item/<element_id>")
@with_session
def planner_item_detail(element_id, _session=None, _creds=None):
    element_type = request.args.get("type", "")
    platform_id = _session.platform_id

    if element_type == "planned-to-dos":
        prefix = "planned-to-dos"
    elif element_type == "planned-assignments":
        prefix = "planned-assignments"
    else:
        return jsonify({"error": "detail niet beschikbaar"}), 400

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

    ok = r.status_code in (200, 201)
    return jsonify({
        "ok": ok,
        "status": r.status_code,
        "response": r.text[:500],
    }), (200 if ok else 502)


# ============================================================
# KLAS DETECTIE ENDPOINT
# ============================================================
@bp.get("/my-class")
@with_session
def planner_my_class(_session=None, _creds=None):
    """Detecteer klas via schoolrooster matching."""
    base = base_url(_creds)
    uid = _get_uid(_session, base)
    if not uid:
        return jsonify({"error": "kon user_id niet vinden"}), 500

    klas, score = _get_cached_class(_session, base, uid)
    if not klas:
        return jsonify({
            "error": "kon klas niet detecteren",
            "score": round(score, 3),
        }), 404

    tz = timezone(timedelta(hours=2))
    now = datetime.now(tz)
    day = now.weekday()
    monday = (now - timedelta(days=day)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    sunday = monday + timedelta(days=6)

    from_iso = monday.strftime("%Y-%m-%dT%H:%M:%S+02:00")
    to_iso = sunday.strftime("%Y-%m-%dT%H:%M:%S+02:00")

    raw = _fetch_planned_elements(_session, base, uid, from_iso, to_iso)
    raw = _filter_placeholders(raw, False)

    rooster = _fetch_school_rooster()
    match_ratio = _planner_matches_rooster(raw, klas, rooster) if rooster else 0
    examen, reden = _is_exam_week(raw, score, match_ratio)

    return jsonify({
        "class": klas,
        "score": round(score, 3),
        "match_ratio": round(match_ratio, 3),
        "exam_week": examen,
        "exam_reason": reden if examen else "",
        "source": "rooster_match",
    })
