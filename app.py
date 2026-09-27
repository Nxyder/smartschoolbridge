import os
import re
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from smartschool import Smartschool, Credentials, PlannedElements

app = Flask(__name__)
CORS(app)


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
    if not html:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    return text.strip()


def get_detail(session, element):
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


def build_creds():
    """Haal credentials uit de request (query params of JSON body)."""
    if request.method == 'POST' and request.is_json:
        data = request.get_json() or {}
    else:
        data = request.args
    return InlineCredentials(
        username=data.get('username'),
        password=data.get('password'),
        main_url=data.get('main_url'),
        mfa=data.get('mfa'),
    )


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
        return jsonify({"status": "error", "message": "Vereiste parameters: username, password, main_url, mfa"}), 400

    try:
        session = Smartschool(InlineCredentials(username, password, main_url, mfa))
        items = []
        for element in PlannedElements(session):
            detail = get_detail(session, element)
            beschrijving = ""
            beschrijving_html = ""
            bijlagen = []
            links = []

            if detail:
                beschrijving_html = detail.get('publicInfo') or ""
                beschrijving = strip_html(beschrijving_html)
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
                "id": element.id,
                "platform_id": element.platform_id,
                "naam": element.name,
                "vakken": [c.name for c in element.courses],
                "deadline": element.period.date_time_to.isoformat(),
                "type": element.planned_element_type,
                "status": element.resolved_status,
                "beschrijving": beschrijving,
                "beschrijving_html": beschrijving_html,
                "bijlagen": bijlagen,
                "links": links,
            })

        return jsonify({"status": "success", "count": len(items), "data": items})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def _get_type_prefix(element_type):
    """Bepaal het URL-prefix op basis van het element type."""
    if element_type == "planned-assignments":
        return "planned-assignments"
    if element_type == "planned-to-dos":
        return "planned-to-dos"
    return None


@app.route('/resolve', methods=['POST'])
def resolve_element():
    """Vink een element af (resolve) of maak het ongedaan (unresolve)."""
    creds = build_creds()
    data = request.get_json() if request.is_json else request.args

    element_id = data.get('id')
    platform_id = data.get('platform_id')
    element_type = data.get('type', 'planned-to-dos')
    action = data.get('action', 'resolve')  # 'resolve' of 'unresolve'

    if not all([element_id, platform_id]):
        return jsonify({"status": "error", "message": "Vereist: id, platform_id"}), 400

    prefix = _get_type_prefix(element_type)
    if not prefix:
        return jsonify({"status": "error", "message": f"Onbekend type: {element_type}"}), 400

    url = f"/planner/api/v1/{prefix}/{platform_id}/{element_id}/{action}"

    try:
        session = Smartschool(creds)
        # De library heeft geen publieke PUT methode; gebruik de interne request
        result = session.put(url)
        return jsonify({"status": "success", "action": action, "result": result})

    except AttributeError:
        # Fallback: gebruik .json() met method parameter als die bestaat
        try:
            result = session.json(url, method="PUT")
            return jsonify({"status": "success", "action": action, "result": result})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/trash', methods=['POST'])
def trash_element():
    """Verplaats een element naar de prullenbak."""
    creds = build_creds()
    data = request.get_json() if request.is_json else request.args

    element_id = data.get('id')
    platform_id = data.get('platform_id')
    element_type = data.get('type', 'planned-to-dos')

    if not all([element_id, platform_id]):
        return jsonify({"status": "error", "message": "Vereist: id, platform_id"}), 400

    prefix = _get_type_prefix(element_type)
    if not prefix:
        return jsonify({"status": "error", "message": f"Onbekend type: {element_type}"}), 400

    url = f"/planner/api/v1/{prefix}/{platform_id}/{element_id}/trash"

    try:
        session = Smartschool(creds)
        result = session.put(url)
        return jsonify({"status": "success", "action": "trash", "result": result})

    except AttributeError:
        try:
            result = session.json(url, method="PUT")
            return jsonify({"status": "success", "action": "trash", "result": result})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
