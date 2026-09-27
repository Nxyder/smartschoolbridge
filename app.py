import os
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


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/planner', methods=['GET'])
def get_planner():
    username = request.args.get('username')
    password = request.args.get('password')
    main_url = request.args.get('main_url')
    mfa = request.args.get('mfa')

    start = request.args.get('start', '2026-09-27')
    end = request.args.get('end', '2026-10-31')

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
            items.append({
                "naam": element.name,
                "vakken": [c.name for c in element.courses],
                "deadline": element.period.date_time_to.isoformat()
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
