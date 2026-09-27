import os
import re
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
from smartschool import Smartschool, Credentials, PlannedElements

app = Flask(__name__)
CORS(app)


class InlineCredentials(Credentials):
    def __init__(self, username, password, main_url, mfa):
        self._username = username
        self._password = password
        self._main_url = main_url
        self._mfa = mfa

    @property
    def username(self):
        return self._username

    @username.setter
    def username(self, value):
        self._username = value

    @property
    def password(self):
        return self._password

    @password.setter
    def password(self, value):
        self._password = value

    @property
    def main_url(self):
        return self._main_url

    @main_url.setter
    def main_url(self, value):
        self._main_url = value

    @property
    def mfa(self):
        return self._mfa

    @mfa.setter
    def mfa(self, value):
        self._mfa = value


def _get_params():
    if request.method == "POST" and request.is_json:
        return request.get_json() or {}
    return request.args


def _build_session():
    params = _get_params()
    username = params.get("username")
    password = params.get("password")
    main_url = params.get("main_url")
    mfa = params.get("mfa")

    if not all([username, password, main_url, mfa]):
        raise ValueError("Missing credentials")

    return Smartschool(InlineCredentials(username, password, main_url, mfa))


def strip_html(html):
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return text.strip()


def get_detail(session, element):
    if element.planned_element_type == "planned-to-dos":
        path = f"/planner/api/v1/planned-to-dos/{element.platform_id}/{element.id}"
    elif element.planned_element_type == "planned-assignments":
        path = f"/planner/api/v1/planned-assignments/{element.platform_id}/{element.id}"
    else:
        return None

    try:
        return session.json(path)
    except Exception:
        return None


def _serialize_element(session, element, include_detail=False):
    data = {
        "id": element.id,
        "platform_id": element.platform_id,
        "name": element.name,
        "courses": [c.name for c in element.courses],
        "type": element.planned_element_type,
        "status": element.resolved_status,
        "color": element.color,
        "icon": getattr(element, "icon", None),
        "pinned": element.pinned,
        "unconfirmed": element.unconfirmed,
        "sort": element.sort,
        "period": {
            "date_time_from": element.period.date_time_from.isoformat() if element.period.date_time_from else None,
            "date_time_to": element.period.date_time_to.isoformat() if element.period.date_time_to else None,
            "whole_day": element.period.whole_day,
            "deadline": getattr(element.period, "deadline", None),
        },
    }

    if include_detail:
        detail = get_detail(session, element)
        data["public_info_html"] = (detail or {}).get("publicInfo") or ""
        data["public_info_text"] = strip_html((detail or {}).get("publicInfo") or "")
        data["attachments"] = (detail or {}).get("attachments") or []
        data["weblinks"] = (detail or {}).get("weblinks") or []

    return data


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok"}), 200


@app.route("/planner", methods=["GET"])
def get_planner():
    try:
        session = _build_session()
        elements = list(PlannedElements(session))
        return jsonify({
            "status": "success",
            "count": len(elements),
            "data": [_serialize_element(session, el, include_detail=True) for el in elements],
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/planner/summary", methods=["GET"])
def get_planner_summary():
    try:
        session = _build_session()
        elements = list(PlannedElements(session))
        return jsonify({
            "status": "success",
            "count": len(elements),
            "data": [_serialize_element(session, el, include_detail=False) for el in elements],
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/element", methods=["GET"])
def get_element():
    params = _get_params()
    element_id = params.get("id")
    platform_id = params.get("platform_id")
    element_type = params.get("type", "planned-to-dos")

    if not all([element_id, platform_id]):
        return jsonify({"status": "error", "message": "Missing id or platform_id"}), 400

    if element_type == "planned-to-dos":
        path = f"/planner/api/v1/planned-to-dos/{platform_id}/{element_id}"
    elif element_type == "planned-assignments":
        path = f"/planner/api/v1/planned-assignments/{platform_id}/{element_id}"
    else:
        return jsonify({"status": "error", "message": "Unknown type"}), 400

    try:
        session = _build_session()
        detail = session.json(path)
        detail["public_info_text"] = strip_html(detail.get("publicInfo") or "")
        return jsonify({"status": "success", "data": detail})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/element/resolve", methods=["POST"])
def resolve_element():
    return _element_action("resolve")


@app.route("/element/unresolve", methods=["POST"])
def unresolve_element():
    return _element_action("unresolve")


@app.route("/element/trash", methods=["POST"])
def trash_element():
    return _element_action("trash")


def _element_action(action):
    params = _get_params()
    username = params.get("username")
    password = params.get("password")
    main_url = params.get("main_url")
    mfa = params.get("mfa")
    element_id = params.get("id")
    platform_id = params.get("platform_id")
    element_type = params.get("type", "planned-to-dos")

    if not all([username, password, main_url, mfa, element_id, platform_id]):
        return jsonify({"status": "error", "message": "Missing required parameters"}), 400

    if element_type == "planned-to-dos":
        prefix = "planned-to-dos"
    elif element_type == "planned-assignments":
        prefix = "planned-assignments"
    else:
        return jsonify({"status": "error", "message": "Unknown type"}), 400

    path = f"/planner/api/v1/{prefix}/{platform_id}/{element_id}/{action}"

    try:
        session = Smartschool(InlineCredentials(username, password, main_url, mfa))
        response = session.request(method="POST", url=path)

        if response.status_code in (200, 204):
            try:
                body = response.json()
            except Exception:
                body = None
            return jsonify({
                "status": "success",
                "action": action,
                "http_status": response.status_code,
                "result": body,
            })

        return jsonify({
            "status": "error",
            "action": action,
            "http_status": response.status_code,
            "message": response.text[:300],
        }), response.status_code

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/element/create", methods=["POST"])
def create_element():
    params = _get_params()
    username = params.get("username")
    password = params.get("password")
    main_url = params.get("main_url")
    mfa = params.get("mfa")
    name = params.get("name")
    description = params.get("description", "")
    color = params.get("color", "tangerine-200")
    icon = params.get("icon", "icon_fill_flag")

    if not all([username, password, main_url, mfa, name]):
        return jsonify({"status": "error", "message": "Missing required parameters"}), 400

    date_from = params.get("date_from")
    date_to = params.get("date_to")

    dt_from = datetime.fromisoformat(date_from) if date_from else datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    dt_to = datetime.fromisoformat(date_to) if date_to else dt_from.replace(hour=23, minute=59, second=59)

    body = {
        "name": name,
        "publicInfo": f"<p>{description}</p>" if description else "",
        "color": color,
        "icon": icon,
        "period": {
            "dateTimeFrom": dt_from.strftime("%Y-%m-%dT%H:%M:%S+02:00"),
            "dateTimeTo": dt_to.strftime("%Y-%m-%dT%H:%M:%S+02:00"),
            "wholeDay": params.get("whole_day", True),
        },
    }

    try:
        session = Smartschool(InlineCredentials(username, password, main_url, mfa))
        response = session.request(method="POST", url="/planner/api/v1/planned-to-dos/", json=body)

        if response.status_code in (200, 201):
            try:
                result = response.json()
            except Exception:
                result = None
            return jsonify({
                "status": "success",
                "action": "create",
                "http_status": response.status_code,
                "result": result,
            }), 201

        return jsonify({
            "status": "error",
            "action": "create",
            "http_status": response.status_code,
            "message": response.text[:300],
        }), response.status_code

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.errorhandler(404)
def not_found(_):
    return jsonify({"status": "error", "message": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(_):
    return jsonify({"status": "error", "message": "Method not allowed"}), 405


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
