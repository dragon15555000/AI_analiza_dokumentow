import importlib
import logging

from flask import Blueprint, request

from utils.http import json_error, json_success

diagnostics_bp = Blueprint("diagnostics", __name__)
logger = logging.getLogger("ai_analiza")


def _app_module():
    return importlib.import_module("app")


@diagnostics_bp.route("/health", methods=["GET"])
def health():
    """Zwraca bogaty status systemu dla UI (dashboard + modal diagnostyczny)."""
    app_mod = _app_module()
    light = request.args.get("light", "0") == "1"
    try:
        q = app_mod._check_qdrant_health()
        ocr = app_mod._ocr_health_status()
        parsers = app_mod._file_parsers_health() if not light else {}
        ollama_h = {"ok": True} if light else app_mod._check_ollama_health()
        or_h = {"ok": True} if light else app_mod._check_openrouter_health()

        llm_ok = False
        llm_detail = ""
        if light:
            llm_ok = True
            llm_detail = "light"
        elif app_mod.OPENROUTER_API_KEY:
            llm_ok = or_h.get("ok", False)
            llm_detail = or_h.get("error") or "OpenRouter"
        else:
            llm_ok = ollama_h.get("ok", False)
            llm_detail = ollama_h.get("error") or "Ollama"

        sql_cfg = app_mod._load_sql_config()
        sql_configured = app_mod._sql_conn_configured(sql_cfg)
        sql_status = (
            "ok"
            if (
                sql_configured
                and (app_mod._sql_dialect(sql_cfg) == "sqlite" or app_mod.PYMSSQL_AVAILABLE)
            )
            else ("warn" if sql_configured else "absent")
        )

        vectors = 0
        coll_name = app_mod.ACTIVE_COLLECTION
        try:
            if q.get("ok") and q.get("active_collection_exists"):
                vectors = q.get("points_in_active", 0)
        except Exception:
            pass

        critical_ok = q.get("ok", False) and (llm_ok or True)
        overall = "ok" if critical_ok else "degraded"

        try:
            ver = app_mod._get_app_version()
        except Exception:
            ver = "dev"

        return json_success(
            overall=overall,
            version=ver,
            timestamp=int(app_mod.time.time()),
            qdrant=q,
            qdrant_status="ok" if q.get("ok") else "error",
            llm={"ok": llm_ok, "detail": llm_detail},
            llm_status="ok" if llm_ok else "warn",
            embedding={
                "ok": ollama_h.get("ok", False),
                "model": "nomic-embed-text",
            },
            ocr=ocr,
            ocr_available=ocr.get("available", False),
            file_parsers=parsers,
            sql_available=app_mod.PYMSSQL_AVAILABLE,
            sql_configured=sql_configured,
            sql_status=sql_status,
            active_collection={"name": coll_name, "points": vectors},
            vectors_count=vectors,
            provider=app_mod.DEFAULT_LLM_PROVIDER,
            llm_model=app_mod._effective_llm_model(app_mod.DEFAULT_LLM_PROVIDER),
            gemini_configured=bool(app_mod.GEMINI_API_KEY),
            gemini_model=app_mod.GEMINI_MODEL,
            gemini=app_mod._check_gemini_health(),
            app_api_key_set=bool(app_mod.APP_API_KEY),
        )
    except Exception as exc:
        logger.exception("health endpoint error")
        return json_error(str(exc)[:200], status=500, overall="error")
