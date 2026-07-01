import json

from flask import Response, jsonify, stream_with_context

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def json_success(status: int = 200, **payload):
    return jsonify({"success": True, **payload}), status


def json_error(error_message: str, status: int = 400, **payload):
    return jsonify({"success": False, "error": error_message, **payload}), status


def sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_response(generator):
    return Response(
        stream_with_context(generator),
        mimetype="text/event-stream",
        headers=_SSE_HEADERS,
    )
