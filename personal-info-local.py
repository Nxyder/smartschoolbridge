import os, re, json
from smartschool import Smartschool, EnvCredentials

os.environ.update(
    SMARTSCHOOL_USERNAME="odne.bontemps",
    SMARTSCHOOL_PASSWORD="JOUW_WACHTWOORD",
    SMARTSCHOOL_MAIN_URL="stjozefasoessen.smartschool.be",
    SMARTSCHOOL_MFA="2011-07-27",
)

def get_personalia(session):
    r = session.request("GET", "/?module=Profile&file=personalia&function=personalia")
    html = r.text

    # Alle inputvelden met name + value
    fields = {}
    for m in re.finditer(
        r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"',
        html, re.IGNORECASE
    ):
        name, value = m.group(1), m.group(2)
        if name.startswith("_") or name == "action":
            continue
        fields[name] = value

    # Reset als een veld eerder zonder value stond
    for m in re.finditer(r'<input[^>]+name="([^"]+)"[^>]*>', html, re.IGNORECASE):
        name = m.group(1)
        if name not in fields and not name.startswith("_"):
            fields[name] = ""

    return fields

if __name__ == "__main__":
    session = Smartschool(EnvCredentials())
    _ = session.platform_id

    profile = get_personalia(session)

    print(json.dumps(profile, indent=2, ensure_ascii=False))
