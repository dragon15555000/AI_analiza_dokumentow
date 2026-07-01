import importlib
import logging
import os
import subprocess
import threading
import time

from flask import Blueprint

from utils.http import json_error, json_success

admin_bp = Blueprint("admin", __name__)
logger = logging.getLogger("ai_analiza")


def _app_module():
    return importlib.import_module("app")


@admin_bp.route("/api/update/status", methods=["GET"])
def api_update_status():
    """Zwraca status aktualizacji — aktualna wersja, najnowsza, changelog."""
    app_mod = _app_module()
    if not app_mod._localhost_only():
        return json_error("Dostępne tylko z localhost", status=403)
    try:
        local_tag = app_mod._get_local_latest_tag()
        remote_tag = app_mod._get_remote_latest_tag()
        release_info = app_mod._get_latest_github_release() or {}

        if release_info.get("tag_name"):
            remote_tag = release_info["tag_name"]

        def _version_key(version: str) -> tuple:
            try:
                return tuple(int(part) for part in version.lstrip("v").split("."))
            except Exception:
                return (0, 0)

        update_available = bool(
            remote_tag and local_tag and _version_key(remote_tag) > _version_key(local_tag)
        )

        return json_success(
            local_version=local_tag,
            remote_version=remote_tag or local_tag,
            current_version=local_tag,
            latest_version=remote_tag or local_tag,
            update_available=update_available,
            release=release_info,
            changelog=release_info.get("body", ""),
            release_url=release_info.get("html_url"),
            message="Nowa wersja dostępna"
            if update_available
            else "Jesteś na najnowszej wersji",
        )
    except Exception as exc:
        logger.exception("api_update_status error")
        return json_error(str(exc)[:200], status=500)


@admin_bp.route("/api/update/pull", methods=["POST"])
def api_update_pull():
    """Pobiera najnowszy kod z GitHub (git pull origin master)."""
    app_mod = _app_module()
    if not app_mod._localhost_only():
        return json_error("Dostępne tylko z localhost", status=403)
    try:
        result = app_mod._git_pull()
        ok = result.get("success", False)
        msg = (
            result.get("stdout")
            or result.get("stderr")
            or (
                "Kod zaktualizowany. Zalecany restart aplikacji."
                if ok
                else "Błąd podczas aktualizacji."
            )
        )
        if ok:
            return json_success(
                message=msg,
                output=result.get("stdout", ""),
                error="",
                details=result,
            )
        return json_error(
            result.get("stderr", "") or msg,
            status=200,
            message=msg,
            output=result.get("stdout", ""),
            details=result,
        )
    except Exception as exc:
        logger.exception("api_update_pull error")
        return json_error(str(exc)[:200], status=500)


@admin_bp.route("/api/update/restart", methods=["POST"])
def api_update_restart():
    """Restartuje aplikację (poprzez systemd --user service)."""
    app_mod = _app_module()
    if not app_mod._localhost_only():
        return json_error("Dostępne tylko z localhost", status=403)
    try:
        result = app_mod._try_restart_service()
        if result.get("success", False):
            return json_success(
                message=result.get("message", "Restart initiated"),
                method=result.get("method", "unknown"),
            )
        return json_error(
            result.get("message", "Restart failed"),
            status=200,
            method=result.get("method", "unknown"),
        )
    except Exception as exc:
        logger.exception("api_update_restart error")
        return json_error(str(exc)[:200], status=500)


@admin_bp.route("/api/service/status", methods=["GET"])
def service_status():
    """Status usługi systemd i ostatnie logi — tylko localhost."""
    app_mod = _app_module()
    if not app_mod._localhost_only():
        return json_error("Dostępne tylko z localhost", status=403)

    return json_success(
        is_systemd=bool(os.environ.get("INVOCATION_ID")),
        active=app_mod._systemd_service_state(),
        logs=app_mod._systemd_service_logs(30),
    )


@admin_bp.route("/api/service/restart", methods=["POST"])
def service_restart():
    """Restart usługi ai_analiza — tylko localhost."""
    app_mod = _app_module()
    if not app_mod._localhost_only():
        return json_error("Dostępne tylko z localhost", status=403)

    def _do_restart():
        time.sleep(0.6)
        try:
            import shutil

            if shutil.which("systemctl"):
                for cmd in (
                    ["systemctl", "--user", "restart", "ai_analiza"],
                    ["systemctl", "restart", "ai_analiza"],
                ):
                    try:
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                        if result.returncode == 0:
                            return
                    except Exception:
                        continue
            os._exit(0)
        except Exception:
            os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()
    return json_success(msg="Restart zlecony")
