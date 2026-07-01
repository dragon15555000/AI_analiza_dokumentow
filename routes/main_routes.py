from pathlib import Path

from flask import Blueprint, make_response, render_template, session

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    session.permanent = True
    session["user_authenticated"] = True
    asset_version = int(
        (Path(__file__).resolve().parent.parent / "static" / "financial_audit.js").stat().st_mtime
    )
    response = make_response(
        render_template("index.html", api_key_required=False, asset_version=asset_version)
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
