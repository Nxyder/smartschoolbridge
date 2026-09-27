import os, json
from collections import defaultdict
from smartschool import Smartschool, EnvCredentials

os.environ.update(
    SMARTSCHOOL_USERNAME="odne.bontemps",
    SMARTSCHOOL_PASSWORD="JOUW_WACHTWOORD",
    SMARTSCHOOL_MAIN_URL="stjozefasoessen.smartschool.be",
    SMARTSCHOOL_MFA="2011-07-27",
)

BASE = "/results/api/v1"

RESET = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"
COLORS = {
    "red": "\033[91m", "orange": "\033[93m", "yellow": "\033[93m",
    "green": "\033[92m", "blue": "\033[94m",
}


def c(color):
    return COLORS.get(color, "")


def get(session, path):
    r = session.request(
        "GET", path,
        headers={
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Referer": "https://stjozefasoessen.smartschool.be/",
        },
    )
    return r.json()


def fetch_all_evals(session):
    all_items = []
    page = 1
    while True:
        batch = get(session, f"{BASE}/evaluations/?pageNumber={page}&itemsOnPage=200")
        if not batch:
            break
        all_items.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return all_items


def fetch_eval_detail(session, identifier):
    """Haal volledige detail op (inclusief centralTendencies)."""
    try:
        return get(session, f"{BASE}/evaluations/{identifier}/")
    except Exception:
        return None


def clean_html(s):
    import re
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</(p|div|li)>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&quot;",'"'),
                 ("&#39;","'"),("&nbsp;"," "),("&#8364;","€")]:
        s = s.replace(a, b)
    return s.strip()


def extract_comments(data):
    """Verzamel alle commentaar uit alle mogelijke velden."""
    comments = []
    if not isinstance(data, dict):
        return comments

    for key in ["feedback", "feedbacks", "comment", "comments",
                "remark", "remarks", "note", "notes", "publicInfo"]:
        val = data.get(key)
        if not val:
            continue
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    txt = item.get("text") or item.get("comment") or item.get("value") or item.get("description") or ""
                    if txt:
                        comments.append(clean_html(str(txt)))
                elif isinstance(item, str):
                    comments.append(clean_html(item))
        elif isinstance(val, str):
            comments.append(clean_html(val))

    return [c for c in dict.fromkeys(comments) if c]


def print_eval(e, full=None, indent="    "):
    g = e.get("graphic", {})
    score = g.get("description", "?")
    pct = g.get("value", "?")
    color = c(g.get("color", ""))
    date = (e.get("date") or "")[:10]
    comp = e.get("component", {})
    comp_str = comp.get("abbreviation", "")
    period = e.get("period", {}).get("name", "")
    counts = "✓" if e.get("doesCount") else "✗"

    teacher = e.get("gradebookOwner", {}).get("name", {})
    teacher_name = teacher.get("startingWithLastName") or "?"
    courses = e.get("courses", [])
    vak = courses[0]["name"] if courses else "?"

    print(f"{indent}{color}{score:>8}{RESET}  ({pct}%)  {date}  "
          f"{vak}  [{comp_str}]  {period}  telt mee: {counts}")
    print(f"{indent}    {DIM}{e['name']}{RESET}  — {teacher_name}")

    # Klasgemiddelde uit detail
    if full:
        ct = (full.get("details") or {}).get("centralTendencies") or []
        for t in ct:
            g2 = t.get("graphic", {})
            type_label = {"average": "Klasgemiddelde", "median": "Mediaan"}.get(t.get("type"), t.get("type"))
            print(f"{indent}    {type_label}: {g2.get('description', '?')} ({g2.get('value', '?')}%)")

    # Commentaar
    comments = extract_comments(e)
    if full:
        comments += [cm for cm in extract_comments(full) if cm not in comments]

    if comments:
        print(f"{indent}    {BOLD}Commentaar:{RESET}")
        for cm in comments:
            for line in cm.split("\n"):
                if line.strip():
                    print(f"{indent}      {line}")


def main():
    session = Smartschool(EnvCredentials())
    _ = session.platform_id

    print("Ophalen...")
    courses = get(session, f"{BASE}/courses/")
    periods = get(session, f"{BASE}/periods/")
    evals = fetch_all_evals(session)

    course_names = {c["id"]: c["name"] for c in courses if c.get("name") != "Totaal"}

    print(f"\n{BOLD}=== CIJFERS ==={RESET}")
    print(f"{DIM}{len(evals)} evaluaties, {len(course_names)} vakken{RESET}\n")

    per_course = defaultdict(list)
    for e in evals:
        for cv in e.get("courses", []):
            per_course[cv["id"]].append(e)

    for cid, items in sorted(per_course.items(),
                             key=lambda x: course_names.get(x[0], "")):
        naam = course_names.get(cid, f"Vak {cid}")
        print(f"\n{BOLD}━━━ {naam} ━━━{RESET}")

        for e in sorted(items, key=lambda x: x.get("date", ""), reverse=True):
            # Detail ophalen voor klasgemiddelde
            full = fetch_eval_detail(session, e["identifier"])
            print_eval(e, full)
            print()


if __name__ == "__main__":
    main()
