import os, re
from smartschool import Smartschool, EnvCredentials

os.environ.update(
    SMARTSCHOOL_USERNAME="odne.bontemps",
    SMARTSCHOOL_PASSWORD="JOUW_WACHTWOORD",
    SMARTSCHOOL_MAIN_URL="stjozefasoessen.smartschool.be",
    SMARTSCHOOL_MFA="2011-07-27",
)

BASE_URL = "https://stjozefasoessen.smartschool.be"
PATH = "/?module=Profile&file=personalia&function=personalia"


def parse_inputs(html):
    """Parse alle <input>-tags onafhankelijk van attribuut-volgorde."""
    fields = {}
    for tag in re.findall(r"<input\b[^>]*>", html, re.IGNORECASE):
        attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', tag))
        name = attrs.get("name")
        if not name:
            continue
        value = attrs.get("value", "")
        # Onthoud ook of het readonly is
        fields[name] = {
            "value": value,
            "readonly": "readonly" in tag.lower(),
            "type": attrs.get("type", "text"),
        }
    return fields


def get_current(session):
    r = session.request("GET", PATH)
    return parse_inputs(r.text), r.text


def update(session, changes):
    fields, html = get_current(session)

    form = {"action": "store"}

    for name, info in fields.items():
        if name == "action":
            continue
        form[name] = info["value"]

    # Overschrijf
    for k, v in changes.items():
        if k in fields and fields[k]["readonly"]:
            print(f"  ⚠️  '{k}' is readonly, kan niet gewijzigd worden")
            continue
        form[k] = v

    r = session.request(
        "POST", PATH,
        data=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": BASE_URL,
            "Referer": BASE_URL + PATH,
        },
        allow_redirects=True,
    )
    return r


def main():
    session = Smartschool(EnvCredentials())
    _ = session.platform_id

    print("=== Huidige gegevens ===")
    fields, _ = get_current(session)
    for name, info in fields.items():
        if name.startswith("_"):
            continue
        ro = " (readonly)" if info["readonly"] else ""
        print(f"  {name}: {info['value']!r}{ro}")

    print("\n=== Aanpassen ===")
    changes = {}
    for name, info in fields.items():
        if name.startswith("_") or name == "action" or info["readonly"]:
            continue
        val = input(f"{name} [{info['value']}]: ").strip()
        if val:
            changes[name] = val

    if not changes:
        print("Niets te wijzigen.")
        return

    print("\n=== Wijzigingen ===")
    for k, v in changes.items():
        print(f"  {k}: {fields[k]['value']!r} → {v!r}")

    if input("\nOpslaan? (ja/nee): ").strip().lower() not in ("ja", "j", "yes", "y"):
        return

    r = update(session, changes)
    print(f"\nStatus: {r.status_code}")
    print(f"URL: {r.url}")

    # Controleer
    new_fields, _ = get_current(session)
    ok = all(new_fields.get(k, {}).get("value") == v for k, v in changes.items())
    if ok:
        print("✅ Opgeslagen!")
    else:
        print("⚠️  Niet alle wijzigingen doorgevoerd:")
        for k, v in changes.items():
            got = new_fields.get(k, {}).get("value", "")
            print(f"    {k}: verwacht {v!r}, kreeg {got!r}")


if __name__ == "__main__":
    main()
