"""
Berichten (messages) endpoints — inclusief uploads, drafts, scheduled verzending
en beheer van geplande berichten.
"""

import calendar
import json
import re
import secrets
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
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

SCHEDULED_BOX = "scheduled"
DRAFT_BOX = "draft"

# Max bytes die we van een enkele searchUsers-response accepteren.
# Alles groter = weigeren, om OOM op de gratis Render tier te vermijden.
MAX_SEARCH_BYTES = 2 * 1024 * 1024  # 2 MB per query

# Standaard request timeout in seconden.
REQUEST_TIMEOUT = 15


# ============================================================
# TIJDFORMAAT — ISO met tijdzone (Brussel)
# ============================================================
def _is_dst(dt_naive):
    year = dt_naive.year
    mar = calendar.monthcalendar(year, 3)
    last_mar_sun = max(w[6] for w in mar if w[6] != 0)
    dst_start = datetime(year, 3, last_mar_sun, 2, 0)
    oct_cal = calendar.monthcalendar(year, 10)
    last_oct_sun = max(w[6] for w in oct_cal if w[6] != 0)
    dst_end = datetime(year, 10, last_oct_sun, 3, 0)
    return dst_start <= dt_naive < dst_end


def _format_send_date(dt):
    if isinstance(dt, str):
        s = dt.strip()
        m = re.match(r"^(\d+)\s*([hdm])$", s, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            unit = m.group(2).lower()
            if unit == "h":
                dt = datetime.now() + timedelta(hours=n)
            elif unit == "d":
                dt = datetime.now() + timedelta(days=n)
            elif unit == "m":
                dt = datetime.now() + timedelta(minutes=n)
        else:
            if "T" in s and ("+" in s or s.endswith("Z")):
                return s
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                        "%d-%m-%Y %H:%M"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"Kan send_date '{dt}' niet parsen")

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    offset_hours = 2 if _is_dst(dt) else 1
    sign = "+" if offset_hours >= 0 else "-"
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}{sign}{abs(offset_hours):02d}:00"


def _parse_send_date_to_iso(raw):
    if not raw:
        return None
    try:
        return _format_send_date(raw)
    except ValueError:
        return None


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
                           data=f"command={encoded}", headers=headers,
                           timeout=REQUEST_TIMEOUT)


def _extract_tag(block, tag, default=""):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.DOTALL)
    return m.group(1).strip() if m else default


def _clean_search_name(name):
    if not name:
        return ""
    name = re.sub(r"\s*[-–]\s*[A-Za-z0-9]+\s*$", "", name).strip()
    return name


# ============================================================
# HELPERS — U-prefix
# ============================================================
def _strip_u_prefix(uid):
    s = str(uid or "").strip()
    if s.startswith("U") or s.startswith("u"):
        return s[1:]
    return s


# ============================================================
# RECEIVERS uit XML/HTML
# ============================================================
def _parse_recipients_from_receivers(msg, tag_name):
    items = []
    seen = set()

    if tag_name == "to":
        containers = ["receivers"]
    elif tag_name == "cc":
        containers = ["ccreceivers", "ccReceivers"]
    elif tag_name == "bcc":
        containers = ["bccreceivers", "bccReceivers"]
    else:
        containers = []

    for cname in containers:
        container = msg.find(cname)
        if container is None:
            continue

        found_any = False
        for recip_tag in (tag_name, "to"):
            for recip in container.findall(recip_tag):
                txt = (recip.text or "").strip()
                if not txt:
                    continue
                found_any = True
                m = re.match(r"(.+?)\s*<\s*(U?\d+)\s*>", txt)
                if m:
                    name = m.group(1).strip()
                    uid = m.group(2)
                else:
                    name = txt
                    uid = ""
                key = (name, uid)
                if key in seen:
                    continue
                seen.add(key)
                items.append({"user_id": uid, "name": name})

        if not found_any:
            txt = (container.text or "").strip()
            if txt:
                for part in re.split(r"[,;]", txt):
                    part = part.strip()
                    if not part:
                        continue
                    m = re.match(r"(.+?)\s*<\s*(U?\d+)\s*>", part)
                    if m:
                        name = m.group(1).strip()
                        uid = m.group(2)
                    else:
                        name = part
                        uid = ""
                    key = (name, uid)
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append({"user_id": uid, "name": name})

    return items


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
            "unread": msg.findtext("unread", "") == "0",
            "real_box": msg.findtext("realBox", "") or box_type,
            "is_draft": (msg.findtext("realBox", "") == "draft") or (box_type == "draft"),
        })
    return out


def _get_message_full_xml(session, base, msg_id, box_type="inbox"):
    r = _dispatch(session, base, [
        {"subsystem": "postboxes", "action": "show message",
         "params": {"msgID": msg_id, "boxType": box_type,
                    "limitList": "false", "limitListNr": "501"}},
        {"subsystem": "postboxes", "action": "attachment list",
         "params": {"msgID": msg_id, "boxType": box_type,
                    "limitList": "false", "limitListNr": "501"}},
    ])
    return r.text


def _extract_names_from_message_xml(xml_text):
    result = {}
    if not xml_text:
        return result

    def _store(uid, name):
        uid = (uid or "").strip()
        name = (name or "").strip()
        if not uid or not name:
            return
        if name.isdigit() or re.match(r"^U?\d+$", name):
            return
        result[uid] = name
        if uid.startswith("U"):
            result[uid[1:]] = name
        else:
            result["U" + uid] = name

    for m in re.finditer(
        r'<[^>]*\bid="(U?\d+)"[^>]*\bname="([^"]+)"[^>]*>',
        xml_text, re.IGNORECASE
    ):
        _store(m.group(1), m.group(2))

    for m in re.finditer(
        r'<[^>]*\bname="([^"]+)"[^>]*\bid="(U?\d+)"[^>]*>',
        xml_text, re.IGNORECASE
    ):
        _store(m.group(2), m.group(1))

    for um in re.finditer(r'<user>(.*?)</user>', xml_text, re.DOTALL | re.IGNORECASE):
        block = um.group(1)
        uid_m = re.search(r'<id>(U?\d+)</id>', block, re.IGNORECASE)
        name_m = re.search(r'<name>(.*?)</name>', block, re.DOTALL | re.IGNORECASE)
        if uid_m and name_m:
            name = re.sub(r"<[^>]+>", "", name_m.group(1)).strip()
            name = (name.replace("&nbsp;", " ").replace("&amp;", "&")
                        .replace("&lt;", "<").replace("&gt;", ">")
                        .replace("&quot;", '"').replace("&#39;", "'"))
            _store(uid_m.group(1), name)

    for um in re.finditer(
        r'<userID>(U?\d+)</userID>\s*<[^>]*?(?:userName|displayName|fullName)>(.*?)</[^>]+>',
        xml_text, re.DOTALL | re.IGNORECASE
    ):
        name = re.sub(r"<[^>]+>", "", um.group(2)).strip()
        _store(um.group(1), name)

    for m in re.finditer(
        r'([A-Za-zÀ-ÿ][\w\s\.\-\'À-ÿ]{1,60}?)\s*<\s*(U?\d+)\s*>',
        xml_text
    ):
        _store(m.group(2), m.group(1).strip())

    return result


def _parse_full_message(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return {"error": f"XML parse error: {e}"}

    result = {"id": "", "subject": "", "sender": "", "to": "", "cc": "", "bcc": "",
              "date": "", "body_html": "", "body_text": "",
              "send_date": "",
              "attachments": [], "inline_images": [],
              "to_list": [], "cc_list": [], "bcc_list": []}

    names_map = _extract_names_from_message_xml(xml_text)

    msg = root.find(".//message")
    if msg is not None:
        result["id"] = msg.findtext("id", "")
        result["subject"] = msg.findtext("subject", "")
        result["date"] = msg.findtext("date", "")
        result["sender"] = msg.findtext("from", "")
        result["send_date"] = (msg.findtext("sendDate", "")
                               or msg.findtext("senddate", ""))

        result["to_list"]  = _parse_recipients_from_receivers(msg, "to")
        result["cc_list"]  = _parse_recipients_from_receivers(msg, "cc")
        result["bcc_list"] = _parse_recipients_from_receivers(msg, "bcc")

        def _parse_recipients_legacy(tag_name):
            items = []
            seen = set()
            for recip in msg.findall(f".//{tag_name}"):
                uid = recip.get("id") or recip.get("userID") or ""
                name = recip.get("name") or ""
                txt = (recip.text or "").strip()
                if not uid and txt:
                    m = re.match(r"(.+?)\s*<\s*(U?\d+)\s*>", txt)
                    if m:
                        name, uid = m.group(1).strip(), m.group(2)
                    elif txt.isdigit() or re.match(r"^U\d+$", txt):
                        uid = txt
                    else:
                        name = txt
                if not uid and not name:
                    continue
                if uid in seen and uid:
                    continue
                if uid:
                    seen.add(uid)
                if not name and uid:
                    name = names_map.get(uid, "")
                items.append({"user_id": uid, "name": name})

            if not items:
                raw = msg.findtext(tag_name, "") or ""
                for part in raw.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    m = re.match(r"(.+?)\s*<\s*(U?\d+)\s*>", part)
                    if m:
                        items.append({
                            "user_id": m.group(2),
                            "name": m.group(1).strip(),
                        })
                    elif part.isdigit() or re.match(r"^U\d+$", part):
                        items.append({
                            "user_id": part,
                            "name": names_map.get(part, ""),
                        })
                    else:
                        items.append({"user_id": "", "name": part})
            return items

        if not result["to_list"]:
            result["to_list"] = _parse_recipients_legacy("to")
        if not result["cc_list"]:
            result["cc_list"] = _parse_recipients_legacy("cc")
        if not result["bcc_list"]:
            result["bcc_list"] = _parse_recipients_legacy("bcc")

        result["to"] = ", ".join(
            (r["name"] or r["user_id"]) for r in result["to_list"]
        ) or msg.findtext("to", "")
        result["cc"] = ", ".join(
            (r["name"] or r["user_id"]) for r in result["cc_list"]
        ) or msg.findtext("cc", "")
        result["bcc"] = ", ".join(
            (r["name"] or r["user_id"]) for r in result["bcc_list"]
        ) or msg.findtext("bcc", "")

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


def _extract_receivers_from_html(html, role):
    if not html:
        return []

    result = []
    seen = set()

    span_re = re.compile(
        r'<div\b(?P<attrs>[^>]*?\bclass=["\']receiverSpan\b[^"\']*["\'][^>]*)>'
        r'(?P<inner>.*?)'
        r'(?=<div\b[^>]*\bclass=["\']receiverSpan\b|</div>\s*</div>|\Z)',
        re.DOTALL | re.IGNORECASE,
    )

    def get_attr(attrs, name):
        m = re.search(rf'\b{name}=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
        return m.group(1) if m else ""

    for span in span_re.finditer(html):
        attrs = span.group("attrs")
        inner = span.group("inner")

        real_id  = get_attr(attrs, "realuserid")
        idatt    = get_attr(attrs, "idatt")
        userlt   = get_attr(attrs, "userltatt") or "0"
        typeatt  = get_attr(attrs, "typeatt") or "0"
        ssid     = get_attr(attrs, "ssidatt") or "455"

        if not real_id:
            continue

        try:
            ta = int(typeatt)
        except ValueError:
            ta = 0

        if ta in (0, 1):
            this_role = 0
        elif ta in (2, 4):
            this_role = 1
        elif ta in (3, 5):
            this_role = 2
        else:
            this_role = 0

        if this_role != role:
            continue

        name = ""
        nm = re.search(
            r'<(?:div|span)[^>]*class=["\']receiverSpanName[^"\']*["\'][^>]*>(.*?)</(?:div|span)>',
            inner, re.DOTALL | re.IGNORECASE,
        )
        if nm:
            name = nm.group(1)

        name = re.sub(r"<[^>]+>", "", name)
        name = (
            name.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&#39;", "'")
        ).strip()

        is_co = (userlt == "2") or (userlt == "1")

        if is_co:
            user_id = idatt if idatt.startswith("U") else f"U{real_id}"
        else:
            user_id = real_id

        key = (user_id, userlt, name.lower())
        if key in seen:
            continue
        seen.add(key)

        result.append({
            "user_id": user_id,
            "name": name,
            "is_co_account": is_co,
            "user_lt": userlt,
            "ss_id": ssid,
        })

    return result


def _strip_receiver_meta(users):
    out = []
    for u in users or []:
        if isinstance(u, str):
            s = u.strip()
            if s:
                out.append(s)
            continue
        if not isinstance(u, dict):
            continue
        name = (u.get("name") or "").strip()
        if not name:
            name = (u.get("user_id") or "").strip()
        if name:
            out.append(name)
    return out


def _resolve_receivers_from_string(session, base, s, role):
    if not s:
        return []

    names = [n.strip() for n in re.split(r"[,;]", s) if n.strip()]
    if not names:
        return []

    try:
        tokens = _get_compose_tokens(session, base)
    except Exception:
        return []

    if not tokens.get("random_dir"):
        return []

    result = []
    for name in names:
        cleaned = _clean_search_name(name)
        try:
            users = _search_users(session, base, cleaned, tokens["unique_usc"], "all")
        except Exception:
            continue
        if not users:
            continue

        needle = cleaned.lower()
        needle_full = name.lower()

        best = None
        for u in users:
            if (u.get("display_name") or "").lower() == needle:
                best = u
                break
        if not best:
            for u in users:
                if (u.get("raw_name") or "").lower() == needle:
                    best = u
                    break
        if not best:
            for u in users:
                if needle in (u.get("display_name") or "").lower():
                    best = u
                    break
        if not best:
            for u in users:
                if needle_full in (u.get("display_name") or "").lower():
                    best = u
                    break
        if not best:
            best = users[0]

        result.append({
            "user_id": best["user_id"],
            "name": best.get("display_name") or best.get("raw_name") or cleaned,
            "is_co_account": bool(best.get("is_co_account")),
            "user_lt": str(best.get("user_lt", "2" if best.get("is_co_account") else "0")),
            "ss_id": best.get("ss_id", "455"),
        })

    return result


# ============================================================
# NORMALISATIE
# ============================================================
def _normalize_user_list(session, base, lst, role, old_list=None):
    out = []
    old_list = old_list or []

    for u in (lst or []):
        if isinstance(u, str):
            resolved = _resolve_receivers_from_string(session, base, u, role)
            out.extend(resolved)
            continue

        if not isinstance(u, dict):
            continue

        uid = (u.get("user_id") or u.get("id") or "").strip()
        name = (u.get("name") or u.get("display_name") or "").strip()
        is_co = bool(u.get("is_co_account"))

        user_lt = u.get("user_lt")
        if user_lt is None or user_lt == "":
            user_lt = "2" if is_co else "0"
        user_lt = str(user_lt)

        if not uid:
            matched = False
            if name:
                for old_u in old_list:
                    old_name = (old_u.get("name") or "").strip()
                    if old_name and old_name.lower() == name.lower():
                        out.append(dict(old_u))
                        matched = True
                        break

            if not matched and name:
                resolved = _resolve_receivers_from_string(session, base, name, role)
                if resolved:
                    out.append(resolved[0])
                    matched = True

            if not matched:
                continue
            else:
                continue

        clean_uid = _strip_u_prefix(uid)

        out.append({
            "user_id": clean_uid,
            "name": name,
            "is_co_account": is_co,
            "user_lt": user_lt,
            "ss_id": str(u.get("ss_id", "455")),
        })

    return out


# ============================================================
# COMPOSE-SESSIE
# ============================================================
def _clear_compose_recipients(session, base, unique_usc, role_idx):
    if role_idx == 0:
        field = "to"
        unique_key = f"msgcompose{unique_usc}"
    elif role_idx == 1:
        field = "tocc"
        unique_key = f"msgCCcompose{unique_usc}"
    elif role_idx == 2:
        field = "tobcc"
        unique_key = f"msgBCCcompose{unique_usc}"
    else:
        return

    try:
        _dispatch(session, base, [{
            "subsystem": "quickactions",
            "action": "clearusers",
            "params": {
                "type": field,
                "uniqueKey": unique_key,
                "field": str(role_idx),
            },
        }])
    except Exception:
        pass


def _count_compose_users(session, base, unique_usc):
    try:
        r = session.request(
            "POST",
            "/?module=Messages&file=searchUsers&function=countUsers",
            data={"uniqueUsc": unique_usc},
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": base, "Referer": base + "/"},
            timeout=REQUEST_TIMEOUT,
        )
        return json.loads(r.text)
    except Exception:
        return None


# ============================================================
# TOEVOEGEN + WACHTEN
# ============================================================
def _add_users_and_wait(session, base, tokens, to_users, cc_users, bcc_users,
                        max_wait=5.0, poll_interval=0.15, sleep_between=0.12):
    unique_usc = tokens["unique_usc"]

    debug = {
        "to_users": [],
        "added": [],
        "counts_history": [],
        "expected": {},
        "final_counts": None,
        "success": False,
        "error": None,
    }

    for u in (to_users or []):
        debug["to_users"].append({
            "user_id": u.get("user_id"),
            "name": u.get("name"),
            "is_co_account": bool(u.get("is_co_account")),
            "user_lt": str(u.get("user_lt", "2" if u.get("is_co_account") else "0")),
        })

    for role_idx, user_list in [(0, to_users or []),
                                 (1, cc_users or []),
                                 (2, bcc_users or [])]:
        for u in user_list:
            is_co = u.get("is_co_account", False)
            dropped = DROPPED_TYPE[(role_idx, is_co)]
            userlt = str(u.get("user_lt", "2" if is_co else "0")) or ("2" if is_co else "0")
            uid_raw = u.get("user_id", "")
            clean_id = _strip_u_prefix(uid_raw)
            try:
                resp = _add_user_to_selected(
                    session, base, clean_id, unique_usc, dropped,
                    ssid=u.get("ss_id", "455"), userlt=userlt,
                )
                debug["added"].append({
                    "role": role_idx,
                    "user_id_raw": uid_raw,
                    "user_id_clean": clean_id,
                    "name": u.get("name"),
                    "is_co": is_co,
                    "user_lt": userlt,
                    "dropped_type": dropped,
                    "response_len": len(resp) if resp else 0,
                    "response_snippet": (resp or "")[:200],
                })
            except Exception as e:
                debug["added"].append({
                    "role": role_idx,
                    "user_id_raw": uid_raw,
                    "user_id_clean": clean_id,
                    "name": u.get("name"),
                    "error": f"{type(e).__name__}: {e}",
                })

            if sleep_between and sleep_between > 0:
                time.sleep(sleep_between)

    expected_to = len(to_users or [])
    expected_cc = len(cc_users or [])
    expected_bcc = len(bcc_users or [])
    debug["expected"] = {"to": expected_to, "cc": expected_cc, "bcc": expected_bcc}

    deadline = time.time() + max_wait
    while time.time() < deadline:
        counts = _count_compose_users(session, base, unique_usc)
        if counts:
            try:
                got = {
                    "to": int(counts.get("amountTo", 0)),
                    "cc": int(counts.get("amountCc", 0)),
                    "bcc": int(counts.get("amountBcc", 0)),
                    "raw": counts,
                }
            except (ValueError, TypeError):
                got = {"to": 0, "cc": 0, "bcc": 0, "raw": counts}
            debug["counts_history"].append(got)
            debug["final_counts"] = got

            if (got["to"] >= expected_to and
                got["cc"] >= expected_cc and
                got["bcc"] >= expected_bcc):
                debug["success"] = True
                return True, debug

        time.sleep(poll_interval)

    return False, debug


# ============================================================
# ROUTE — LIJST
# ============================================================
@bp.get("")
@with_session
def messages_list(_session=None, _creds=None):
    box = request.args.get("box", "inbox")
    return jsonify({
        "box": box,
        "messages": _get_message_list(_session, base_url(_creds), box),
    })


@bp.get("/scheduled")
@with_session
def messages_scheduled(_session=None, _creds=None):
    base = base_url(_creds)
    msgs = _get_message_list(_session, base, SCHEDULED_BOX)
    return jsonify({
        "box": SCHEDULED_BOX,
        "count": len(msgs),
        "messages": msgs,
    })


@bp.get("/drafts")
@with_session
def messages_drafts(_session=None, _creds=None):
    base = base_url(_creds)
    msgs = _get_message_list(_session, base, DRAFT_BOX)
    return jsonify({
        "box": DRAFT_BOX,
        "count": len(msgs),
        "messages": msgs,
    })


@bp.get("/<msg_id>")
@with_session
def message_read(msg_id, _session=None, _creds=None):
    box = request.args.get("box", "inbox")
    xml_text = _get_message_full_xml(_session, base_url(_creds), msg_id, box)
    parsed = _parse_full_message(xml_text)

    parsed["to_list"]  = _strip_receiver_meta(parsed.get("to_list", []))
    parsed["cc_list"]  = _strip_receiver_meta(parsed.get("cc_list", []))
    parsed["bcc_list"] = _strip_receiver_meta(parsed.get("bcc_list", []))

    return jsonify(parsed)


@bp.post("/<msg_id>/read")
@with_session
def message_mark_read(msg_id, _session=None, _creds=None):
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
        if mid and msg.findtext("unread", "") == "0":
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
                         allow_redirects=True,
                         timeout=60)
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
# SCHEDULED — BEHEER
# ============================================================
@bp.post("/<msg_id>/cancel-scheduled")
@with_session
def message_cancel_scheduled(msg_id, _session=None, _creds=None):
    base = base_url(_creds)
    try:
        r = _dispatch(_session, base, [{
            "subsystem": "postboxes",
            "action": "quick delete",
            "params": {"msgID": msg_id},
        }])
        body = r.text or ""
        body_low = body.lower()
        ok = ("<status>ok</status>" in body_low or
              "<success>1</success>" in body_low or
              (r.status_code in (200, 204) and "error" not in body_low))
        return jsonify({
            "ok": ok,
            "action": "quick delete",
            "msg_id": msg_id,
            "status": r.status_code,
            "response": body[:300],
        })
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


def _get_scheduled_compose_html(session, base, msg_id):
    url = (f"/?module=Messages&file=composeMessage"
           f"&boxType=draft&composeType=5&msgID={msg_id}")
    r = session.request("GET", url, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        return None
    return r.text


def _extract_html_recipients(session, base, msg_id):
    html = _get_scheduled_compose_html(session, base, msg_id)
    if not html:
        return {"to": [], "cc": [], "bcc": []}

    return {
        "to":  _extract_receivers_from_html(html, role=0),
        "cc":  _extract_receivers_from_html(html, role=1),
        "bcc": _extract_receivers_from_html(html, role=2),
    }


def _read_scheduled_full(session, base, msg_id, include_meta=False):
    xml_text = _get_message_full_xml(session, base, msg_id, SCHEDULED_BOX)
    parsed = _parse_full_message(xml_text)

    if include_meta:
        html_recipients = _extract_html_recipients(session, base, msg_id)
    else:
        html_recipients = {"to": [], "cc": [], "bcc": []}

    def _pack(rlist):
        packed = [
            {
                "user_id": r.get("user_id", ""),
                "name": r.get("name", ""),
                "is_co_account": bool(r.get("is_co_account"))
                                 or (r.get("user_id", "") or "").startswith("U"),
                "user_lt": str(r.get("user_lt", "2" if r.get("is_co_account") else "0")),
                "ss_id": r.get("ss_id", "455"),
            }
            for r in rlist
        ]
        if include_meta:
            return packed
        return _strip_receiver_meta(packed)

    def _merge(xml_list, html_list):
        merged = []
        seen = set()

        for r in html_list:
            uid = r.get("user_id", "") or ""
            name = (r.get("name") or "").strip()
            key = (uid, str(r.get("user_lt", "")), name.lower())
            if key in seen:
                continue
            seen.add(key)
            merged.append({
                "user_id": uid,
                "name": name,
                "is_co_account": bool(r.get("is_co_account")),
                "user_lt": str(r.get("user_lt", "2" if r.get("is_co_account") else "0")),
                "ss_id": r.get("ss_id", "455"),
            })

        for r in xml_list:
            uid = r.get("user_id", "") or ""
            name = (r.get("name") or "").strip()
            name_lc = name.lower()
            key = (uid, str(r.get("user_lt", "")), name_lc)
            if key in seen:
                continue
            if not uid and name_lc:
                already = False
                for m in merged:
                    if (m.get("name") or "").strip().lower() == name_lc:
                        already = True
                        break
                if already:
                    continue
            seen.add(key)
            merged.append({
                "user_id": uid,
                "name": name,
                "is_co_account": bool(r.get("is_co_account")),
                "user_lt": str(r.get("user_lt", "2" if r.get("is_co_account") else "0")),
                "ss_id": r.get("ss_id", "455"),
            })

        return merged

    to_list  = _merge(parsed.get("to_list", []),  html_recipients["to"])
    cc_list  = _merge(parsed.get("cc_list", []),  html_recipients["cc"])
    bcc_list = _merge(parsed.get("bcc_list", []), html_recipients["bcc"])

    receivers = {
        "to":  _pack(to_list),
        "cc":  _pack(cc_list),
        "bcc": _pack(bcc_list),
    }

    if include_meta:
        for role_key in ("to", "cc", "bcc"):
            rlist = receivers[role_key]
            if not rlist:
                continue
            if all(r.get("user_id") for r in rlist):
                continue
            names_str = ", ".join(r["name"] for r in rlist if r.get("name") and not r.get("user_id"))
            if not names_str:
                continue
            resolved = _resolve_receivers_from_string(session, base, names_str, role_key)
            if resolved:
                new_list = []
                for r in rlist:
                    if r.get("user_id"):
                        new_list.append(r)
                    else:
                        match = None
                        for rr in resolved:
                            if (rr.get("name") or "").lower() == (r.get("name") or "").lower():
                                match = rr
                                break
                        if match:
                            new_list.append(match)
                        else:
                            new_list.append(r)
                receivers[role_key] = new_list

    return {
        "html": "",
        "fields": {},
        "receivers": receivers,
        "subject": parsed.get("subject", "") or "",
        "body_html": parsed.get("body_html", "") or "",
        "send_date": parsed.get("send_date", "") or "",
        "orig_msg_id": "",
        "old": parsed,
        "include_meta": include_meta,
    }


@bp.get("/<msg_id>/scheduled")
@with_session
def message_read_scheduled(msg_id, _session=None, _creds=None):
    base = base_url(_creds)

    include_meta = (request.args.get("edit") == "1"
                    or request.args.get("include_meta") == "1")

    try:
        info = _read_scheduled_full(_session, base, msg_id, include_meta=include_meta)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    return jsonify({
        "msg_id": msg_id,
        "subject": info["subject"],
        "body_html": info["body_html"],
        "body_text": strip_html(info["body_html"]),
        "send_date": info["send_date"],
        "orig_msg_id": info["orig_msg_id"],
        "to": info["receivers"]["to"],
        "cc": info["receivers"]["cc"],
        "bcc": info["receivers"]["bcc"],
        "to_full": info["receivers"]["to"],
        "cc_full": info["receivers"]["cc"],
        "bcc_full": info["receivers"]["bcc"],
        "include_meta": include_meta,
        "has_compose_html": include_meta,
        "fields_found": [],
    })


# ============================================================
# SCHEDULED — UPDATE
# ============================================================
@bp.put("/<msg_id>/scheduled")
@bp.patch("/<msg_id>/scheduled")
@with_session
def message_update_scheduled(msg_id, _session=None, _creds=None):
    data = request.get_json(force=True, silent=True) or {}
    base = base_url(_creds)

    try:
        info = _read_scheduled_full(_session, base, msg_id, include_meta=True)
    except Exception as e:
        return jsonify({"error": f"kon bericht niet lezen: {e}"}), 500

    old_subject = info["subject"]
    old_body = info["body_html"]
    old_receivers = info["receivers"]
    old_send_date = info["send_date"]

    new_subject = data.get("subject")
    if new_subject is None:
        new_subject = old_subject or "(geen onderwerp)"

    new_body = data.get("message_html")
    if new_body is None or new_body == "":
        plain = data.get("message")
        if plain:
            new_body = f"<p>{plain}</p>"
        else:
            new_body = old_body or "<p></p>"

    if data.get("keep_send_date"):
        send_date_raw = old_send_date
    else:
        send_date_raw = data.get("send_date")
        if send_date_raw is None:
            send_date_raw = old_send_date

    if send_date_raw:
        try:
            send_date = _format_send_date(send_date_raw)
        except ValueError as e:
            return jsonify({"error": f"ongeldige send_date: {e}"}), 400
    else:
        send_date = ""

    if "to" in data:
        new_to = _normalize_user_list(
            _session, base, data.get("to") or [], "to",
            old_list=old_receivers["to"])
    else:
        new_to = list(old_receivers["to"])

    if "cc" in data:
        new_cc = _normalize_user_list(
            _session, base, data.get("cc") or [], "cc",
            old_list=old_receivers["cc"])
    else:
        new_cc = list(old_receivers["cc"])

    if "bcc" in data:
        new_bcc = _normalize_user_list(
            _session, base, data.get("bcc") or [], "bcc",
            old_list=old_receivers["bcc"])
    else:
        new_bcc = list(old_receivers["bcc"])

    if not new_to:
        return jsonify({
            "error": "geen ontvangers (to)",
            "old_to_count": len(old_receivers["to"]),
        }), 400

    try:
        r_del = _dispatch(_session, base, [{
            "subsystem": "postboxes",
            "action": "quick delete",
            "params": {"msgID": msg_id},
        }])
        body_del = r_del.text or ""
        body_del_low = body_del.lower()
        del_ok = ("<status>ok</status>" in body_del_low or
                  "<success>1</success>" in body_del_low or
                  (r_del.status_code in (200, 204) and "error" not in body_del_low))
    except Exception as e:
        return jsonify({"error": f"verwijderen mislukt: {e}"}), 500

    if not del_ok:
        return jsonify({
            "error": "kon oud bericht niet verwijderen",
            "delete_response": body_del[:300],
        }), 500

    try:
        tokens = _get_compose_tokens(_session, base)
    except Exception as e:
        return jsonify({"error": f"kon tokens niet ophalen: {e}"}), 500

    if not tokens.get("random_dir"):
        return jsonify({"error": "geen compose-tokens"}), 500

    add_ok, add_debug = _add_users_and_wait(
        _session, base, tokens, new_to, new_cc, new_bcc,
        max_wait=5.0, sleep_between=0.12)

    fields = {
        "send": "send",
        "origMsgID": "0",
        "composeAction": "0",
        "randomDir": tokens["random_dir"],
        "uniqueUsc": tokens["unique_usc"],
        "showTab": "tab1Container",
        "delFile": "0",
        "composeType": "0",
        "msgID": "0",
        "msgFormSelectedTab": "",
        "sendDate": send_date,
        "subject": new_subject,
        "bcc": "1" if new_bcc else "0",
        "message": new_body,
        "encryptedSender": tokens["encrypted_sender"],
    }
    if new_to:
        fields["receiverPart0"] = _build_receiver_xml(new_to, 0)
    if new_cc:
        fields["receiverPart1"] = _build_receiver_xml(new_cc, 1)
    if new_bcc:
        fields["receiverPart2"] = _build_receiver_xml(new_bcc, 2)

    m = MultipartEncoder(fields=fields)
    try:
        r_send = _session.request(
            "POST",
            "/?module=Messages&file=composeMessage&boxType=inbox&composeType=0&msgID=undefined",
            data=m,
            headers={"Content-Type": m.content_type,
                     "Origin": base, "Referer": base + "/"},
            allow_redirects=False,
            timeout=30,
        )
    except Exception as e:
        return jsonify({
            "error": f"verzenden mislukt NA delete: {e}",
            "deleted_old": True,
        }), 500

    ok, reason = _is_send_success(r_send)

    return jsonify({
        "ok": ok,
        "reason": reason,
        "msg_id": msg_id,
        "deleted_old": del_ok,
        "subject": new_subject,
        "send_date": send_date,
        "body_length": len(new_body),
        "to_count": len(new_to),
        "cc_count": len(new_cc),
        "bcc_count": len(new_bcc),
        "to": new_to,
        "cc": new_cc,
        "bcc": new_bcc,
        "status": r_send.status_code,
        "add_debug": add_debug,
        "add_ok": add_ok,
    })


# ============================================================
# RESCHEDULE
# ============================================================
@bp.post("/<msg_id>/reschedule")
@with_session
def message_reschedule(msg_id, _session=None, _creds=None):
    data = request.get_json(force=True, silent=True) or {}
    send_date_raw = data.get("send_date") or ""

    if not send_date_raw:
        return jsonify({"error": "send_date verplicht"}), 400

    try:
        send_date = _format_send_date(send_date_raw)
    except ValueError as e:
        return jsonify({"error": f"ongeldige send_date: {e}"}), 400

    base = base_url(_creds)

    try:
        info = _read_scheduled_full(_session, base, msg_id, include_meta=True)
    except Exception as e:
        return jsonify({"error": f"kon bericht niet lezen: {e}"}), 500

    new_to = data.get("to")
    new_cc = data.get("cc")
    new_bcc = data.get("bcc")

    if new_to is None:
        new_to = info["receivers"]["to"]
    if new_cc is None:
        new_cc = info["receivers"]["cc"]
    if new_bcc is None:
        new_bcc = info["receivers"]["bcc"]

    if not new_to:
        return jsonify({
            "error": "kon ontvangers niet bepalen",
            "hint": "geef 'to' mee in de request body",
        }), 400

    subject = data.get("subject") or info["subject"] or "(geen onderwerp)"
    body_html = data.get("message_html") or info["body_html"] or "<p></p>"

    try:
        r_del = _dispatch(_session, base, [{
            "subsystem": "postboxes",
            "action": "quick delete",
            "params": {"msgID": msg_id},
        }])
        body_del = r_del.text or ""
        body_del_low = body_del.lower()
        del_ok = ("<status>ok</status>" in body_del_low or
                  "<success>1</success>" in body_del_low or
                  (r_del.status_code in (200, 204) and "error" not in body_del_low))
    except Exception as e:
        return jsonify({"error": f"verwijderen mislukt: {e}"}), 500

    if not del_ok:
        return jsonify({
            "error": "kon oud bericht niet verwijderen",
            "delete_response": body_del[:300],
        }), 500

    try:
        tokens = _get_compose_tokens(_session, base)
    except Exception as e:
        return jsonify({"error": f"kon tokens niet ophalen: {e}"}), 500

    if not tokens.get("random_dir"):
        return jsonify({"error": "geen compose-tokens"}), 500

    add_ok, add_debug = _add_users_and_wait(
        _session, base, tokens, new_to, new_cc, new_bcc,
        max_wait=5.0, sleep_between=0.12)

    fields = {
        "send": "send",
        "origMsgID": "0",
        "composeAction": "0",
        "randomDir": tokens["random_dir"],
        "uniqueUsc": tokens["unique_usc"],
        "showTab": "tab1Container",
        "delFile": "0",
        "composeType": "0",
        "msgID": "0",
        "msgFormSelectedTab": "",
        "sendDate": send_date,
        "subject": subject,
        "bcc": "1" if new_bcc else "0",
        "message": body_html,
        "encryptedSender": tokens["encrypted_sender"],
    }
    if new_to:
        fields["receiverPart0"] = _build_receiver_xml(new_to, 0)
    if new_cc:
        fields["receiverPart1"] = _build_receiver_xml(new_cc, 1)
    if new_bcc:
        fields["receiverPart2"] = _build_receiver_xml(new_bcc, 2)

    m = MultipartEncoder(fields=fields)
    try:
        r_send = _session.request(
            "POST",
            "/?module=Messages&file=composeMessage&boxType=inbox&composeType=0&msgID=undefined",
            data=m,
            headers={"Content-Type": m.content_type,
                     "Origin": base, "Referer": base + "/"},
            allow_redirects=False,
            timeout=30,
        )
    except Exception as e:
        return jsonify({"error": f"verzenden mislukt: {e}"}), 500

    ok, reason = _is_send_success(r_send)

    return jsonify({
        "ok": ok,
        "reason": reason,
        "msg_id": msg_id,
        "new_send_date": send_date,
        "status": r_send.status_code,
        "deleted_old": del_ok,
        "to_count": len(new_to),
        "cc_count": len(new_cc or []),
        "bcc_count": len(new_bcc or []),
        "subject": subject,
        "add_debug": add_debug,
        "add_ok": add_ok,
    })


# ============================================================
# DRAFTS — TOKENS
# ============================================================
def _get_draft_tokens(session, base, draft_id):
    url = (f"/?module=Messages&file=composeMessage"
           f"&boxType=draft&composeType=5&msgID={draft_id}")
    r = session.request("GET", url, timeout=REQUEST_TIMEOUT)
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

    receivers = {
        "to": _extract_receivers_from_html(html, role=0),
        "cc": _extract_receivers_from_html(html, role=1),
        "bcc": _extract_receivers_from_html(html, role=2),
    }

    return {
        "draft_id": draft_id,
        "orig_msg_id": fields.get("origMsgID", draft_id),
        "subject": fields.get("subject", ""),
        "body": body.strip(),
        "send_date": fields.get("sendDate", ""),
        "random_dir": fields.get("randomDir", ""),
        "unique_usc": fields.get("uniqueUsc", ""),
        "encrypted_sender": fields.get("encryptedSender", ""),
        "receivers": receivers,
    }


def _read_draft_full(session, base, draft_id, include_meta=False):
    data = _get_draft_tokens(session, base, draft_id)

    receivers = {
        "to":  data["receivers"]["to"],
        "cc":  data["receivers"]["cc"],
        "bcc": data["receivers"]["bcc"],
    }

    def _pack(rlist):
        packed = [
            {
                "user_id": r.get("user_id", ""),
                "name": r.get("name", ""),
                "is_co_account": bool(r.get("is_co_account"))
                                 or (r.get("user_id", "") or "").startswith("U"),
                "user_lt": str(r.get("user_lt", "2" if r.get("is_co_account") else "0")),
                "ss_id": r.get("ss_id", "455"),
            }
            for r in rlist
        ]
        if include_meta:
            return packed
        return _strip_receiver_meta(packed)

    return {
        "draft_id": draft_id,
        "orig_msg_id": data.get("orig_msg_id", draft_id),
        "subject": data.get("subject", "") or "",
        "body_html": data.get("body", "") or "",
        "send_date": data.get("send_date", "") or "",
        "receivers": {
            "to":  _pack(receivers["to"]),
            "cc":  _pack(receivers["cc"]),
            "bcc": _pack(receivers["bcc"]),
        },
    }


@bp.get("/draft/<draft_id>/full")
@with_session
def message_read_draft(draft_id, _session=None, _creds=None):
    base = base_url(_creds)
    include_meta = (request.args.get("edit") == "1"
                    or request.args.get("include_meta") == "1")

    try:
        info = _read_draft_full(_session, base, draft_id, include_meta=include_meta)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    return jsonify({
        "draft_id": info["draft_id"],
        "orig_msg_id": info["orig_msg_id"],
        "subject": info["subject"],
        "body_html": info["body_html"],
        "body_text": strip_html(info["body_html"]),
        "send_date": info["send_date"],
        "to": info["receivers"]["to"],
        "cc": info["receivers"]["cc"],
        "bcc": info["receivers"]["bcc"],
        "to_full": info["receivers"]["to"],
        "cc_full": info["receivers"]["cc"],
        "bcc_full": info["receivers"]["bcc"],
        "include_meta": include_meta,
        "is_draft": True,
    })


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
# DRAFTS — VOORKOM DUBBELE DRAFTS
# ============================================================
def _find_existing_draft(session, base, subject, to_users, cc_users, bcc_users):
    if not subject:
        return None

    try:
        drafts = _get_message_list(session, base, DRAFT_BOX)
    except Exception:
        return None

    if not drafts:
        return None

    subject_norm = (subject or "").strip().lower()

    new_to_ids = set()
    for u in (to_users or []):
        uid = _strip_u_prefix(u.get("user_id", ""))
        lt = str(u.get("user_lt", "0"))
        new_to_ids.add((uid, lt))

    if not new_to_ids:
        return None

    for d in drafts:
        d_subject = (d.get("subject") or "").strip().lower()
        if d_subject != subject_norm:
            continue

        try:
            info = _read_draft_full(session, base, d["id"], include_meta=True)
        except Exception:
            continue

        draft_to_ids = set()
        for u in (info.get("receivers", {}).get("to") or []):
            uid = _strip_u_prefix(u.get("user_id", ""))
            lt = str(u.get("user_lt", "0"))
            draft_to_ids.add((uid, lt))

        if draft_to_ids == new_to_ids:
            return d["id"]

    return None


# ============================================================
# DRAFTS — SAVE
# ============================================================
@bp.post("/draft/save")
@with_session
def message_save_draft(_session=None, _creds=None):
    data = request.get_json(force=True, silent=True) or {}
    base = base_url(_creds)

    draft_id = (data.get("draft_id") or "").strip()
    subject = data.get("subject") or "(geen onderwerp)"
    message_html = data.get("message_html") or ""
    message_plain = data.get("message") or ""

    if message_html.strip():
        body = message_html
    elif message_plain:
        body = f"<p>{message_plain}</p>"
    else:
        body = "<p></p>"

    to_users  = _normalize_user_list(_session, base, data.get("to") or [],  "to")
    cc_users  = _normalize_user_list(_session, base, data.get("cc") or [],  "cc")
    bcc_users = _normalize_user_list(_session, base, data.get("bcc") or [], "bcc")

    if not to_users:
        return jsonify({"error": "minstens één ontvanger (to) verplicht"}), 400

    auto_found_draft_id = None
    if not draft_id:
        try:
            auto_found_draft_id = _find_existing_draft(
                _session, base, subject, to_users, cc_users, bcc_users
            )
        except Exception:
            auto_found_draft_id = None
        if auto_found_draft_id:
            draft_id = auto_found_draft_id

    if draft_id:
        try:
            tokens = _get_draft_tokens(_session, base, draft_id)
        except Exception as e:
            return jsonify({"error": f"kon concept niet laden: {e}"}), 500
    else:
        tokens = _get_compose_tokens(_session, base)

    if not tokens.get("random_dir"):
        return jsonify({"error": "kon compose-tokens niet ophalen"}), 500

    for role_idx in (0, 1, 2):
        _clear_compose_recipients(_session, base, tokens["unique_usc"], role_idx)

    add_ok, add_debug = _add_users_and_wait(
        _session, base, tokens, to_users, cc_users, bcc_users,
        max_wait=5.0, sleep_between=0.12)

    is_update = bool(draft_id)
    compose_action = "5" if is_update else "0"

    try:
        r = _dispatch(_session, base, [{
            "subsystem": "draft",
            "action": "save draft",
            "params": {
                "draftID": draft_id if is_update else "0",
                "send": "refresh",
                "origMsgID": "0",
                "composeAction": compose_action,
                "randomDir": tokens["random_dir"],
                "uniqueUsc": tokens["unique_usc"],
                "subject": subject,
                "bcc": "1" if bcc_users else "0",
                "composeType": "0",
                "msgID": "0",
                "message": body,
                "preload_type": "",
                "encryptedSender": tokens["encrypted_sender"],
                "sendDate": "",
            },
        }])
    except Exception as e:
        return jsonify({"error": f"opslaan mislukt: {e}"}), 500

    body_low = (r.text or "").lower()
    ok = ("<status>ok</status>" in body_low or
          "<success>1</success>" in body_low or
          (r.status_code in (200, 204) and "error" not in body_low))

    return jsonify({
        "ok": ok,
        "status": r.status_code,
        "draft_id": draft_id or None,
        "auto_found_draft_id": auto_found_draft_id,
        "is_update": is_update,
        "subject": subject,
        "to_count": len(to_users),
        "cc_count": len(cc_users),
        "bcc_count": len(bcc_users),
        "body_length": len(body),
        "response_preview": (r.text or "")[:300],
        "add_debug": add_debug,
        "add_ok": add_ok,
    })


# ============================================================
# DRAFTS — DELETE
# ============================================================
@bp.post("/draft/<draft_id>/delete")
@with_session
def message_delete_draft(draft_id, _session=None, _creds=None):
    base = base_url(_creds)
    try:
        r = _dispatch(_session, base, [{
            "subsystem": "postboxes",
            "action": "quick delete",
            "params": {"msgID": draft_id},
        }])
        body = r.text or ""
        body_low = body.lower()
        ok = ("<status>ok</status>" in body_low or
              "<success>1</success>" in body_low or
              (r.status_code in (200, 204) and "error" not in body_low))
        return jsonify({
            "ok": ok,
            "action": "quick delete",
            "draft_id": draft_id,
            "status": r.status_code,
            "response": body[:300],
        })
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


# ============================================================
# DRAFTS — SCHEDULE
# ============================================================
@bp.post("/draft/<draft_id>/schedule")
@with_session
def message_schedule_draft(draft_id, _session=None, _creds=None):
    data = request.get_json(force=True, silent=True) or {}
    send_date_raw = data.get("send_date") or ""

    if not send_date_raw:
        return jsonify({"error": "send_date verplicht"}), 400

    try:
        send_date = _format_send_date(send_date_raw)
    except ValueError as e:
        return jsonify({"error": f"ongeldige send_date: {e}"}), 400

    base = base_url(_creds)

    try:
        info = _read_draft_full(_session, base, draft_id, include_meta=True)
    except Exception as e:
        return jsonify({"error": f"kon concept niet lezen: {e}"}), 500

    subject = data.get("subject") or info["subject"] or "(geen onderwerp)"
    body_html = data.get("message_html") or info["body_html"] or "<p></p>"

    if "to" in data:
        new_to = _normalize_user_list(_session, base, data.get("to") or [], "to")
    else:
        new_to = info["receivers"]["to"]

    if "cc" in data:
        new_cc = _normalize_user_list(_session, base, data.get("cc") or [], "cc")
    else:
        new_cc = info["receivers"]["cc"]

    if "bcc" in data:
        new_bcc = _normalize_user_list(_session, base, data.get("bcc") or [], "bcc")
    else:
        new_bcc = info["receivers"]["bcc"]

    if not new_to:
        return jsonify({"error": "geen ontvangers (to) in concept"}), 400

    try:
        _dispatch(_session, base, [{
            "subsystem": "postboxes",
            "action": "quick delete",
            "params": {"msgID": draft_id},
        }])
    except Exception:
        pass

    tokens = _get_compose_tokens(_session, base)
    if not tokens.get("random_dir"):
        return jsonify({"error": "geen compose-tokens"}), 500

    add_ok, add_debug = _add_users_and_wait(
        _session, base, tokens, new_to, new_cc, new_bcc,
        max_wait=5.0, sleep_between=0.12)

    fields = {
        "send": "send",
        "origMsgID": "0",
        "composeAction": "0",
        "randomDir": tokens["random_dir"],
        "uniqueUsc": tokens["unique_usc"],
        "showTab": "tab1Container",
        "delFile": "0",
        "composeType": "0",
        "msgID": "0",
        "msgFormSelectedTab": "",
        "sendDate": send_date,
        "subject": subject,
        "bcc": "1" if new_bcc else "0",
        "message": body_html,
        "encryptedSender": tokens["encrypted_sender"],
    }
    if new_to:
        fields["receiverPart0"] = _build_receiver_xml(new_to, 0)
    if new_cc:
        fields["receiverPart1"] = _build_receiver_xml(new_cc, 1)
    if new_bcc:
        fields["receiverPart2"] = _build_receiver_xml(new_bcc, 2)

    m = MultipartEncoder(fields=fields)
    try:
        r = _session.request(
            "POST",
            "/?module=Messages&file=composeMessage&boxType=inbox&composeType=0&msgID=undefined",
            data=m,
            headers={"Content-Type": m.content_type,
                     "Origin": base, "Referer": base + "/"},
            allow_redirects=False,
            timeout=30,
        )
    except Exception as e:
        return jsonify({"error": f"verzenden mislukt: {e}"}), 500

    ok, reason = _is_send_success(r)

    return jsonify({
        "ok": ok,
        "reason": reason,
        "draft_id": draft_id,
        "send_date": send_date,
        "status": r.status_code,
        "to_count": len(new_to),
        "cc_count": len(new_cc or []),
        "bcc_count": len(new_bcc or []),
        "subject": subject,
        "add_debug": add_debug,
        "add_ok": add_ok,
    })


# ============================================================
# GEBRUIKERS ZOEKEN
# ============================================================
def _get_compose_tokens(session, base):
    r = session.request(
        "GET",
        "/?module=Messages&file=composeMessage&boxType=inbox&composeType=0&msgID=undefined",
        timeout=REQUEST_TIMEOUT,
    )
    fields = {}
    for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"[^>]*>', r.text):
        fields[m.group(1)] = m.group(2)
    return {
        "random_dir": fields.get("randomDir", ""),
        "unique_usc": fields.get("uniqueUsc", ""),
        "encrypted_sender": fields.get("encryptedSender", ""),
    }


def _read_response_safely(r, max_bytes=MAX_SEARCH_BYTES):
    """
    Leest de response in chunks en stopt zodra max_bytes is bereikt.
    Retourneert (text, truncated).
    """
    chunks = []
    total = 0
    try:
        for chunk in r.iter_content(chunk_size=16384):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                chunks.append(chunk[:max(0, max_bytes - (total - len(chunk)))])
                return b"".join(chunks).decode("utf-8", errors="ignore"), True
            chunks.append(chunk)
    except Exception:
        # Bij netwerkfout: retourneer wat we hebben
        pass
    finally:
        try:
            r.close()
        except Exception:
            pass
    return b"".join(chunks).decode("utf-8", errors="ignore"), False


def _search_users_single(session, base, query, unique_usc, search_type):
    parent = f"insertSearchFieldContainer_{search_type}_0"
    try:
        r = session.request(
            "POST", "/?module=Messages&file=searchUsers",
            data={"val": query, "type": str(search_type),
                  "parentNodeId": parent,
                  "xml": "<results></results>",
                  "uniqueUsc": unique_usc},
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": base, "Referer": base + "/"},
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )
    except Exception:
        return []

    text, truncated = _read_response_safely(r)
    if truncated:
        # Response te groot — geef niets terug in plaats van OOM
        return []

    users = []
    for um in re.finditer(r"<user>(.*?)</user>", text, re.DOTALL):
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

        is_co = (user_lt in ("1", "2")) or bool(co_name)

        if is_co and co_name:
            display_name = co_name
            if name and name.lower() != co_name.lower():
                display_name = f"{co_name} (via {name})"
        else:
            display_name = name

        full_name = f"Co: {display_name}" if is_co else display_name

        if is_co:
            type_label = "co (professioneel)" if user_lt == "1" else "co (familie)"
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
                    key = (u["user_id"], u["is_co_account"], u.get("user_lt", ""))
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
    clean_id = _strip_u_prefix(user_id)
    parent = f"insertSearchFieldContainer_{dropped_type}_0"
    return session.request(
        "POST", "/?module=Messages&file=searchUsers&function=addUserToSelected",
        data={"id": clean_id, "typeId": type_id, "type": str(dropped_type),
              "parentNodeId": parent, "ssid": ssid, "userlt": userlt,
              "uniqueUsc": unique_usc},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest",
                 "Origin": base, "Referer": base + "/"},
        timeout=REQUEST_TIMEOUT,
    ).text


def _build_receiver_xml(users, receiver_type):
    xml = "<results>"
    for u in users:
        uid = str(u.get("user_id", ""))
        if u.get("is_co_account"):
            if not uid.startswith("U"):
                uid = f"U{uid}"
            userlt = str(u.get("user_lt", "2")) or "2"
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
# USER INFO
# ============================================================
@bp.get("/user/<user_id>")
@with_session
def messages_get_user(user_id, _session=None, _creds=None):
    base = base_url(_creds)
    raw = (user_id or "").strip()
    if not raw:
        return jsonify({"error": "user_id verplicht"}), 400

    has_u = raw.startswith("U") or raw.startswith("u")
    clean_id = raw[1:] if has_u else raw

    tried = []

    try:
        tokens = _get_compose_tokens(_session, base)
        if tokens.get("unique_usc"):
            for cand in (clean_id, f"U{clean_id}"):
                tried.append(f"search:{cand}")
                for st in [0, 1, 2, 3, 4, 5]:
                    try:
                        users = _search_users_single(_session, base, cand, tokens["unique_usc"], st)
                        for u in users:
                            uid = str(u.get("user_id", ""))
                            if uid == cand or uid == clean_id or uid == f"U{clean_id}":
                                return jsonify({
                                    "found": True,
                                    "user_id": uid,
                                    "name": u.get("display_name") or u.get("name"),
                                    "is_co_account": bool(u.get("is_co_account")),
                                    "user_lt": u.get("user_lt", "0"),
                                    "ss_id": u.get("ss_id", "455"),
                                    "type_label": u.get("type_label"),
                                    "source": "search",
                                    "tried": tried,
                                })
                    except Exception:
                        continue
    except Exception:
        pass

    return jsonify({
        "found": False,
        "user_id": raw,
        "clean_id": clean_id,
        "tried": tried,
        "is_co_account": has_u,
        "ss_id": "455",
    })


def _extract_name_from_html(html):
    patterns = [
        r'<h1[^>]*class="[^"]*user[^"]*name[^"]*"[^>]*>(.*?)</h1>',
        r'<span[^>]*class="[^"]*displayName[^"]*"[^>]*>(.*?)</span>',
        r'<div[^>]*class="[^"]*profileName[^"]*"[^>]*>(.*?)</div>',
        r'<title>(.*?)</title>',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
        if m:
            name = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            name = (name.replace("&nbsp;", " ")
                        .replace("&amp;", "&")
                        .replace("&lt;", "<")
                        .replace("&gt;", ">")
                        .replace("&quot;", '"')
                        .replace("&#39;", "'"))
            if name and name.lower() not in ("smartschool", "profiel", "profile", ""):
                name = re.sub(r"\s*[-–|]\s*Smartschool.*$", "", name, flags=re.IGNORECASE).strip()
                if name:
                    return name
    return None


def _is_send_success(r):
    if r.status_code in (302, 303):
        loc = r.headers.get("Location", "")
        return True, f"redirect → {loc}"

    if r.status_code == 200:
        body = r.text or ""
        body_low = body[:8000].lower()

        if ('http-equiv="refresh"' in body_low or
                "http-equiv='refresh'" in body_low):
            return True, "meta-refresh (scheduled succes)"

        for indicator in ("bericht verzonden", "bericht is verzonden",
                          "succesvol verzonden", "werd verzonden",
                          "message sent", "message was sent"):
            if indicator in body_low:
                return True, f"succes-indicator '{indicator}'"

        if '<form' in body_low and ('name="send"' in body_low or
                                     "name='send'" in body_low):
            return False, "compose-formulier opnieuw (form reloaded)"

        for err in ("oeps, er ging iets mis", 'class="error"',
                    "class='error'", "fout opgetreden"):
            if err in body_low:
                return False, f"foutmelding: '{err}'"

        if "<!doctype html" in body_low or "<html" in body_low:
            return True, "HTML zonder form (waarschijnlijk succes)"

        if len(body.strip()) < 50:
            return True, "lege/korte body"

        return True, "200 zonder HTML"

    return False, f"HTTP {r.status_code}"


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
    attachments = data.get("attachments") or []
    send_date_raw = data.get("send_date") or ""
    draft_id = data.get("draft_id") or ""

    base = base_url(_creds)

    to_users = _normalize_user_list(_session, base, data.get("to") or [], "to")
    cc_users = _normalize_user_list(_session, base, data.get("cc") or [], "cc")
    bcc_users = _normalize_user_list(_session, base, data.get("bcc") or [], "bcc")

    if not subject or not to_users:
        return jsonify({"error": "subject en to zijn verplicht"}), 400

    if message_html and message_html.strip():
        body = message_html
    elif message_plain:
        body = f"<p>{message_plain}</p>"
    else:
        body = "<p></p>"

    if attachments:
        body += '<hr><p><b>Bijlagen:</b></p><ul>'
        for att in attachments:
            url = att.get("url")
            fn = att.get("filename", "bestand")
            if url:
                body += f'<li><a href="{url}" target="_blank">{fn}</a></li>'
        body += '</ul>'

    send_date = ""
    if send_date_raw:
        try:
            send_date = _format_send_date(send_date_raw)
        except ValueError as e:
            return jsonify({"error": f"ongeldige send_date: {e}"}), 400

    if draft_id:
        tokens = _get_draft_tokens(_session, base, draft_id)
    else:
        tokens = _get_compose_tokens(_session, base)

    if not tokens.get("random_dir"):
        return jsonify({"error": "kon tokens niet ophalen"}), 500

    if draft_id:
        for role_idx in (0, 1, 2):
            _clear_compose_recipients(_session, base, tokens["unique_usc"], role_idx)

    add_ok, add_debug = _add_users_and_wait(
        _session, base, tokens, to_users, cc_users, bcc_users,
        max_wait=5.0, sleep_between=0.12)

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
        allow_redirects=False,
        timeout=60,
    )

    ok, reason = _is_send_success(r)

    return jsonify({
        "ok": ok,
        "reason": reason,
        "status": r.status_code,
        "scheduled": bool(send_date),
        "send_date": send_date,
        "draft_id": draft_id or None,
        "has_bcc": bool(bcc_users),
        "to_count": len(to_users),
        "cc_count": len(cc_users),
        "bcc_count": len(bcc_users),
        "body_length": len(body),
        "add_debug": add_debug,
        "add_ok": add_ok,
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
            timeout=120,
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
