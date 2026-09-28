"""
Auth endpoints.
"""

from flask import Blueprint, jsonify

from modules.common import with_session


bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@bp.post("/login")
@with_session
def login(_session=None, _creds=None):
    """Test of credentials werken. Credentials komen uit X-SS-Creds header."""
    return jsonify({
        "ok": True,
        "username": _creds["username"],
        "main_url": _creds["main_url"],
    })
