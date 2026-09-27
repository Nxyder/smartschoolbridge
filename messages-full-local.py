import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from requests_toolbelt.multipart.encoder import MultipartEncoder
from smartschool import (
    Smartschool,
    EnvCredentials,
    MarkMessageUnread,
    MessageMoveToArchive,
    MessageMoveToTrash,
    AdjustMessageLabel,
    MessageLabel,
)

# ============================================
# CONFIGURATIE
# ============================================
os.environ["SMARTSCHOOL_USERNAME"] = "odne.bontemps"
os.environ["SMARTSCHOOL_PASSWORD"] = "JOUW_WACHTWOORD"
os.environ["SMARTSCHOOL_MAIN_URL"] = "stjozefasoessen.smartschool.be"
os.environ["SMARTSCHOOL_MFA"] = "2011-07-27"
# ============================================

BASE_URL = "https://stjozefasoessen.smartschool.be"


# ============================================================
# DROPPEDTYPE MAPPING
# ============================================================
# (doel, is_co) -> droppedtype
# doel: 0 = Aan, 1 = CC, 2 = BCC
DROPPED_TYPE = {
    (0, False): 0,
    (1, False): 2,
    (2, False): 3,
    (0, True):  1,
    (1, True):  4,
    (2, True):  5,
}


# ============================================================
# DISPATCHER (lezen)
# ============================================================

def build_command_xml(commands):
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


def dispatch(session, commands):
    xml = build_command_xml(commands)
    encoded_xml = urllib.parse.quote(xml, safe="")
    body = f"command={encoded_xml}"

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/",
    }

    return session.request(
        method="POST",
        url="/?module=Messages&file=dispatcher",
        data=body,
        headers=headers,
    )


# ============================================================
# HELPERS
# ============================================================

def strip_html(html):
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</div>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    text = text.replace("&apos;", "'").replace("&#8211;", "-").replace("&#8364;", "€")
    return text.strip()


def clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _extract(block, tag, default=""):
    match = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.DOTALL)
    if match:
        return match.group(1).strip()
    return default


# ============================================================
# BERICHTEN LEZEN
# ============================================================

def get_message_list(session, box_type="inbox", box_id="0"):
    response = dispatch(session, [{
        "subsystem": "postboxes",
        "action": "message list",
        "params": {
            "boxType": box_type,
            "boxID": box_id,
            "sortField": "date",
            "sortKey": "desc",
            "poll": "false",
            "poll_ids": "",
            "layout": "new",
        },
    }])

    root = ET.fromstring(response.text)
    messages = []

    for msg in root.findall(".//message"):
        messages.append({
            "id": msg.findtext("id", ""),
            "from": msg.findtext("from", ""),
            "from_image": msg.findtext("fromImage", ""),
            "subject": msg.findtext("subject", ""),
            "date": msg.findtext("date", ""),
            "status": msg.findtext("status", ""),
            "attachment": msg.findtext("attachment", ""),
            "unread": msg.findtext("unread", ""),
            "real_box": msg.findtext("realBox", ""),
        })

    return messages


def get_message_full(session, msg_id, box_type="inbox"):
    response = dispatch(session, [
        {
            "subsystem": "postboxes",
            "action": "show message",
            "params": {"msgID": msg_id, "boxType": box_type, "limitList": "true"},
        },
        {
            "subsystem": "postboxes",
            "action": "attachment list",
            "params": {"msgID": msg_id, "boxType": box_type, "limitList": "true"},
        },
    ])
    return response.text


def parse_full_message(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return {"error": f"XML parse error: {e}"}

    result = {
        "id": "", "subject": "", "sender": "", "to": "", "cc": "",
        "date": "", "body_html": "", "body_text": "", "attachments": [],
    }

    message_el = root.find(".//message")
    if message_el is not None:
        result["id"] = message_el.findtext("id", "")
        result["subject"] = message_el.findtext("subject", "")
        result["date"] = message_el.findtext("date", "")
        result["sender"] = message_el.findtext("from", "")
        result["to"] = message_el.findtext("to", "")
        result["cc"] = message_el.findtext("cc", "")

        for tag in ["body", "messageBody", "content", "htmlBody"]:
            body_el = message_el.find(tag)
            if body_el is not None:
                if body_el.text:
                    result["body_html"] = body_el.text
                elif len(body_el) > 0:
                    result["body_html"] = ET.tostring(body_el, encoding="unicode")
                break

    if not result["body_html"]:
        match = re.search(r"<body[^>]*>(.*?)</body>", xml_text, re.DOTALL | re.IGNORECASE)
        if match:
            result["body_html"] = match.group(1)

    result["body_text"] = strip_html(result["body_html"])

    for att in root.findall(".//attachment"):
        file_id = (att.findtext("fileID", "") or "").strip()
        name = (att.findtext("name", "") or "").strip()
        if not file_id or not name:
            continue
        result["attachments"].append({
            "file_id": file_id,
            "name": name,
            "mime": att.findtext("mime", ""),
            "size": att.findtext("size", ""),
            "icon": att.findtext("icon", ""),
        })

    return result


# ============================================================
# BIJLAGEN
# ============================================================

def download_attachment(session, file_id, save_path=None):
    url = f"/?module=Messages&file=download&fileID={file_id}&target=0"
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Referer": BASE_URL + "/",
    }
    print(f"   Download: {url}")
    response = session.request(method="GET", url=url, headers=headers, allow_redirects=True)
    if response.status_code != 200:
        print(f"   Status: {response.status_code}")
        return None
    print(f"   Status: {response.status_code}  {len(response.content)} bytes")
    if save_path:
        with open(save_path, "wb") as f:
            f.write(response.content)
        print(f"   Opgeslagen: {save_path}")
    return response.content


# ============================================================
# ACTIES (lazy generators → list())
# ============================================================

def mark_unread(session, msg_id):
    print(f"   → Markeer {msg_id} als ongelezen...")
    try:
        result = list(MarkMessageUnread(session, msg_id=int(msg_id)))
        print(f"   ✅ {result}")
        return True
    except Exception as e:
        print(f"   ❌ {type(e).__name__}: {e}")
        return False


def archive_message(session, msg_id):
    print(f"   → Archiveer {msg_id}...")
    try:
        result = list(MessageMoveToArchive(session, msg_id=int(msg_id)))
        print(f"   ✅ {result}")
        return True
    except Exception as e:
        print(f"   ❌ {type(e).__name__}: {e}")
        return False


def trash_message(session, msg_id):
    print(f"   → Verwijder {msg_id}...")
    try:
        result = list(MessageMoveToTrash(session, msg_id=int(msg_id)))
        print(f"   ✅ {result}")
        return True
    except Exception as e:
        print(f"   ❌ {type(e).__name__}: {e}")
        return False


def apply_label(session, msg_id, label):
    print(f"   → Label {label.name} op {msg_id}...")
    try:
        result = list(AdjustMessageLabel(session, msg_id=int(msg_id), label=label))
        print(f"   ✅ {result}")
        return True
    except Exception as e:
        print(f"   ❌ {type(e).__name__}: {e}")
        return False


# ============================================================
# VERZENDEN — TOKENS
# ============================================================

def get_compose_tokens(session):
    r = session.request(
        "GET",
        "/?module=Messages&file=composeMessage&boxType=inbox&composeType=0&msgID=undefined"
    )
    html = r.text
    fields = {}
    for match in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"[^>]*>', html):
        fields[match.group(1)] = match.group(2)
    return {
        "random_dir": fields.get("randomDir", ""),
        "unique_usc": fields.get("uniqueUsc", ""),
        "encrypted_sender": fields.get("encryptedSender", ""),
    }


# ============================================================
# VERZENDEN — GEBRUIKERS ZOEKEN
# ============================================================

def search_users(session, query, unique_usc, search_type=0):
    parent_node_id = f"insertSearchFieldContainer_{search_type}_0"
    r = session.request(
        "POST",
        "/?module=Messages&file=searchUsers",
        data={"val": query, "type": str(search_type),
              "parentNodeId": parent_node_id,
              "xml": "<results></results>",
              "uniqueUsc": unique_usc},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest",
                 "Origin": BASE_URL,
                 "Referer": BASE_URL + "/"}
    )

    users = []
    for user_match in re.finditer(r"<user>(.*?)</user>", r.text, re.DOTALL):
        block = user_match.group(1)
        user_id = _extract(block, "userID")
        if not user_id:
            continue
        name = clean_html(_extract(block, "value"))
        coaccount_name = clean_html(_extract(block, "coaccountname", default=""))
        user_type = _extract(block, "userType", default="U")
        user_lt = _extract(block, "userLT", default="0")
        selectable = _extract(block, "selectable", default="on")
        ss_id = _extract(block, "ssID", default="455")
        classname = _extract(block, "classname", default="")

        is_co = (user_lt == "2") or bool(coaccount_name)

        if is_co and coaccount_name:
            display_name = coaccount_name
            if name:
                display_name += f" (via {name})"
        else:
            display_name = name

        if is_co:
            label = "CO (ouder/voogd)"
        elif user_type == "U":
            label = f"hoofd - {classname}" if classname else "hoofd"
        elif user_type == "T":
            label = "leerkracht"
        elif user_type == "G":
            label = "groep"
        else:
            label = user_type

        users.append({
            "user_id": user_id, "name": display_name, "raw_name": name,
            "user_type": user_type, "user_lt": user_lt,
            "coaccount_name": coaccount_name, "is_co_account": is_co,
            "selectable": selectable, "ss_id": ss_id,
            "classname": classname, "type_label": label,
        })
    return users


# ============================================================
# VERZENDEN — SESSIE-REGISTRATIE
# ============================================================

def add_user_to_selected(session, user_id, unique_usc, dropped_type,
                         ssid="455", userlt="0", type_id="users"):
    parent_node_id = f"insertSearchFieldContainer_{dropped_type}_0"
    data = {
        "id": user_id, "typeId": type_id, "type": str(dropped_type),
        "parentNodeId": parent_node_id, "ssid": ssid,
        "userlt": userlt, "uniqueUsc": unique_usc,
    }
    return session.request(
        "POST",
        "/?module=Messages&file=searchUsers&function=addUserToSelected",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest",
                 "Origin": BASE_URL, "Referer": BASE_URL + "/"}
    ).text


# ============================================================
# VERZENDEN — INTERACTIEVE SELECTIE
# ============================================================

def select_users_interactive(session, unique_usc, role_label="Aan",
                             search_types=(0,)):
    selected = []
    print(f"\n--- {role_label} ontvangers toevoegen ---")
    print("(typ een zoekterm, of leeg om te stoppen)\n")

    while True:
        query = input(f"[{role_label}] Zoek (leeg = stoppen): ").strip()
        if not query:
            break

        seen_ids = set()
        users = []
        for st in search_types:
            for u in search_users(session, query, unique_usc, search_type=st):
                key = (u["user_id"], u["is_co_account"])
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                users.append(u)

        if not users:
            print("  Geen gebruikers gevonden.\n")
            continue

        print(f"\n  Resultaten voor '{query}':")
        for i, user in enumerate(users, 1):
            marker = "★" if user["is_co_account"] else " "
            selectable = "" if user["selectable"] == "on" else " (niet selecteerbaar)"
            print(f"    [{i}] {marker} {user['name']} [{user['type_label']}]{selectable}")

        print()
        keuze = input(f"  Kies nummer(s) (komma-gescheiden, of leeg): ").strip()
        if not keuze:
            continue

        for part in keuze.split(","):
            part = part.strip()
            if not part.isdigit():
                continue
            idx = int(part) - 1
            if 0 <= idx < len(users):
                user = users[idx]
                if user["selectable"] != "on":
                    print(f"  ⚠️  '{user['name']}' kan niet geselecteerd worden.")
                    continue
                selected.append(user)
                marker = "★" if user["is_co_account"] else " "
                print(f"  ✅ {marker} '{user['name']}' [{user['type_label']}] toegevoegd.")
        print()

    return selected


# ============================================================
# VERZENDEN — XML OPBOUWEN
# ============================================================

def build_receiver_xml(users, receiver_type):
    xml = "<results>"
    for u in users:
        user_id = u['user_id']
        if u.get("is_co_account"):
            if not user_id.startswith("U"):
                user_id = f"U{user_id}"
            user_lt = "2"
        else:
            if user_id.startswith("U"):
                user_id = user_id[1:]
            user_lt = "0"

        xml += "<result>"
        xml += f"<id>{user_id}</id>"
        xml += f"<ssid>{u.get('ss_id', '455')}</ssid>"
        xml += f"<type>{receiver_type}</type>"
        xml += f"<userlt>{user_lt}</userlt>"
        xml += "</result>"
    xml += "</results>"
    return xml


# ============================================================
# VERZENDEN — HOOFDFUNCTIE
# ============================================================

def send_message(session, subject, message,
                 to_users, cc_users=None, bcc_users=None,
                 send_date=None):
    cc_users = cc_users or []
    bcc_users = bcc_users or []

    print("\n[1] Compose-tokens ophalen...")
    tokens = get_compose_tokens(session)
    random_dir = tokens["random_dir"]
    unique_usc = tokens["unique_usc"]
    encrypted_sender = tokens["encrypted_sender"]

    if not random_dir:
        print("    ❌ Kon geen tokens ophalen")
        return False

    print("[2] Ontvangers registreren in sessie...")
    for role_idx, user_list in [(0, to_users), (1, cc_users), (2, bcc_users)]:
        for u in user_list:
            is_co = u.get("is_co_account", False)
            dropped = DROPPED_TYPE[(role_idx, is_co)]
            userlt = "2" if is_co else "0"
            add_user_to_selected(session, u['user_id'], unique_usc, dropped,
                                 ssid=u.get('ss_id', '455'), userlt=userlt)

    print("[3] XML opbouwen...")
    multipart_fields = {
        "send": "send",
        "origMsgID": "0",
        "composeAction": "0",
        "randomDir": random_dir,
        "uniqueUsc": unique_usc,
        "showTab": "tab1Container",
        "delFile": "0",
        "composeType": "0",
        "msgID": "0",
        "msgFormSelectedTab": "",
        "sendDate": send_date or "",
        "subject": subject,
        "bcc": "0",
        "message": f"<p>{message}</p>",
        "encryptedSender": encrypted_sender,
    }

    if to_users:
        multipart_fields["receiverPart0"] = build_receiver_xml(to_users, 0)
        print(f"    Aan ({len(to_users)}):")
        for u in to_users:
            marker = "★" if u.get("is_co_account") else " "
            print(f"       {marker} {u['name']}")

    if cc_users:
        multipart_fields["receiverPart1"] = build_receiver_xml(cc_users, 1)
        print(f"    CC ({len(cc_users)}):")
        for u in cc_users:
            marker = "★" if u.get("is_co_account") else " "
            print(f"       {marker} {u['name']}")

    if bcc_users:
        multipart_fields["receiverPart2"] = build_receiver_xml(bcc_users, 2)
        print(f"    BCC ({len(bcc_users)}):")
        for u in bcc_users:
            marker = "★" if u.get("is_co_account") else " "
            print(f"       {marker} {u['name']}")

    print("[4] Bericht verzenden...")
    m = MultipartEncoder(fields=multipart_fields)
    response = session.request(
        "POST",
        "/?module=Messages&file=composeMessage&boxType=inbox&composeType=0&msgID=undefined",
        data=m,
        headers={
            "Content-Type": m.content_type,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/",
        },
        allow_redirects=True
    )
    print(f"    Status: {response.status_code}")
    if response.status_code == 200:
        if send_date:
            print(f"    ✅ INGEPLAND voor {send_date}!")
        else:
            print("    ✅ VERZONDEN!")
        return True
    print("    ❌ Mislukt")
    return False


# ============================================================
# DATUM PARSER
# ============================================================

def parse_send_date(user_input):
    user_input = user_input.strip().lower()
    if not user_input:
        return None
    now = datetime.now()
    rel_match = re.match(r"^\+?(\d+)([hmd])$", user_input)
    if rel_match:
        amount = int(rel_match.group(1))
        unit = rel_match.group(2)
        if unit == "h":
            target = now + timedelta(hours=amount)
        elif unit == "m":
            target = now + timedelta(minutes=amount)
        else:
            target = now + timedelta(days=amount)
        return target.strftime("%Y-%m-%d %H:%M")

    if user_input.startswith("morgen"):
        parts = user_input.split()
        time_str = parts[1] if len(parts) > 1 else "09:00"
        target = (now + timedelta(days=1)).replace(hour=0, minute=0)
        try:
            h, m = time_str.split(":")
            target = target.replace(hour=int(h), minute=int(m))
        except Exception:
            target = target.replace(hour=9, minute=0)
        return target.strftime("%Y-%m-%d %H:%M")

    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
                "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M"]:
        try:
            return datetime.strptime(user_input, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return user_input


# ============================================================
# NIEUW BERICHT FLOW
# ============================================================

def new_message_flow(session):
    print("\n" + "=" * 60)
    print("NIEUW BERICHT")
    print("=" * 60)

    subject = input("Onderwerp: ").strip()
    message = input("Bericht: ").strip()
    if not subject or not message:
        print("Onderwerp en bericht zijn verplicht.")
        return

    tokens = get_compose_tokens(session)
    unique_usc = tokens["unique_usc"]

    to_users = select_users_interactive(session, unique_usc, "Aan", search_types=(0, 1))
    if not to_users:
        print("\n❌ Geen ontvangers geselecteerd.")
        return

    cc_users = []
    if input("\nCC ontvangers toevoegen? (ja/nee): ").strip().lower() in ("ja","j","yes","y"):
        cc_users = select_users_interactive(session, unique_usc, "CC", search_types=(2, 4))

    bcc_users = []
    if input("\nBCC ontvangers toevoegen? (ja/nee): ").strip().lower() in ("ja","j","yes","y"):
        bcc_users = select_users_interactive(session, unique_usc, "BCC", search_types=(3, 5))

    print("\nWanneer verzenden? (leeg = direct, of '45m', '2h', 'morgen 14:00', '2026-10-01 09:30')")
    date_input = input("Datum/tijd: ").strip()
    send_date = parse_send_date(date_input) if date_input else None

    # Samenvatting
    print("\n" + "=" * 60)
    print("SAMENVATTING")
    print("=" * 60)
    print(f"Onderwerp : {subject}")
    print(f"Aan       :")
    for u in to_users:
        print(f"            {'★' if u['is_co_account'] else ' '} {u['name']}")
    if cc_users:
        print(f"CC        :")
        for u in cc_users:
            print(f"            {'★' if u['is_co_account'] else ' '} {u['name']}")
    if bcc_users:
        print(f"BCC       :")
        for u in bcc_users:
            print(f"            {'★' if u['is_co_account'] else ' '} {u['name']}")
    print(f"Datum     : {send_date or 'direct'}")

    if input("\nVerzenden? (ja/nee): ").strip().lower() not in ("ja","j","yes","y"):
        print("Geannuleerd.")
        return

    send_message(session, subject, message, to_users, cc_users, bcc_users, send_date)


# ============================================================
# MAIN MENU
# ============================================================

def main():
    session = Smartschool(EnvCredentials())
    _ = session.platform_id

    while True:
        print("\n" + "=" * 60)
        print("SMARTSCHOOL BERICHTEN")
        print("=" * 60)
        print("  [1] Berichten lezen")
        print("  [2] Nieuw bericht versturen")
        print("  [3] Stoppen")

        keuze = input("\nKeuze: ").strip()

        if keuze == "1":
            read_messages_flow(session)
        elif keuze == "2":
            new_message_flow(session)
        elif keuze == "3":
            print("Tot ziens!")
            break
        else:
            print("Ongeldige keuze.")


def read_messages_flow(session):
    print("\nWelke map?")
    print("  [1] Inbox  [2] Outbox  [3] Prullenbak  [4] Archief")
    box_map = {"1": ("inbox","Inbox"), "2": ("outbox","Outbox"),
               "3": ("trash","Prullenbak"), "4": ("archive","Archief")}
    box_keuze = input("Keuze [1]: ").strip() or "1"
    box_type, box_label = box_map.get(box_keuze, ("inbox","Inbox"))

    print(f"\n{box_label} ophalen...\n")
    messages = get_message_list(session, box_type=box_type)
    if not messages:
        print("Geen berichten gevonden.")
        return

    print(f"{len(messages)} berichten gevonden\n")
    for msg in messages[:20]:
        att = "[B]" if msg["attachment"] == "1" else "   "
        unr = "●" if msg["unread"] == "1" else " "
        print(f"{unr} {att} [{msg['id']}] {msg['from']}: {msg['subject']}")

    keuze = input("\nBericht-ID (of leeg): ").strip()
    if not keuze:
        return

    target = next((m for m in messages if m["id"] == keuze), None)
    if not target:
        print("Niet gevonden.")
        return

    xml_text = get_message_full(session, target["id"], target["real_box"] or box_type)
    parsed = parse_full_message(xml_text)

    print(f"\n{'=' * 60}")
    print(f"{parsed['subject']}")
    print(f"Van: {parsed['sender']}")
    print(f"Datum: {parsed['date']}")
    print(f"{'=' * 60}\n{parsed['body_text']}\n")

    if parsed["attachments"]:
        print(f"BIJLAGEN ({len(parsed['attachments'])}):")
        for i, att in enumerate(parsed["attachments"], 1):
            print(f"   [{i}] {att['name']} ({att['mime']}, {att['size']})")
        dl = input("\nBijlage downloaden? (nummer of leeg): ").strip()
        if dl:
            try:
                idx = int(dl) - 1
                att = parsed["attachments"][idx]
                if att["file_id"]:
                    save_path = att["name"].replace("/","_").replace("\\","_")
                    download_attachment(session, att["file_id"], save_path)
            except (ValueError, IndexError):
                print("Ongeldige keuze.")

    print("\nACTIES:")
    print("  [1] Markeer ongelezen  [2] Archiveer  [3] Verwijder")
    print("  [4] Rood  [5] Groen  [6] Geel  [7] Blauw  [8] Geen label")
    print("  [9] Niets")
    actie = input("Keuze: ").strip()

    if actie == "1":
        mark_unread(session, target["id"])
    elif actie == "2":
        archive_message(session, target["id"])
    elif actie == "3":
        if input("Zeker? (ja/nee): ").strip().lower() in ("ja","j","yes","y"):
            trash_message(session, target["id"])
    elif actie == "4":
        apply_label(session, target["id"], MessageLabel.RED_FLAG)
    elif actie == "5":
        apply_label(session, target["id"], MessageLabel.GREEN_FLAG)
    elif actie == "6":
        apply_label(session, target["id"], MessageLabel.YELLOW_FLAG)
    elif actie == "7":
        apply_label(session, target["id"], MessageLabel.BLUE_FLAG)
    elif actie == "8":
        apply_label(session, target["id"], MessageLabel.NO_FLAG)
    else:
        print("Geen actie.")


if __name__ == "__main__":
    main()
