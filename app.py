"""
Smartschool API — Flask app (single file)

Endpoints:
  GET    /                                    → health/info
  GET    /api/grades                          → alle cijfers (?detail=1)
  GET    /api/grades/evaluation/<id>          → detail van 1 evaluatie
  GET    /api/messages?box=inbox              → lijst (inbox|outbox|trash|archive)
  GET    /api/messages/<id>?box=inbox         → bericht lezen
  GET    /api/messages/<id>/attachment/<fid>  → bijlage downloaden
  POST   /api/messages/<id>/action            → {action: unread|archive|trash|label, label?}
  GET    /api/messages/search-users?q=...     → gebruikers zoeken
  POST   /api/messages/send                   → {subject, message, to, cc?, bcc?, send_date?}
  GET    /api/profile                         → persoonlijke gegevens
  PATCH  /api/profile                         → {changes: {veld: waarde}}
  GET    /api/planner                         → planner items (?detail=1)
  POST   /api/planner/<pid>/<eid>/<action>    → resolve|unresolve|trash
  POST   /api/planner/todo                    → nieuwe to-do
"""

import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from io import BytesIO

from flask import Flask, jsonify, request, send_file
from requests_toolbelt.multipart.encoder import MultipartEncoder
from smartschool import (
    Smartschool, EnvCredentials, PlannedElements,
    MarkMessageUnread, MessageMoveToArchive, MessageMoveToTrash,
    AdjustMessageLabel, MessageLabel,
)
from werkzeug.middleware.proxy_fix import ProxyFix

# ============================================================
# CONFIG — via Render Environment Variables
# ============================================================
USERNAME = os.environ.get("SMARTSCHOOL_USERNAME")
PASSWORD = os.environ.get("SMARTSCHOOL_PASSWORD")
MAIN_URL = os.environ.get("SMARTSCHOOL_MAIN_URL")
MFA      = os.environ.get("SMARTSCHOOL_MFA")

if not all([USERNAME, PASSWORD, MAIN_URL, MFA]):
    raise RuntimeError(
        "Ontbrekende env vars: SMARTSCHOOL_USERNAME, SMARTSCHOOL_PASSWORD, "
        "SMARTSCHOOL_MAIN_URL, SMARTSCHOOL_MFA"
    )

# De smartschool library leest deze env vars via EnvCredentials
os.environ["SMARTSCHOOL_USERNAME"] = USERNAME
os.environ["SMARTSCHOOL_PASSWORD"] = PASSWORD
os.environ["SMARTSCHOOL_MAIN_URL"] = MAIN_URL
os.environ["SMARTSCHOOL_MFA"] = MFA

BASE_URL = MAIN_URL if MAIN_URL.startswith("http") else f"https://{MAIN_URL}"
RESULTS_BASE = "/results/api/v1"


# ============================================================
# SESSION
# ============================================================
def new_session() -> Smartschool:
    s = Smartschool(EnvCredentials())
    _ = s.platform_id  # forceert login
    return s


# ============================================================
# HTML HELPERS
# ============================================================
def strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    for a, b in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&#39;", "'"), ("&nbsp;", " "), ("&#8364;", "€")]:
        text = text.replace(a, b)
    return text.strip()


def clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


# ============================================================
# APP
# ============================================================
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


@app.get("/")
def index():
    return jsonify({
        "service": "smartschool-api",
        "status": "ok",
        "endpoints": [
            "GET  /api/grades?detail=0|1",
            "GET  /api/grades/evaluation/<id>",
            "GET  /api/messages?box=inbox|outbox|trash|archive",
            "GET  /api/messages/<id>?box=inbox",
            "GET  /api/messages/<id>/attachment/<file_id>",
            "POST /api/messages/<id>/action",
            "GET  /api/messages/search-users?q=...&type=0",
            "POST /api/messages/send",
            "GET  /api/profile",
            "PATCH /api/profile",
            "GET  /api/planner?detail=0|1",
            "POST /api/planner/<platform_id>/<element_id>/<action>",
            "POST /api/planner/todo",
        ],
    })


# ============================================================
# /api/grades
# ============================================================
def _rget(session, path):
    r = session.request(
        "GET", path,
        headers={"Accept": "*/*", "Content-Type": "application/json",
                 "Referer": BASE_URL + "/"},
    )
    return r.json()


def _fetch_all_evals(session):
    items, page = [], 1
    while True:
        batch = _rget(session, f"{RESULTS_BASE}/evaluations/?pageNumber={page}&itemsOnPage=200")
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return items


def _fetch_eval_detail(session, identifier):
    try:
        return _rget(session, f"{RESULTS_BASE}/evaluations/{identifier}/")
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


@app.get("/api/grades")
def grades():
    with_detail = request.args.get("detail", "0") == "1"
    session = new_session()

    courses = _rget(session, f"{RESULTS_BASE}/courses/")
    evals = _fetch_all_evals(session)
    course_names = {c["id"]: c["name"] for c in courses if c.get("name") != "Totaal"}

    per_course = defaultdict(list)
    for e in evals:
        for cv in e.get("courses", []):
            per_course[cv["id"]].append(e)

    result = {"total_evaluations": len(evals),
              "total_courses": len(course_names),
              "courses": []}

    for cid, items in sorted(per_course.items(),
                             key=lambda x: course_names.get(x[0], "")):
        entry = {"course_id": cid,
                 "course_name": course_names.get(cid, f"Vak {cid}"),
                 "evaluations": []}
        for e in sorted(items, key=lambda x: x.get("date", ""), reverse=True):
            full = _fetch_eval_detail(session, e["identifier"]) if with_detail else None
            entry["evaluations"].append(_eval_summary(e, full))
        result["courses"].append(entry)

    return jsonify(result)


@app.get("/api/grades/evaluation/<identifier>")
def grade_detail(identifier):
    session = new_session()
    full = _fetch_eval_detail(session, identifier)
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


# ============================================================
# /api/messages
# ============================================================
DROPPED_TYPE = {
    (0, False): 0, (1, False): 2, (2, False): 3,
    (0, True):  1, (1, True):  4, (2, True):  5,
}


def _build_command_xml(commands):
    lines = ["<request>"]
    for cmd in commands:
        lines.append("\t<command>")
        lines.append(f"\t\t<subsystem>{cmd['subsystem']}</subsystem>")
        lines.append(f"\t\t<action>{cmd['action']}</action>")
        lines.append("\t\t<params>")
        for name, value in cmd.get("params", {}).items():
            lines.append(f'\t\t\t<param name="{name}"><![CDATA[{value}]]></param>')
        lines.append("\t\t</params>")
        lines.append("\t</command>")
    lines.append("</request>")
    return "\n".join(lines)


def _dispatch(session, commands):
    xml = _build_command_xml(commands)
    encoded = urllib.parse.quote(xml, safe="")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/",
    }
    return session.request("POST", "/?module=Messages&file=dispatcher",
                           data=f"command={encoded}", headers=headers)


def _extract_tag(block, tag, default=""):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.DOTALL)
    return m.group(1).strip() if m else default


def _get_message_list(session, box_type="inbox", box_id="0"):
    r = _dispatch(session, [{
        "subsystem": "postboxes", "action": "message list",
        "params": {"boxType": box_type, "boxID": box_id,
                   "sortField": "date", "sortKey": "desc",
                   "poll": "false", "poll_ids": "", "layout": "new"},
    }])
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return []
    out = []
    for msg in root.findall(".//message"):
        out.append({
            "id": msg.findtext("id", ""),
            "from": msg.findtext("from", ""),
            "subject": msg.findtext("subject", ""),
            "date": msg.findtext("date", ""),
            "attachment": msg.findtext("attachment", "") == "1",
            "unread": msg.findtext("unread", "") == "1",
            "real_box": msg.findtext("realBox", ""),
        })
    return out


def _get_message_full_xml(session, msg_id, box_type="inbox"):
    r = _dispatch(session, [
        {"subsystem": "postboxes", "action": "show message",
         "params": {"msgID": msg_id, "boxType": box_type, "limitList": "true"}},
        {"subsystem": "postboxes", "action": "attachment list",
         "params": {"msgID": msg_id, "boxType": box_type, "limitList": "true"}},
    ])
    return r.text


def _parse_full_message(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return {"error": f"XML parse error: {e}"}

    result = {"id": "", "subject": "", "sender": "", "to": "", "cc": "",
              "date": "", "body_html": "", "body_text": "", "attachments": []}
    msg = root.find(".//message")
    if msg is not None:
        result["id"] = msg.findtext("id", "")
        result["subject"] = msg.findtext("subject", "")
        result["date"] = msg.findtext("date", "")
        result["sender"] = msg.findtext("from", "")
        result["to"] = msg.findtext("to", "")
        result["cc"] = msg.findtext("cc", "")
        for tag in ["body", "messageBody", "content", "htmlBody"]:
            b = msg.find(tag)
            if b is not None:
                if b.text:
                    result["body_html"] = b.text
                elif len(b) > 0:
                    result["body_html"] = ET.tostring(b, encoding="unicode")
                break

    if not result["body_html"]:
        m = re.search(r"<body[^>]*>(.*?)</body>", xml_text, re.DOTALL | re.IGNORECASE)
        if m:
            result["body_html"] = m.group(1)

    result["body_text"] = strip_html(result["body_html"])

    for att in root.findall(".//attachment"):
        fid = (att.findtext("fileID", "") or "").strip()
        name = (att.findtext("name", "") or "").strip()
        if not fid or not name:
            continue
        result["attachments"].append({
            "file_id": fid, "name": name,
            "mime": att.findtext("mime", ""),
            "size": att.findtext("size", ""),
        })
    return result


@app.get("/api/messages")
def messages_list():
    box = request.args.get("box", "inbox")
    session = new_session()
    return jsonify({"box": box, "messages": _get_message_list(session, box)})


@app.get("/api/messages/<msg_id>")
def message_read(msg_id):
    box = request.args.get("box", "inbox")
    session = new_session()
    xml_text = _get_message_full_xml(session, msg_id, box)
    return jsonify(_parse_full_message(xml_text))


@app.get("/api/messages/<msg_id>/attachment/<file_id>")
def message_attachment(msg_id, file_id):
    session = new_session()
    url = f"/?module=Messages&file=download&fileID={file_id}&target=0"
    r = session.request("GET", url,
                        headers={"Referer": BASE_URL + "/"},
                        allow_redirects=True)
    if r.status_code != 200:
        return jsonify({"error": f"status {r.status_code}"}), 502
    return send_file(BytesIO(r.content), as_attachment=True,
                     download_name=f"{file_id}.bin",
                     mimetype="application/octet-stream")


@app.post("/api/messages/<msg_id>/action")
def message_action(msg_id):
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action")
    session = new_session()
    try:
        if action == "unread":
            list(MarkMessageUnread(session, msg_id=int(msg_id)))
        elif action == "archive":
            list(MessageMoveToArchive(session, msg_id=int(msg_id)))
        elif action == "trash":
            list(MessageMoveToTrash(session, msg_id=int(msg_id)))
        elif action == "label":
            label_map = {"red": MessageLabel.RED_FLAG,
                         "green": MessageLabel.GREEN_FLAG,
                         "yellow": MessageLabel.YELLOW_FLAG,
                         "blue": MessageLabel.BLUE_FLAG,
                         "none": MessageLabel.NO_FLAG}
            label = label_map.get(data.get("label", "none"))
            if not label:
                return jsonify({"error": "onbekend label"}), 400
            list(AdjustMessageLabel(session, msg_id=int(msg_id), label=label))
        else:
            return jsonify({"error": "onbekende actie"}), 400
        return jsonify({"ok": True, "action": action})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


# --- verzenden ---
def _get_compose_tokens(session):
    r = session.request(
        "GET",
        "/?module=Messages&file=composeMessage&boxType=inbox&composeType=0&msgID=undefined",
    )
    fields = {}
    for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"[^>]*>', r.text):
        fields[m.group(1)] = m.group(2)
    return {
        "random_dir": fields.get("randomDir", ""),
        "unique_usc": fields.get("uniqueUsc", ""),
        "encrypted_sender": fields.get("encryptedSender", ""),
    }


def _search_users(session, query, unique_usc, search_type=0):
    parent = f"insertSearchFieldContainer_{search_type}_0"
    r = session.request(
        "POST", "/?module=Messages&file=searchUsers",
        data={"val": query, "type": str(search_type),
              "parentNodeId": parent,
              "xml": "<results></results>",
              "uniqueUsc": unique_usc},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest",
                 "Origin": BASE_URL, "Referer": BASE_URL + "/"},
    )
    users = []
    for um in re.finditer(r"<user>(.*?)</user>", r.text, re.DOTALL):
        block = um.group(1)
        uid = _extract_tag(block, "userID")
        if not uid:
            continue
        name = clean_html(_extract_tag(block, "value"))
        co = clean_html(_extract_tag(block, "coaccountname"))
        users.append({
            "user_id": uid, "name": name,
            "coaccount_name": co, "is_co_account": bool(co),
            "ss_id": _extract_tag(block, "ssID", "455"),
        })
    return users


def _add_user_to_selected(session, user_id, unique_usc, dropped_type,
                          ssid="455", userlt="0"):
    parent = f"insertSearchFieldContainer_{dropped_type}_0"
    return session.request(
        "POST", "/?module=Messages&file=searchUsers&function=addUserToSelected",
        data={"id": user_id, "typeId": "users", "type": str(dropped_type),
              "parentNodeId": parent, "ssid": ssid, "userlt": userlt,
              "uniqueUsc": unique_usc},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest",
                 "Origin": BASE_URL, "Referer": BASE_URL + "/"},
    ).text


def _build_receiver_xml(users, receiver_type):
    xml = "<results>"
    for u in users:
        uid = u["user_id"]
        if u.get("is_co_account"):
            if not uid.startswith("U"):
                uid = f"U{uid}"
            userlt = "2"
        else:
            if uid.startswith("U"):
                uid = uid[1:]
            userlt = "0"
        xml += (f"<result><id>{uid}</id>"
                f"<ssid>{u.get('ss_id', '455')}</ssid>"
                f"<type>{receiver_type}</type>"
                f"<userlt>{userlt}</userlt></result>")
    xml += "</results>"
    return xml


@app.get("/api/messages/search-users")
def messages_search_users():
    q = request.args.get("q", "").strip()
    st = int(request.args.get("type", 0))
    if not q:
        return jsonify({"error": "q verplicht"}), 400
    session = new_session()
    tokens = _get_compose_tokens(session)
    return jsonify({"query": q, "users": _search_users(session, q, tokens["unique_usc"], st)})


@app.post("/api/messages/send")
def messages_send():
    data = request.get_json(force=True, silent=True) or {}
    subject = data.get("subject")
    message = data.get("message")
    to_users = data.get("to") or []
    cc_users = data.get("cc") or []
    bcc_users = data.get("bcc") or []
    send_date = data.get("send_date") or ""

    if not subject or not message or not to_users:
        return jsonify({"error": "subject, message en to zijn verplicht"}), 400

    session = new_session()
    tokens = _get_compose_tokens(session)
    if not tokens["random_dir"]:
        return jsonify({"error": "kon tokens niet ophalen"}), 500

    for role_idx, user_list in [(0, to_users), (1, cc_users), (2, bcc_users)]:
        for u in user_list:
            is_co = u.get("is_co_account", False)
            dropped = DROPPED_TYPE[(role_idx, is_co)]
            userlt = "2" if is_co else "0"
            _add_user_to_selected(session, u["user_id"], tokens["unique_usc"],
                                  dropped, ssid=u.get("ss_id", "455"),
                                  userlt=userlt)

    fields = {
        "send": "send", "origMsgID": "0", "composeAction": "0",
        "randomDir": tokens["random_dir"], "uniqueUsc": tokens["unique_usc"],
        "showTab": "tab1Container", "delFile": "0", "composeType": "0",
        "msgID": "0", "msgFormSelectedTab": "", "sendDate": send_date,
        "subject": subject, "bcc": "0",
        "message": f"<p>{message}</p>",
        "encryptedSender": tokens["encrypted_sender"],
    }
    if to_users:
        fields["receiverPart0"] = _build_receiver_xml(to_users, 0)
    if cc_users:
        fields["receiverPart1"] = _build_receiver_xml(cc_users, 1)
    if bcc_users:
        fields["receiverPart2"] = _build_receiver_xml(bcc_users, 2)

    m = MultipartEncoder(fields=fields)
    r = session.request(
        "POST",
        "/?module=Messages&file=composeMessage&boxType=inbox&composeType=0&msgID=undefined",
        data=m,
        headers={"Content-Type": m.content_type, "Origin": BASE_URL,
                 "Referer": BASE_URL + "/"},
        allow_redirects=True,
    )
    return jsonify({"ok": r.status_code == 200,
                    "status": r.status_code,
                    "scheduled": bool(send_date)})


# ============================================================
# /api/profile
# ============================================================
PROFILE_PATH = "/?module=Profile&file=personalia&function=personalia"


def _parse_profile_inputs(html):
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
    return _parse_profile_inputs(r.text)


@app.get("/api/profile")
def profile_read():
    session = new_session()
    fields = _get_profile(session)
    return jsonify({
        "fields": {k: {"value": v["value"], "readonly": v["readonly"], "type": v["type"]}
                   for k, v in fields.items() if not k.startswith("_")}
    })


@app.patch("/api/profile")
def profile_update():
    data = request.get_json(force=True, silent=True) or {}
    changes = data.get("changes") or {}
    if not changes:
        return jsonify({"error": "changes verplicht"}), 400

    session = new_session()
    fields = _get_profile(session)

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

    r = session.request(
        "POST", PROFILE_PATH, data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Origin": BASE_URL, "Referer": BASE_URL + PROFILE_PATH},
        allow_redirects=True,
    )

    new_fields = _get_profile(session)
    applied = {k: v for k, v in changes.items()
               if new_fields.get(k, {}).get("value") == v}

    return jsonify({
        "status": r.status_code,
        "applied": applied,
        "skipped_readonly": skipped,
        "ok": len(applied) == len([k for k in changes if k not in skipped]),
    })


# ============================================================
# /api/planner
# ============================================================
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


@app.get("/api/planner")
def planner_list():
    with_detail = request.args.get("detail", "0") == "1"
    session = new_session()
    elements = list(PlannedElements(session))
    out = []
    for el in elements:
        d = _element_dict(el)
        if with_detail:
            detail = _planner_detail(session, el) or {}
            d["info"] = strip_html(detail.get("publicInfo") or "")
            d["attachments"] = [{"name": a.get("name")}
                                for a in (detail.get("attachments") or [])]
            d["weblinks"] = [l.get("url", l) for l in (detail.get("weblinks") or [])]
        out.append(d)
    return jsonify({"count": len(out), "items": out})


@app.post("/api/planner/<platform_id>/<element_id>/<action>")
def planner_item_action(platform_id, element_id, action):
    if action not in ("resolve", "unresolve", "trash"):
        return jsonify({"error": "ongeldige actie"}), 400
    session = new_session()
    elements = list(PlannedElements(session))
    target = None
    for el in elements:
        if str(el.platform_id) == str(platform_id) and str(el.id) == str(element_id):
            target = el
            break
    if not target:
        return jsonify({"error": "element niet gevonden"}), 404
    ok = _planner_action(session, target, action)
    return jsonify({"ok": ok, "action": action})


@app.post("/api/planner/todo")
def planner_create_todo():
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
               else datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
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
    session = new_session()
    try:
        r = session.request("POST", "/planner/api/v1/planned-to-dos/", json=body)
        return jsonify({"ok": r.status_code in (200, 201),
                        "status": r.status_code,
                        "response": r.text[:500]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# ERROR HANDLER
# ============================================================
@app.errorhandler(Exception)
def on_error(e):
    return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
