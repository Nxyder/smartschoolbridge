import os
import re
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from smartschool import Smartschool, Credentials, PlannedElements

app = Flask(__name__)
CORS(app)


class InlineCredentials(Credentials):
    """Credentials die direct uit de URL-parameters komen."""

    def __init__(self, username, password, main_url, mfa):
        self._username = username
        self._password = password
        self._main_url = main_url
        self._mfa = mfa

    @property
    def username(self):
        return self._username

    @username.setter
    def username(self, value):
        self._username = value

    @property
    def password(self):
        return self._password

    @password.setter
    def password(self, value):
        self._password = value

    @property
    def main_url(self):
        return self._main_url

    @main_url.setter
    def main_url(self, value):
        self._main_url = value

    @property
    def mfa(self):
        return self._mfa

    @mfa.setter
    def mfa(self, value):
        self._mfa = value


def strip_html(html):
    """Verwijder HTML-tags voor plain-text weergave."""
    if not html:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    return text.strip()


def get_detail(session, element):
    """Haal de volledige beschrijving op van een gepland element."""
    if element.planned_element_type == "planned-to-dos":
        url = f"/planner/api/v1/planned-to-dos/{element.platform_id}/{element.id}"
    elif element.planned_element_type == "planned-assignments":
        url = f"/planner/api/v1/planned-assignments/{element.platform_id}/{element.id}"
    else:
        return None

    try:
        return session.json(url)
    except Exception:
        return None


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/planner', methods=['GET'])
def get_planner():
    username = request.args.get('username')
    password = request.args.get('password')
    main_url = request.args.get('main_url')
    mfa = request.args.get('mfa')

    if not all([username, password, main_url, mfa]):
        return jsonify({
            "status": "error",
            "message": "Vereiste parameters: username, password, main_url, mfa"
        }), 400

    try:
        creds = InlineCredentials(
            username=username,
            password=password,
            main_url=main_url,
            mfa=mfa,
        )
        session = Smartschool(creds)

        items = []
        for element in PlannedElements(session):
            detail = get_detail(session, element)

            beschrijving = ""
            bijlagen = []
            links = []

            if detail:
                raw_info = detail.get('publicInfo') or ""
                beschrijving = strip_html(raw_info)

                for att in detail.get('attachments') or []:
                    bijlagen.append({
                        "naam": att.get('name') or att.get('fileName') or 'bijlage',
                        "url": att.get('url') or att.get('downloadUrl') or ''
                    })

                for link in detail.get('weblinks') or []:
                    links.append({
                        "naam": link.get('name') or link.get('title') or '',
                        "url": link.get('url') or link.get('href') or ''
                    })

            items.append({
                "naam": element.name,
                "vakken": [c.name for c in element.courses],
                "deadline": element.period.date_time_to.isoformat(),
                "type": element.planned_element_type,
                "beschrijving": beschrijving,
                "beschrijving_html": (detail.get('publicInfo') if detail else "") or "",
                "bijlagen": bijlagen,
                "links": links,
            })

        return jsonify({
            "status": "success",
            "count": len(items),
            "data": items
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
