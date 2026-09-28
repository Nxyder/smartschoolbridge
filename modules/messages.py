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
            if name and name.lower() != co_name.lower():
                display_name = f"{co_name} (via {name})"
        else:
            display_name = name

        full_name = f"Co: {display_name}" if is_co else display_name

        if is_co:
            type_label = "ouder"
        elif user_type == "U":
            type_label = f"leerling - {classname}" if classname else "leerling"
        elif user_type == "T":
            type_label = "leerkracht"
        elif user_type == "G":
            type_label = "groep"
        else:
            type_label = user_type

        users.append({
            "user_id": uid,
            "name": full_name,
            "display_name": display_name,
            "raw_name": name,
            "coaccount_name": co_name,
            "is_co_account": is_co,
            "user_type": user_type,
            "user_lt": user_lt,
            "selectable": selectable,
            "ss_id": ss_id,
            "classname": classname,
            "type_label": type_label,
            "found_via_type": search_type,
        })
    return users


def _search_users(session, base, query, unique_usc, search_type=None):
    if search_type is None or search_type == "all":
        all_users = []
        seen = set()
        for st in [0, 1, 2, 3, 4, 5]:
            try:
                for u in _search_users_single(session, base, query, unique_usc, st):
                    key = (u["user_id"], u["is_co_account"])
                    if key in seen:
                        continue
                    seen.add(key)
                    all_users.append(u)
            except Exception:
                continue
        return all_users
    else:
        return _search_users_single(session, base, query, unique_usc, int(search_type))


def _add_user_to_selected(session, base, user_id, unique_usc, dropped_type,
                          ssid="455", userlt="0", type_id="users"):
    parent = f"insertSearchFieldContainer_{dropped_type}_0"
    return session.request(
        "POST", "/?module=Messages&file=searchUsers&function=addUserToSelected",
        data={"id": user_id, "typeId": type_id, "type": str(dropped_type),
              "parentNodeId": parent, "ssid": ssid, "userlt": userlt,
              "uniqueUsc": unique_usc},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest",
                 "Origin": base, "Referer": base + "/"},
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


@bp.get("/search-users")
@with_session
def messages_search_users(_session=None, _creds=None):
    q = request.args.get("q", "").strip()
    st_raw = request.args.get("type", "all")
    if not q:
        return jsonify({"error": "q verplicht"}), 400

    st = "all" if st_raw == "all" else int(st_raw)
    base = base_url(_creds)
    tokens = _get_compose_tokens(_session, base)
    users = _search_users(_session, base, q, tokens["unique_usc"], st)
    return jsonify({"query": q, "type": st_raw, "users": users})


# ============================================================
# VERZENDEN
# ============================================================
@bp.post("/send")
@with_session
def messages_send(_session=None, _creds=None):
    data = request.get_json(force=True, silent=True) or {}
    subject = data.get("subject")
    message_html = data.get("message_html")
    message_plain = data.get("message") or ""
    to_users = data.get("to") or []
    cc_users = data.get("cc") or []
    bcc_users = data.get("bcc") or []
    attachments = data.get("attachments") or []
    send_date = data.get("send_date") or ""
    draft_id = data.get("draft_id") or ""

    if not subject or not to_users:
        return jsonify({"error": "subject en to zijn verplicht"}), 400

    if message_html and message_html.strip():
        body = message_html
    elif message_plain:
        body = f"<p>{message_plain}</p>"
    else:
        body = "<p></p>"

    if attachments:
        body += '<hr><p><b>📎 Bijlagen:</b></p><ul>'
        for att in attachments:
            url = att.get("url")
            fn = att.get("filename", "bestand")
            if url:
                body += f'<li><a href="{url}" target="_blank">{fn}</a></li>'
        body += '</ul>'

    base = base_url(_creds)
    if draft_id:
        tokens = _get_draft_tokens(_session, base, draft_id)
    else:
        tokens = _get_compose_tokens(_session, base)

    if not tokens.get("random_dir"):
        return jsonify({"error": "kon tokens niet ophalen"}), 500

    for role_idx, user_list in [(0, to_users), (1, cc_users), (2, bcc_users)]:
        for u in user_list:
            is_co = u.get("is_co_account", False)
            dropped = DROPPED_TYPE[(role_idx, is_co)]
            userlt = "2" if is_co else "0"
            _add_user_to_selected(_session, base, u["user_id"], tokens["unique_usc"],
                                  dropped, ssid=u.get("ss_id", "455"),
                                  userlt=userlt)

    fields = {
        "send": "send",
        "origMsgID": tokens.get("orig_msg_id", "0") if draft_id else "0",
        "composeAction": "5" if draft_id else "0",
        "randomDir": tokens["random_dir"],
        "uniqueUsc": tokens["unique_usc"],
        "showTab": "tab1Container",
        "delFile": "0",
        "composeType": "5" if draft_id else "0",
        "msgID": draft_id if draft_id else "0",
        "msgFormSelectedTab": "",
        "sendDate": send_date,
        "subject": subject,
        "bcc": "1" if bcc_users else "0",
        "message": body,
        "encryptedSender": tokens["encrypted_sender"],
    }
    if to_users:
        fields["receiverPart0"] = _build_receiver_xml(to_users, 0)
    if cc_users:
        fields["receiverPart1"] = _build_receiver_xml(cc_users, 1)
    if bcc_users:
        fields["receiverPart2"] = _build_receiver_xml(bcc_users, 2)

    m = MultipartEncoder(fields=fields)
    r = _session.request(
        "POST",
        "/?module=Messages&file=composeMessage&boxType=inbox&composeType=0&msgID=undefined",
        data=m,
        headers={"Content-Type": m.content_type, "Origin": base,
                 "Referer": base + "/"},
        allow_redirects=True,
    )
    return jsonify({
        "ok": r.status_code == 200,
        "status": r.status_code,
        "scheduled": bool(send_date),
        "draft_id": draft_id or None,
        "has_bcc": bool(bcc_users),
        "to_count": len(to_users),
        "cc_count": len(cc_users),
        "bcc_count": len(bcc_users),
        "body_length": len(body),
    })


# ============================================================
# UPLOADS
# ============================================================
def _cleanup_old_uploads():
    try:
        now = time.time()
        for f in UPLOAD_DIR.iterdir():
            if f.is_file() and (now - f.stat().st_mtime) > UPLOAD_TTL:
                try:
                    f.unlink()
                except Exception:
                    pass
    except Exception:
        pass


def _save_local_upload(filename, content):
    _cleanup_old_uploads()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    token = secrets.token_hex(8)
    stored_name = f"{token}_{safe}"

    filepath = UPLOAD_DIR / stored_name
    with open(filepath, "wb") as f:
        f.write(content)

    try:
        host = request.host_url.rstrip("/")
    except Exception:
        host = ""
    return {
        "ok": True,
        "url": f"{host}/files/{stored_name}",
        "path": f"/files/{stored_name}",
        "filename": filename,
        "stored_name": stored_name,
        "size": len(content),
        "storage": "local",
        "ttl_hours": 24,
    }


@bp.post("/upload")
@with_session
def upload_file(_session=None, _creds=None):
    if "file" not in request.files:
        return jsonify({"error": "geen 'file'"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "lege filename"}), 400

    content = f.read()
    if len(content) > 200 * 1024 * 1024:
        return jsonify({"error": "bestand > 200MB"}), 400

    mime = f.mimetype or "application/octet-stream"
    is_image = mime.startswith("image/")

    try:
        r = _requests.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": (f.filename, content, mime)},
            timeout=60,
        )
        if r.status_code == 200:
            url = r.text.strip()
            if url.startswith("http"):
                return jsonify({
                    "ok": True, "url": url, "filename": f.filename,
                    "mime": mime, "size": len(content),
                    "is_image": is_image, "storage": "catbox",
                })
    except Exception as e:
        print(f"[upload] catbox exception: {type(e).__name__}: {e}")

    try:
        result = _save_local_upload(f.filename, content)
        result["mime"] = mime
        result["is_image"] = is_image
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"lokale opslag mislukt: {type(e).__name__}: {e}"}), 500


def _upload_fallback(filename, content, mime):
    try:
        r = _requests.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": (filename, content, mime)},
            timeout=60,
        )
        if r.status_code == 200:
            url = r.text.strip()
            if url.startswith("http"):
                return jsonify({
                    "ok": True, "url": url, "filename": filename,
                    "mime": mime, "size": len(content),
                    "is_image": mime.startswith("image/"),
                    "storage": "catbox-fallback",
                })
    except Exception:
        pass

    try:
        result = _save_local_upload(filename, content)
        result["mime"] = mime
        result["is_image"] = mime.startswith("image/")
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"alle uploads mislukt: {e}"}), 500


@bp.post("/upload-smartschool")
@with_session
def upload_smartschool(_session=None, _creds=None):
    if "file" not in request.files:
        return jsonify({"error": "geen 'file'"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "lege filename"}), 400

    content = f.read()
    if len(content) > 512 * 1024 * 1024:
        return jsonify({"error": "bestand > 512MB"}), 400

    prefix = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789")
                     for _ in range(10))
    unique_id = f"{prefix}_{int(time.time() * 1000)}"

    mime = f.mimetype or "application/octet-stream"
    base = base_url(_creds)

    try:
        r = _session.request(
            "POST", "/TinyMCE/Upload",
            files={"file": (f.filename, content, mime)},
            data={"unique_id": unique_id},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": base,
                "Referer": base + "/",
            },
        )

        if r.status_code not in (200, 201):
            return _upload_fallback(f.filename, content, mime)

        try:
            resp_data = r.json()
        except Exception:
            return _upload_fallback(f.filename, content, mime)

        location = resp_data.get("location")
        if not location:
            return jsonify({"error": "geen location in response",
                            "body": resp_data}), 502

        full_url = location if location.startswith("http") else base + location
        return jsonify({
            "ok": True,
            "location": location,
            "url": full_url,
            "filename": f.filename,
            "size": len(content),
            "mime": mime,
            "is_image": mime.startswith("image/"),
            "unique_id": unique_id,
            "storage": "smartschool",
        })
    except Exception:
        return _upload_fallback(f.filename, content, mime)


@bp.post("/upload-image")
@with_session
def upload_image(_session=None, _creds=None):
    return upload_file(_session=_session, _creds=_creds)


@bp.post("/upload-file")
@with_session
def upload_file_alias(_session=None, _creds=None):
    return upload_file(_session=_session, _creds=_creds)
