"""
Planner endpoints.
"""

from datetime import datetime

from flask import Blueprint, jsonify, request
from smartschool import PlannedElements

from modules.common import with_session, strip_html


bp = Blueprint("planner", __name__, url_prefix="/api/planner")


def _element_dict(el):
    p = el.period
    return {
        "platform_id": el.platform_id,
        "id": el.id,
        "name": el.name,
        "type": el.planned_element_type,
        "courses": [c.name for c in el.courses] if el.courses else [],
        "date_from": p.date_time_from.isoformat() if p.date_time_from else None,
        "date_to": p.date_time_to.isoformat() if p.date_time_to else None,
        "status": el.resolved_status or None,
        "pinned": el.pinned,
    }


def _planner_detail(session, el):
    if el.planned_element_type == "planned-to-dos":
        path = f"/planner/api/v1/planned-to-dos/{el.platform_id}/{el.id}"
    elif el.planned_element_type == "planned-assignments":
        path = f"/planner/api/v1/planned-assignments/{el.platform_id}/{el.id}"
    else:
        return None
    try:
        return session.json(path)
    except Exception:
        return None


def _planner_action(session, el, action):
    if el.planned_element_type == "planned-to-dos":
        prefix = "planned-to-dos"
    elif el.planned_element_type == "planned-assignments":
        prefix = "planned-assignments"
    else:
        return False
    path = f"/planner/api/v1/{prefix}/{el.platform_id}/{el.id}/{action}"
    try:
        r = session.request("POST", path)
        return r.status_code in (200, 204)
    except Exception:
        return False


@bp.get("")
@with_session
def planner_list(_session=None, _creds=None):
    with_detail = request.args.get("detail", "0") == "1"
    elements = list(PlannedElements(_session))
    out = []
    for el in elements:
        d = _element_dict(el)
        if with_detail:
            detail = _planner_detail(_session, el) or {}
            d["info"] = strip_html(detail.get("publicInfo") or "")
            d["attachments"] = [{"name": a.get("name")}
                                for a in (detail.get("attachments") or [])]
            d["weblinks"] = [l.get("url", l)
                             for l in (detail.get("weblinks") or [])]
        out.append(d)
    return jsonify({"count": len(out), "items": out})


@bp.post("/<platform_id>/<element_id>/<action>")
@with_session
def planner_item_action(platform_id, element_id, action,
                        _session=None, _creds=None):
    if action not in ("resolve", "unresolve", "trash"):
        return jsonify({"error": "ongeldige actie"}), 400
    elements = list(PlannedElements(_session))
    target = None
    for el in elements:
        if (str(el.platform_id) == str(platform_id)
                and str(el.id) == str(element_id)):
            target = el
            break
    if not target:
        return jsonify({"error": "element niet gevonden"}), 404
    ok = _planner_action(_session, target, action)
    if not ok:
        return jsonify({"error": f"Smartschool weigerde actie '{action}'",
                        "ok": False}), 502
    return jsonify({"ok": True, "action": action})


@bp.post("/todo")
@with_session
def planner_create_todo(_session=None, _creds=None):
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name")
    if not name:
        return jsonify({"error": "name verplicht"}), 400

    description = data.get("description", "")
    color = data.get("color", "tangerine-200")
    icon = data.get("icon", "icon_fill_flag")

    dt_from_s = data.get("date_from")
    dt_to_s = data.get("date_to")

    dt_from = (datetime.fromisoformat(dt_from_s) if dt_from_s
               else datetime.now().replace(hour=0, minute=0, second=0,
                                           microsecond=0))
    dt_to = (datetime.fromisoformat(dt_to_s) if dt_to_s
             else dt_from.replace(hour=23, minute=59, second=59))

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
        return jsonify({"ok": r.status_code in (200, 201),
                        "status": r.status_code,
                        "response": r.text[:500]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
