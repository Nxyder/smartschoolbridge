"""
Berichten (messages) endpoints — inclusief uploads en drafts.
"""

import re
import secrets
import time
import urllib.parse
import xml.etree.ElementTree as ET
from io import BytesIO

import requests as _requests
from flask import (
    Blueprint, jsonify, request, send_file, send_from_directory,
)
from requests_toolbelt.multipart.encoder import MultipartEncoder
from smartschool import (
    MarkMessageUnread, MessageMoveToArchive, MessageMoveToTrash,
    AdjustMessageLabel, MessageLabel,
)

from modules.common import (
    with_session, base_url, clean_html, strip_html,
    UPLOAD_DIR, UPLOAD_TTL,
)


bp = Blueprint("messages", __name__, url_prefix="/api/messages")


DROPPED_TYPE = {
    (0, False): 0, (1, False): 2, (2, False): 3,
    (0, True):  1, (1, True):  4, (2, True):  5,
}


# ============================================================
# DISPATCHER
# ============================================================
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


def _dispatch(session, base, commands):
    xml = _build_command_xml(commands)
    encoded = urllib.parse.quote(xml, safe="")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Origin": base,
        "Referer": base + "/",
    }
    return session.request("POST", "/?module=Messages&file=dispatcher",
                           data=f"command={encoded}", headers=headers)


def _extract_tag(block, tag, default=""):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.DOTALL)
    return m.group(1).strip() if m else default


# ============================================================
# BERICHTEN LEZEN
# ============================================================
def _get_message_list(session, base, box_type="inbox", box_id="0"):
    r = _dispatch(session, base, [{
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
        mid = msg.findtext("id", "")
        if not mid:
            continue
        out.append({
            "id": mid,
            "from": msg.findtext("from", ""),
            "subject": msg.findtext("subject", ""),
            "date": msg.findtext("date", ""),
            "attachment": msg.findtext("attachment", "") == "1",
            # LET OP: Smartschool's unread veld is in deze instantie OMGEKEERD.
            "unread": msg.findtext("unread", "") == "0",
            "real_box": msg.findtext("realBox", "") or box_type,
            "is_draft": (msg.findtext("realBox", "") == "draft") or (box_type == "draft"),
        })
    return out


def _get_message_full_xml(session, base, msg_id, box_type="inbox"):
    r = _dispatch(session, base, [
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

    result = {"id": "", "subject": "", "sender": "", "to": "", "cc": "", "bcc": "",
              "date": "", "body_html": "", "body_text": "",
              "attachments": [], "inline_images": []}
    msg = root.find(".//message")
    if msg is not None:
        result["id"] = msg.findtext("id", "")
        result["subject"] = msg.findtext("subject", "")
        result["date"] = msg.findtext("date", "")
        result["sender"] = msg.findtext("from", "")
        result["to"] = msg.findtext("to", "")
        result["cc"] = msg.findtext("cc", "")
        result["bcc"] = msg.findtext("bcc", "")
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

    for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']',
                         result["body_html"], re.IGNORECASE):
        result["inline_images"].append({"src": m.group(1)})

    return result


@bp.get("")
@with_session
def messages_list(_session=None, _creds=None):
    box = request.args.get("box", "inbox")
    return jsonify({
        "box": box,
        "messages": _get_message_list(_session, base_url(_creds), box),
    })


@bp.get("/<msg_id>")
@with_session
def message_read(msg_id, _session=None, _creds=None):
    box = request.args.get("box", "inbox")
    xml_text = _get_message_full_xml(_session, base_url(_creds), msg_id, box)
    return jsonify(_parse_full_message(xml_text))


@bp.post("/<msg_id>/read")
@with_session
def message_mark_read(msg_id, _session=None, _creds=None):
    """Markeer een bericht als gelezen."""
    base = base_url(_creds)
    box = request.args.get("box", "inbox")
    try:
        r = _dispatch(_session, base, [{
            "subsystem": "postboxes",
            "action": "mark message read",
            "params": {"msgID": msg_id, "boxType": box, "limitList": "true"},
        }])
        body = r.text
        ok = "<status>ok</status>" in body
        return jsonify({"ok": ok, "status": r.status_code, "response": body[:300]})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@bp.post("/mark-all-read")
@with_session
def messages_mark_all_read(_session=None, _creds=None):
    """Markeer alle berichten in een box als gelezen."""
    box = request.args.get("box", "inbox")
    base = base_url(_creds)

    r = _dispatch(_session, base, [{
        "subsystem": "postboxes", "action": "message list",
        "params": {"boxType": box, "boxID": "0",
                   "sortField": "date", "sortKey": "desc",
                   "poll": "false", "poll_ids": "", "layout": "new"},
    }])
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return jsonify({"error": "kon berichtenlijst niet parsen"}), 500

    msg_ids = []
    for msg in root.findall(".//message"):
        mid = msg.findtext("id", "")
        if mid and msg.findtext("unread", "") == "0":  # omgekeerd!
            msg_ids.append(mid)

    if not msg_ids:
        return jsonify({"ok": True, "marked": 0})

    commands = [{
        "subsystem": "postboxes",
        "action": "mark message read",
        "params": {"msgID": mid, "boxType": box, "limitList": "true"},
    } for mid in msg_ids]

    r2 = _dispatch(_session, base, commands)
    body = r2.text

    return jsonify({
        "ok": True,
        "marked": len(msg_ids),
        "ids": msg_ids,
        "response_preview": body[:300],
    })


@bp.get("/<msg_id>/attachment/<file_id>")
@with_session
def message_attachment(msg_id, file_id, _session=None, _creds=None):
    base = base_url(_creds)
    url = f"/?module=Messages&file=download&fileID={file_id}&target=0"
    r = _session.request("GET", url,
                         headers={"Referer": base + "/"},
                         allow_redirects=True)
    if r.status_code != 200:
        return jsonify({"error": f"status {r.status_code}"}), 502
    return send_file(BytesIO(r.content), as_attachment=True,
                     download_name=f"{file_id}.bin",
                     mimetype="application/octet-stream")


@bp.post("/<msg_id>/action")
@with_session
def message_action(msg_id, _session=None, _creds=None):
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action")
    try:
        if action == "unread":
            list(MarkMessageUnread(_session, msg_id=int(msg_id)))
        elif action == "archive":
            list(MessageMoveToArchive(_session, msg_id=int(msg_id)))
        elif action == "trash":
            list(MessageMoveToTrash(_session, msg_id=int(msg_id)))
        elif action == "label":
            label_map = {"red": MessageLabel.RED_FLAG,
                         "green": MessageLabel.GREEN_FLAG,
                         "yellow": MessageLabel.YELLOW_FLAG,
                         "blue": MessageLabel.BLUE_FLAG,
                         "none": MessageLabel.NO_FLAG}
            label = label_map.get(data.get("label", "none"))
            if not label:
                return jsonify({"error": "onbekend label"}), 400
            list(AdjustMessageLabel(_session, msg_id=int(msg_id), label=label))
        else:
            return jsonify({"error": "onbekende actie"}), 400
        return jsonify({"ok": True, "action": action})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


# ============================================================
# DRAFTS
# ============================================================
def _get_draft_tokens(session, base, draft_id):
    url = (f"/?module=Messages&file=composeMessage"
           f"&boxType=draft&composeType=5&msgID={draft_id}")
    r = session.request("GET", url)
    html = r.text

    fields = {}
    for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"[^>]*>', html):
        fields[m.group(1)] = m.group(2)

    body = ""
    m = re.search(r'<textarea[^>]*name="message"[^>]*>(.*?)</textarea>',
                  html, re.DOTALL | re.IGNORECASE)
    if m:
        body = m.group(1)

    if body:
        body = (body.replace("&lt;", "<").replace("&gt;", ">")
                    .replace("&quot;", '"').replace("&#39;", "'")
                    .replace("&amp;", "&").replace("&nbsp;", " "))

    receivers = {"to": [], "cc": [], "bcc": []}
    pattern = re.compile(
        r'<div[^>]*class="[^"]*receiverSpan[^"]*"[^>]*>'
        r'(.*?)'
        r'(?=<div[^>]*class="[^"]*receiverSpan|$)',
        re.DOTALL | re.IGNORECASE
    )

    for rm in pattern.finditer(html):
        full_match = rm.group(0)
        open_tag_end = full_match.find(">")
        open_tag = full_match[:open_tag_end + 1]

        real_id_m = re.search(r'\brealuserid="(\d+)"', open_tag)
        idatt_m = re.search(r'\bidatt="([^"]*)"', open_tag)
        userlt_m = re.search(r'\buserltatt="(\d+)"', open_tag)
        typeatt_m = re.search(r'\btypeatt="(\d+)"', open_tag)
        ssid_m = re.search(r'\bssidatt="(\d+)"', open_tag)

        if not real_id_m:
            continue

        name = ""
        name_match = re.search(
            r"""<div[^>]*class=["']receiverSpanName[^"']*["'][^>]*>(.*?)</div>""",
            full_match, re.DOTALL | re.IGNORECASE
        )
        if name_match:
            name = name_match.group(1)
            name = re.sub(r"<[^>]+>", "", name).strip()

        real_id = real_id_m.group(1)
        idatt = idatt_m.group(1) if idatt_m else ""
        userlt = userlt_m.group(1) if userlt_m else "0"
        typeatt = typeatt_m.group(1) if typeatt_m else "0"
        ssid = ssid_m.group(1) if ssid_m else "455"

        is_co = (userlt == "2") or typeatt in ("1", "4", "5")

        if typeatt in ("0", "1"):
            role = "to"
        elif typeatt in ("2", "4"):
            role = "cc"
        elif typeatt in ("3", "5"):
            role = "bcc"
        else:
            role = "to"

        receivers[role].append({
            "user_id": real_id,
            "idatt": idatt,
            "name": name,
            "display_name": name,
            "is_co_account": is_co,
            "user_lt": userlt,
            "ss_id": ssid,
            "typeatt": typeatt,
            "type_label": "ouder" if is_co else "leerling",
        })

    return {
        "draft_id": draft_id,
        "orig_msg_id": fields.get("origMsgID", draft_id),
        "subject": fields.get("subject", ""),
        "body": body.strip(),
        "random_dir": fields.get("randomDir", ""),
        "unique_usc": fields.get("uniqueUsc", ""),
        "encrypted_sender": fields.get("encryptedSender", ""),
        "receivers": receivers,
    }


@bp.get("/draft/<draft_id>")
@with_session
def get_draft(draft_id, _session=None, _creds=None):
    base = base_url(_creds)
    try:
        data = _get_draft_tokens(_session, base, draft_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


# ============================================================
# GEBRUIKERS ZOEKEN
# ============================================================
def _get_compose_tokens(session, base):
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


def _search_users_single(session, base, query, unique_usc, search_type):
    parent = f"insertSearchFieldContainer_{search_type}_0"
    r = session.request(
        "POST", "/?module=Messages&file=searchUsers",
        data={"val": query, "type": str(search_type),
              "parentNodeId": parent,
              "xml": "<results></results>",
              "uniqueUsc": unique_usc},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest",
                 "Origin": base, "Referer": base + "/"},
    )

    users = []
    for um in re.finditer(r"<user>(.*?)</user>", r.text, re.DOTALL):
        block = um.group(1)
        uid = _extract_tag(block, "userID")
        if not uid:
            continue

        name = clean_html(_extract_tag(block, "value"))
        co_name = clean_html(_extract_tag(block, "coaccountname"))
        user_type = _extract_tag(block, "userType", "U")
        user_lt = _extract_tag(block, "userLT", "0")
        selectable = _extract_tag(block, "selectable", "on")
        ss_id = _extract_tag(block, "ssID", "455")
        classname = _extract_tag(block, "classname", "")

        is_co = (user_lt == "2") or bool(co_name)

        if is_co and co_name:
            display_name = co_name
           
