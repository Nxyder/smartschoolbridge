import os
import re
from datetime import datetime, timedelta
from requests_toolbelt.multipart.encoder import MultipartEncoder
from smartschool import Smartschool, EnvCredentials

os.environ["SMARTSCHOOL_USERNAME"] = "odne.bontemps"
os.environ["SMARTSCHOOL_PASSWORD"] = "JOUW_WACHTWOORD"
os.environ["SMARTSCHOOL_MAIN_URL"] = "stjozefasoessen.smartschool.be"
os.environ["SMARTSCHOOL_MFA"] = "2011-07-27"

BASE_URL = "https://stjozefasoessen.smartschool.be"


# ============================================================
# DROPPEDTYPE MAPPING
# ============================================================
# (doel, is_co) -> droppedtype
# doel: 0 = Aan, 1 = CC, 2 = BCC
# is_co: False = normale gebruiker, True = co-account
#
# Uit de HTML:
#   normaal:  Aan=0, CC=2, BCC=3
#   co:       Aan=1, CC=4, BCC=5
DROPPED_TYPE = {
    (0, False): 0,
    (1, False): 2,
    (2, False): 3,
    (0, True):  1,
    (1, True):  4,
    (2, True):  5,
}


# ============================================================
# HELPERS
# ============================================================

def clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract(block, tag, default=""):
    match = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.DOTALL)
    if match:
        return match.group(1).strip()
    return default


# ============================================================
# TOKENS
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
# GEBRUIKERS ZOEKEN
# ============================================================
# search_type is de UI-index:
#   0 = Aan (normaal)
#   1 = Aan (co)
#   2 = Kopie (normaal)
#   3 = Blinde kopie (normaal)
#   4 = Kopie (co)
#   5 = Blinde kopie (co)

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
            "user_id": user_id,
            "name": display_name,
            "raw_name": name,
            "user_type": user_type,
            "user_lt": user_lt,
            "coaccount_name": coaccount_name,
            "is_co_account": is_co,
            "selectable": selectable,
            "ss_id": ss_id,
            "classname": classname,
            "type_label": label,
        })
    return users


# ============================================================
# ADD USER TO SESSION
# ============================================================

def add_user_to_selected(session, user_id, unique_usc, dropped_type,
                         ssid="455", userlt="0", type_id="users"):
    """
    Roep addUserToSelected aan met het juiste droppedtype.

    dropped_type komt uit DROPPED_TYPE:
      0 = Aan normaal
      1 = Aan co
      2 = CC normaal
      3 = BCC normaal
      4 = CC co
      5 = BCC co
    """
    parent_node_id = f"insertSearchFieldContainer_{dropped_type}_0"
    data = {
        "id": user_id,
        "typeId": type_id,
        "type": str(dropped_type),
        "parentNodeId": parent_node_id,
        "ssid": ssid,
        "userlt": userlt,
        "uniqueUsc": unique_usc,
    }
    r = session.request(
        "POST",
        "/?module=Messages&file=searchUsers&function=addUserToSelected",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest",
                 "Origin": BASE_URL,
                 "Referer": BASE_URL + "/"}
    )
    return r.text


# ============================================================
# INTERACTIEVE SELECTIE (met co-accounts in alle velden)
# ============================================================

def select_users_interactive(session, unique_usc, role_label="Aan",
                             search_types=(0,)):
    """
    search_types: tuple van UI-indices om in te zoeken.
    Voor Aan:  (0, 1)   -> normaal + co
    Voor CC:   (2, 4)   -> normaal + co
    Voor BCC:  (3, 5)   -> normaal + co
    """
    selected = []
    print(f"\n--- {role_label} ontvangers toevoegen ---")
    print("(typ een zoekterm, of leeg om te stoppen)\n")

    while True:
        query = input(f"[{role_label}] Zoek (leeg = stoppen): ").strip()
        if not query:
            break

        # Zoek in alle opgegeven containers en voeg resultaten samen
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
                print(f"  ✅ {marker} '{user['name']}' [{user['type_label']}] toegevoegd aan {role_label}.")
        print()

    return selected


# ============================================================
# RECEIVER XML
# ============================================================
# receiver_type: 0 = Aan, 1 = CC, 2 = BCC (de <type> tag in de XML)

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
# BERICHT VERZENDEN
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

    # ==== STAP 2: addUserToSelected voor alle ontvangers ====
    print("[2] Ontvangers registreren in sessie...")

    # Aan
    for u in to_users:
        is_co = u.get("is_co_account", False)
        dropped = DROPPED_TYPE[(0, is_co)]
        userlt = "2" if is_co else "0"
        add_user_to_selected(session, u['user_id'], unique_usc, dropped,
                             ssid=u.get('ss_id', '455'), userlt=userlt)

    # CC
    for u in cc_users:
        is_co = u.get("is_co_account", False)
        dropped = DROPPED_TYPE[(1, is_co)]
        userlt = "2" if is_co else "0"
        add_user_to_selected(session, u['user_id'], unique_usc, dropped,
                             ssid=u.get('ss_id', '455'), userlt=userlt)

    # BCC
    for u in bcc_users:
        is_co = u.get("is_co_account", False)
        dropped = DROPPED_TYPE[(2, is_co)]
        userlt = "2" if is_co else "0"
        add_user_to_selected(session, u['user_id'], unique_usc, dropped,
                             ssid=u.get('ss_id', '455'), userlt=userlt)

    # ==== STAP 3: XML opbouwen ====
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
            print(f"       {marker} {u['name']} [{u['type_label']}]")

    if cc_users:
        multipart_fields["receiverPart1"] = build_receiver_xml(cc_users, 1)
        print(f"    CC ({len(cc_users)}):")
        for u in cc_users:
            marker = "★" if u.get("is_co_account") else " "
            print(f"       {marker} {u['name']} [{u['type_label']}]")

    if bcc_users:
        multipart_fields["receiverPart2"] = build_receiver_xml(bcc_users, 2)
        print(f"    BCC ({len(bcc_users)}):")
        for u in bcc_users:
            marker = "★" if u.get("is_co_account") else " "
            print(f"       {marker} {u['name']} [{u['type_label']}]")

    # ==== STAP 4: Verzenden ====
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
    print(f"    URL: {response.url}")

    if response.status_code == 200:
        if send_date:
            print(f"    ✅ INGEPLAND voor {send_date}!")
        else:
            print("    ✅ VERZONDEN!")
        return True
    else:
        print("    ❌ Verzenden mislukt")
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
        elif unit == "d":
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

    formats = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
               "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M"]
    for fmt in formats:
        try:
            target = datetime.strptime(user_input, fmt)
            return target.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue

    return user_input


# ============================================================
# MAIN
# ============================================================

def main():
    session = Smartschool(EnvCredentials())
    _ = session.platform_id

    print("=" * 60)
    print("SMARTSCHOOL BERICHT VERZENDEN")
    print("=" * 60)
    print()

    subject = input("Onderwerp: ").strip()
    message = input("Bericht: ").strip()

    if not subject or not message:
        print("Onderwerp en bericht zijn verplicht.")
        return

    tokens = get_compose_tokens(session)
    unique_usc = tokens["unique_usc"]

    # Aan: normaal (0) + co (1)
    to_users = select_users_interactive(
        session, unique_usc, role_label="Aan",
        search_types=(0, 1),
    )
    if not to_users:
        print("\n❌ Geen ontvangers geselecteerd.")
        return

    # CC: normaal (2) + co (4)
    cc_keuze = input("\nCC ontvangers toevoegen? (ja/nee): ").strip().lower()
    cc_users = []
    if cc_keuze in ("ja", "j", "yes", "y"):
        cc_users = select_users_interactive(
            session, unique_usc, role_label="CC",
            search_types=(2, 4),
        )

    # BCC: normaal (3) + co (5)
    bcc_keuze = input("\nBCC ontvangers toevoegen? (ja/nee): ").strip().lower()
    bcc_users = []
    if bcc_keuze in ("ja", "j", "yes", "y"):
        bcc_users = select_users_interactive(
            session, unique_usc, role_label="BCC",
            search_types=(3, 5),
        )

    # Datum
    print("\nWanneer verzenden?")
    print("  - Leeg = direct")
    print("  - '45m', '2h', 'morgen 14:00', '2026-10-01 09:30'")
    date_input = input("Datum/tijd: ").strip()
    send_date = parse_send_date(date_input) if date_input else None

    # Samenvatting
    print("\n" + "=" * 60)
    print("SAMENVATTING")
    print("=" * 60)
    print(f"Onderwerp : {subject}")

    print(f"Aan       :")
    for u in to_users:
        marker = "★" if u.get("is_co_account") else " "
        print(f"            {marker} {u['name']} [{u['type_label']}]")

    if cc_users:
        print(f"CC        :")
        for u in cc_users:
            marker = "★" if u.get("is_co_account") else " "
            print(f"            {marker} {u['name']} [{u['type_label']}]")

    if bcc_users:
        print(f"BCC       :")
        for u in bcc_users:
            marker = "★" if u.get("is_co_account") else " "
            print(f"            {marker} {u['name']} [{u['type_label']}]")

    print(f"Datum     : {send_date or 'direct'}")

    bevestig = input("\nVerzenden? (ja/nee): ").strip().lower()
    if bevestig not in ("ja", "j", "yes", "y"):
        print("Geannuleerd.")
        return

    send_message(
        session, subject, message,
        to_users=to_users, cc_users=cc_users, bcc_users=bcc_users,
        send_date=send_date,
    )


if __name__ == "__main__":
    main()
