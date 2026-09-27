import os
from datetime import datetime

from smartschool import Smartschool, Credentials, PlannedElements

os.environ.update(
    SMARTSCHOOL_USERNAME="odne.bontemps",
    SMARTSCHOOL_PASSWORD="JOUW_WACHTWOORD",
    SMARTSCHOOL_MAIN_URL="stjozefasoessen.smartschool.be",
    SMARTSCHOOL_MFA="2011-07-27",
)


class InlineCredentials(Credentials):
    def __init__(self, username, password, main_url, mfa):
        self._username = username
        self._password = password
        self._main_url = main_url
        self._mfa = mfa

    @property
    def username(self): return self._username
    @username.setter
    def username(self, v): self._username = v

    @property
    def password(self): return self._password
    @password.setter
    def password(self, v): self._password = v

    @property
    def main_url(self): return self._main_url
    @main_url.setter
    def main_url(self, v): self._main_url = v

    @property
    def mfa(self): return self._mfa
    @mfa.setter
    def mfa(self, v): self._mfa = v


def strip_html(html):
    import re
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


def element_action(session, element, action):
    if element.planned_element_type == "planned-to-dos":
        prefix = "planned-to-dos"
    elif element.planned_element_type == "planned-assignments":
        prefix = "planned-assignments"
    else:
        return False

    path = f"/planner/api/v1/{prefix}/{element.platform_id}/{element.id}/{action}"
    try:
        r = session.request(method="POST", url=path)
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"   ❌ {e}")
        return False


def create_todo(session, platform_id=455):
    print("\n--- Nieuwe to-do ---")
    name = input("Naam: ").strip()
    if not name:
        return
    description = input("Beschrijving (leeg = geen): ").strip()
    color = input("Kleur [tangerine-200]: ").strip() or "tangerine-200"
    icon = input("Icon [icon_fill_flag]: ").strip() or "icon_fill_flag"

    date_from = input("Datum van (YYYY-MM-DD, leeg = vandaag): ").strip()
    date_to = input("Datum tot (YYYY-MM-DD, leeg = zelfde dag): ").strip()

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
            "wholeDay": True,
        },
    }

    try:
        r = session.request(method="POST", url=f"/planner/api/v1/planned-to-dos/", json=body)
        if r.status_code in (200, 201):
            print(f"   ✅ Aangemaakt (status {r.status_code})")
        else:
            print(f"   ❌ Status {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"   ❌ {e}")


def show_element(session, element, with_detail=False):
    p = element.period
    date_from = p.date_time_from.strftime("%d/%m %H:%M") if p.date_time_from else "?"
    date_to = p.date_time_to.strftime("%d/%m %H:%M") if p.date_time_to else "?"
    courses = ", ".join(c.name for c in element.courses) if element.courses else "-"
    status = element.resolved_status or "-"

    print(f"  [{element.platform_id}/{element.id}]")
    print(f"    {element.name}")
    print(f"    Type: {element.planned_element_type}")
    print(f"    Vak: {courses}")
    print(f"    Van: {date_from}   Tot: {date_to}")
    print(f"    Status: {status}   Pinned: {element.pinned}")

    if with_detail:
        detail = get_detail(session, element)
        if detail:
            info = strip_html(detail.get("publicInfo") or "")
            if info:
                print(f"    Info: {info}")
            att = detail.get("attachments") or []
            if att:
                print(f"    Bijlagen: {len(att)}")
                for a in att:
                    print(f"      - {a.get('name', '?')}")
            links = detail.get("weblinks") or []
            if links:
                for l in links:
                    print(f"    Link: {l.get('url', l)}")
    print()


def list_planner(session, with_detail=False):
    print("\nPlanner ophalen...\n")
    elements = list(PlannedElements(session))
    if not elements:
        print("Geen items.")
        return []

    print(f"{len(elements)} items:\n")
    for el in elements:
        show_element(session, el, with_detail=with_detail)
    return elements


def main():
    session = Smartschool(InlineCredentials(
        username=os.environ["SMARTSCHOOL_USERNAME"],
        password=os.environ["SMARTSCHOOL_PASSWORD"],
        main_url=os.environ["SMARTSCHOOL_MAIN_URL"],
        mfa=os.environ["SMARTSCHOOL_MFA"],
    ))
    _ = session.platform_id

    while True:
        print("=" * 60)
        print("SMARTSCHOOL PLANNER")
        print("=" * 60)
        print("  [1] Planner bekijken (samenvatting)")
        print("  [2] Planner bekijken (met details)")
        print("  [3] Item resolven (gedaan)")
        print("  [4] Item unresolven")
        print("  [5] Item verwijderen")
        print("  [6] Nieuwe to-do aanmaken")
        print("  [7] Stoppen")

        keuze = input("\nKeuze: ").strip()

        if keuze == "7":
            print("Tot ziens!")
            return

        if keuze == "1":
            list_planner(session, with_detail=False)

        elif keuze == "2":
            list_planner(session, with_detail=True)

        elif keuze in ("3", "4", "5"):
            elements = list_planner(session, with_detail=False)
            if not elements:
                continue
            idx = input("Nummer in de lijst (0-based) of leeg: ").strip()
            if not idx.isdigit():
                continue
            i = int(idx)
            if not (0 <= i < len(elements)):
                print("Ongeldig nummer.")
                continue
            el = elements[i]
            action = {"3": "resolve", "4": "unresolve", "5": "trash"}[keuze]
            if element_action(session, el, action):
                print(f"   ✅ {action} uitgevoerd")
            else:
                print(f"   ❌ Mislukt")

        elif keuze == "6":
            create_todo(session)

        else:
            print("Ongeldige keuze.")


if __name__ == "__main__":
    main()
