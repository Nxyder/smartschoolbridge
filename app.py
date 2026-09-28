"""
Smartschool API — Flask app

Alleen basis: CORS, health, ping, en blueprint-registratie.
Alle logica zit in modules/.
"""

from flask import Flask, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

from modules import auth, grades, messages, planner, profile


def create_app():
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # ============================================================
    # CORS
    # ============================================================
    @app.before_request
    def handle_preflight():
        if request.method == "OPTIONS":
            response = app.make_default_options_response()
            origin = request.headers.get("Origin")
            response.headers["Access-Control-Allow-Origin"] = origin or "*"
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PATCH, PUT, DELETE, OPTIONS"
            )
            response.headers["Access-Control-Allow-Headers"] = (
                request.headers.get("Access-Control-Request-Headers")
                or "Content-Type, Authorization, X-Requested-With, X-SS-Creds"
            )
            response.headers["Access-Control-Max-Age"] = "86400"
            response.headers["Vary"] = "Origin"
            return response

    @app.after_request
    def add_cors(response):
        origin = request.headers.get("Origin")
        response.headers["Access-Control-Allow-Origin"] = origin or "*"
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, PATCH, PUT, DELETE, OPTIONS"
        )
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-Requested-With, X-SS-Creds"
        )
        response.headers["Access-Control-Max-Age"] = "86400"
        response.headers["Vary"] = "Origin"
        return response

    # ============================================================
    # HEALTH / PING
    # ============================================================
    @app.get("/")
    def index():
        return jsonify({
            "service": "smartschool-api",
            "status": "ok",
            "endpoints": {
                "auth": "/api/auth/login",
                "grades": "/api/grades",
                "messages": "/api/messages",
                "planner": "/api/planner",
                "profile": "/api/profile",
                "files": "/files/<name>",
                "upload": "/api/upload",
                "upload_smartschool": "/api/upload-smartschool",
            },
        })

    @app.get("/ping")
    def ping():
        return jsonify({"status": "ok"})

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True})

    # ============================================================
    # BLUEPRINTS
    # ============================================================
    app.register_blueprint(auth.bp)
    app.register_blueprint(grades.bp)
    app.register_blueprint(messages.bp)
    app.register_blueprint(planner.bp)
    app.register_blueprint(profile.bp)

    # ============================================================
    # ERROR HANDLER
    # ============================================================
    @app.errorhandler(Exception)
    def on_error(e):
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    return app


app = create_app()


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
