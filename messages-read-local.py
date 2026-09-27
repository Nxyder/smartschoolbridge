import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from smartschool import Smartschool, EnvCredentials

# ============================================
# CONFIGURATIE - VUL DIT IN
# ============================================
os.environ["SMARTSCHOOL_USERNAME"] = "odne.bontemps"
os.environ["SMARTSCHOOL_PASSWORD"] = "JOUW_WACHTWOORD"
os.environ["SMARTSCHOOL_MAIN_URL"] = "stjozefasoessen.smartschool.be"
os.environ["SMARTSCHOOL_MFA"] = "2011-07-27"
# ============================================


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
        "Origin": "https://stjozefasoessen.smartschool.be",
        "Referer": "https://stjozefasoessen.smartschool.be/",
    }

    return session.request(
        method="POST",
        url="/?module=Messages&file=dispatcher",
        data=body,
        headers=headers,
    )


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
    text = text.replace("&#235;", "ë")
    return text.strip()


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
        "id": "",
        "subject": "",
        "sender": "",
        "to": "",
        "cc": "",
        "date": "",
        "body_html": "",
        "body_text": "",
        "attachments": [],
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


def download_attachment(session, file_id, save_path=None):
    url = f"/?module=Messages&file=download&fileID={file_id}&target=0"

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en,nl;q=0.9",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
        "Referer": "https://stjozefasoessen.smartschool.be/",
    }

    print(f"   Download: {url}")
    response = session.request(method="GET", url=url, headers=headers, allow_redirects=True)

    if response.status_code != 200:
        print(f"   Status: {response.status_code}")
        return None

    content_type = response.headers.get("content-type", "")
    content_length = len(response.content)

    print(f"   Status: {response.status_code}")
    print(f"   Content-Type: {content_type}")
    print(f"   Grootte: {content_length} bytes")

    if save_path:
        with open(save_path, "wb") as f:
            f.write(response.content)
        print(f"   Opgeslagen: {save_path}")

    return response.content


def main():
    session = Smartschool(EnvCredentials())
    _ = session.platform_id

    print("Berichten ophalen...\n")
    messages = get_message_list(session)
    print(f"{len(messages)} berichten gevonden\n")

    for msg in messages[:15]:
        attachment = "[B]" if msg["attachment"] == "1" else "   "
        print(f"{attachment} [{msg['id']}] {msg['subject']}")

    print()
    keuze = input("Welk bericht wil je openen? (ID): ").strip()

    target = None
    for msg in messages:
        if msg["id"] == keuze:
            target = msg
            break

    if not target:
        print(f"Bericht {keuze} niet gevonden.")
        return

    xml_text = get_message_full(session, target["id"], target["real_box"] or "inbox")
    parsed = parse_full_message(xml_text)

    print(f"\n{parsed['subject']}")
    print(f"Van: {parsed['sender']}")
    print(f"Datum: {parsed['date']}")
    print(f"\nBODY:\n")
    print(parsed["body_text"])

    if parsed["attachments"]:
        print(f"\nBIJLAGEN ({len(parsed['attachments'])}):")
        for i, att in enumerate(parsed["attachments"], 1):
            print(f"   [{i}] {att['name']} ({att['mime']}, {att['size']})")

        print()
        dl = input("Bijlage downloaden? (nummer of leeg): ").strip()

        if dl:
            try:
                idx = int(dl) - 1
                att = parsed["attachments"][idx]

                if not att["file_id"]:
                    print("Deze bijlage heeft geen geldig fileID.")
                    return

                print(f"\nDownloaden: {att['name']}")
                save_path = att["name"].replace("/", "_").replace("\\", "_")
                content = download_attachment(session, att["file_id"], save_path)

                if content is None:
                    print("\nDownload mislukt.")

            except (ValueError, IndexError):
                print("Ongeldige keuze.")


if __name__ == "__main__":
    main()
