#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:5000}"
API_KEY="${API_KEY:-}"

PASS=0
FAIL=0
SKIP=0
RETRIEVAL_AVAILABLE=1

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

AUTH_ARGS=()
if [[ -n "$API_KEY" ]]; then
  AUTH_ARGS=(-H "X-API-Key: $API_KEY")
fi

note_pass() {
  PASS=$((PASS + 1))
  echo "PASS: $1"
}

note_fail() {
  FAIL=$((FAIL + 1))
  echo "FAIL: $1"
}

note_skip() {
  SKIP=$((SKIP + 1))
  echo "SKIP: $1"
}

json_has() {
  local file="$1"
  local key="$2"
  python3 - "$file" "$key" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)
print("ok" if key in data else "missing")
PY
}

curl_with_retry() {
  local mode="$1"
  local endpoint="$2"
  local body="$3"
  local headers="${4:-}"
  shift 4

  local delays=(0.5 1 2)
  local idx rc code err_file
  err_file="$TMP_DIR/curl_error.log"

  for idx in "${!delays[@]}"; do
    rm -f "$err_file"
    if [[ "$mode" == "json" ]]; then
      code="$(curl -sS -o "$body" -w "%{http_code}" \
        --connect-timeout 8 --max-time 45 \
        "$@" "$endpoint" 2>"$err_file")"
    else
      code="$(curl -sS -D "$headers" -o "$body" -w "%{http_code}" \
        --connect-timeout 8 --max-time 45 \
        "$@" "$endpoint" 2>"$err_file")"
    fi
    rc=$?
    if [[ $rc -eq 0 ]]; then
      echo "$code"
      return 0
    fi
    if [[ $rc -eq 56 || $rc -eq 7 || $rc -eq 28 ]]; then
      local attempt=$((idx + 1))
      local total="${#delays[@]}"
      local err_msg
      err_msg="$(tr -d '\n' <"$err_file")"
      echo "WARN: curl transport error for $endpoint (attempt $attempt/$total, rc=$rc): ${err_msg:-no details}" >&2
      if (( attempt < total )); then
        sleep "${delays[$idx]}"
        continue
      fi
    fi
    echo "ERROR: curl failed for $endpoint (rc=$rc)" >&2
    return "$rc"
  done
}

test_preflight_health() {
  local body="$TMP_DIR/health.json"
  local code qdrant_ok
  if ! code="$(curl_with_retry "json" "$BASE_URL/health" "$body" "" "${AUTH_ARGS[@]}")"; then
    note_fail "Preflight GET /health transport error"
    RETRIEVAL_AVAILABLE=0
    return
  fi
  if [[ "$code" != "200" ]]; then
    note_fail "Preflight GET /health -> HTTP $code"
    RETRIEVAL_AVAILABLE=0
    return
  fi
  for key in success overall ocr file_parsers embedding qdrant; do
    if [[ "$(json_has "$body" "$key")" != "ok" ]]; then
      note_fail "Preflight GET /health missing key: $key"
      RETRIEVAL_AVAILABLE=0
      return
    fi
  done
  if ! python3 - "$body" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
sys.exit(0 if data.get("success") is True else 1)
PY
  then
    note_fail "Preflight GET /health returned success=false"
    RETRIEVAL_AVAILABLE=0
    return
  fi

  qdrant_ok="$(python3 - "$body" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
q = data.get("qdrant")
ok = q.get("ok") if isinstance(q, dict) else None
print("true" if ok is True else "false")
PY
)"
  if [[ "$qdrant_ok" != "true" ]]; then
    RETRIEVAL_AVAILABLE=0
    note_skip "Retrieval-dependent tests skipped: Qdrant unavailable (health.qdrant.ok=false)"
  fi

  note_pass "Preflight GET /health"
}

test_search_empty_query() {
  local body="$TMP_DIR/search_empty.json"
  local code
  if ! code="$(curl_with_retry "json" "$BASE_URL/search" "$body" "" \
    "${AUTH_ARGS[@]}" \
    -H "Content-Type: application/json" \
    -d '{"query": ""}')"; then
    note_fail "POST /search (empty query) transport error"
    return
  fi
  if [[ "$code" == "200" ]]; then
    if ! python3 - "$body" <<'PY'
import json, re, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
if data.get("success") is not False:
    sys.exit(1)
msg = f"{data.get('error','')} {data.get('message','')}".lower()
sys.exit(0 if re.search(r"(empty|query|zapyt)", msg) else 1)
PY
    then
      note_fail "POST /search empty query expected success:false with empty-query error message"
      return
    fi
    note_pass "POST /search (empty query validation)"
    return
  fi
  note_fail "POST /search empty query expected HTTP 200, got $code"
}

test_search_stream_sse() {
  if [[ "$RETRIEVAL_AVAILABLE" -ne 1 ]]; then
    note_skip "POST /search/stream (SSE) skipped: Qdrant unavailable"
    return
  fi
  local headers="$TMP_DIR/search_stream.headers"
  local body="$TMP_DIR/search_stream.body"
  local code
  if ! code="$(curl_with_retry "sse" "$BASE_URL/search/stream" "$body" "$headers" \
    "${AUTH_ARGS[@]}" \
    -H "Content-Type: application/json" \
    -d '{"query":"test","limit":2}')"; then
    note_fail "POST /search/stream transport error"
    return
  fi
  if [[ "$code" != "200" ]]; then
    note_fail "POST /search/stream -> HTTP $code"
    return
  fi
  if ! grep -qi "content-type: text/event-stream" "$headers"; then
    note_fail "POST /search/stream missing text/event-stream content-type"
    return
  fi
  if ! grep -Eq '"event"\s*:\s*"(done|error)"|event:\s*(done|error)' "$body"; then
    note_fail "POST /search/stream missing done/error event"
    return
  fi
  if ! python3 - "$body" <<'PY'
import json, sys
from pathlib import Path

raw = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
done_payload = None
current_event = None
for line in raw.splitlines():
    if line.startswith("event: "):
        current_event = line[7:].strip()
    elif line.startswith("data: ") and current_event == "done":
        try:
            done_payload = json.loads(line[6:])
        except Exception:
            done_payload = None
        break

if not isinstance(done_payload, dict):
    sys.exit(1)

usage = done_payload.get("usage")
if not isinstance(usage, dict):
    sys.exit(1)

if any(k in usage for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
    sys.exit(0)
sys.exit(1)
PY
  then
    note_fail "POST /search/stream missing done.usage payload"
    return
  fi
  note_pass "POST /search/stream (SSE)"
}

test_get_context_validation() {
  local body="$TMP_DIR/get_context_bad.json"
  local code
  if ! code="$(curl_with_retry "json" "$BASE_URL/api/get_context" "$body" "" "${AUTH_ARGS[@]}")"; then
    note_fail "GET /api/get_context transport error"
    return
  fi
  if [[ "$code" == "200" ]]; then
    note_fail "GET /api/get_context without params should not return 200"
    return
  fi
  if ! grep -qi "point_id\|file\|query\|error\|brak" "$body"; then
    note_fail "GET /api/get_context validation response not recognized"
    return
  fi
  note_pass "GET /api/get_context (validation)"
}

test_sql_config_redaction() {
  local post_body="$TMP_DIR/sql_post.json"
  local get_body="$TMP_DIR/sql_get.json"
  local code_post code_get
  local payload='{"type":"sqlite","db_path":"/tmp/smoke_test.db","user":"smoke","password":"secret123"}'

  if ! code_post="$(curl_with_retry "json" "$BASE_URL/sql/config" "$post_body" "" \
    "${AUTH_ARGS[@]}" \
    -H "Content-Type: application/json" \
    -d "$payload")"; then
    note_fail "POST /sql/config transport error"
    return
  fi

  if [[ "$code_post" != "200" ]]; then
    note_fail "POST /sql/config -> HTTP $code_post"
    return
  fi

  if ! code_get="$(curl_with_retry "json" "$BASE_URL/sql/config" "$get_body" "" "${AUTH_ARGS[@]}")"; then
    note_fail "GET /sql/config transport error"
    return
  fi
  if [[ "$code_get" != "200" ]]; then
    note_fail "GET /sql/config -> HTTP $code_get"
    return
  fi

  if grep -q "secret123" "$get_body"; then
    note_fail "GET /sql/config leaked raw password"
    return
  fi
  if ! python3 - "$get_body" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
cfg = data.get("config") if isinstance(data, dict) else None
if not isinstance(cfg, dict):
    sys.exit(1)
# Accept either strict omission of password field or a redacted placeholder.
if "password" not in cfg:
    sys.exit(0)
sys.exit(0 if cfg.get("password") == "********" else 1)
PY
  then
    note_fail "GET /sql/config expected password to be omitted or redacted"
    return
  fi

  note_pass "POST+GET /sql/config (password redaction)"
}

test_network_stream_sse() {
  if [[ "$RETRIEVAL_AVAILABLE" -ne 1 ]]; then
    note_skip "POST /network (SSE) skipped: Qdrant unavailable"
    return
  fi
  local headers="$TMP_DIR/network_stream.headers"
  local body="$TMP_DIR/network_stream.body"
  local code
  if ! code="$(curl_with_retry "sse" "$BASE_URL/network" "$body" "$headers" \
    "${AUTH_ARGS[@]}" \
    -H "Content-Type: application/json" \
    -d '{"query":"test","limit":2}')"; then
    note_fail "POST /network transport error"
    return
  fi
  if [[ "$code" != "200" ]]; then
    note_fail "POST /network -> HTTP $code"
    return
  fi
  if ! grep -qi "content-type: text/event-stream" "$headers"; then
    note_fail "POST /network missing text/event-stream content-type"
    return
  fi
  if ! grep -Eq '"event"\s*:\s*"(progress|done|error)"|event:\s*(progress|done|error)' "$body"; then
    note_fail "POST /network missing progress/done/error event"
    return
  fi
  note_pass "POST /network (SSE)"
}

echo "Running smoke API tests against: $BASE_URL"
test_preflight_health
test_search_empty_query
test_search_stream_sse
test_get_context_validation
test_sql_config_redaction
test_network_stream_sse

echo
echo "Summary: PASS=$PASS FAIL=$FAIL SKIP=$SKIP"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
