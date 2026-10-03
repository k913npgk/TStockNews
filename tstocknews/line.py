"""Quota-aware push; stable retry key permits safe recovery after a timeout."""
import json
import os
import uuid
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from .network import tls_context


def request(path, token, payload=None, retry_key=None):
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    if retry_key:
        headers["X-Line-Retry-Key"] = retry_key
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = Request("https://api.line.me/v2/bot/" + path, data=body, headers=headers)
    try:
        with urlopen(req, timeout=25, context=tls_context()) as response:
            data = response.read()
            return json.loads(data) if data else {}
    except HTTPError as error:
        # A 409 only counts as accepted when LINE identifies the original request.
        if error.code == 409 and error.headers.get("x-line-accepted-request-id"):
            return {"accepted_retry": True}
        raise RuntimeError(f"LINE API failed with HTTP {error.code}") from None


def send(text, delivery, request_fn=request):
    if delivery.get("status") == "SENT":
        return delivery
    created = datetime.fromisoformat(delivery["prepared_at"])
    if datetime.now(timezone.utc) - created >= timedelta(hours=23):
        return {**delivery, "status": "RETRY_WINDOW_EXPIRED"}
    token, group = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"), os.environ.get("LINE_GROUP_ID")
    if not token or not group:
        raise ValueError("Set LINE_CHANNEL_ACCESS_TOKEN and LINE_GROUP_ID in secrets.")
    quota = request_fn("message/quota", token)
    usage = request_fn("message/quota/consumption", token)
    count = request_fn(f"group/{group}/members/count", token).get("count")
    if not isinstance(count, int) or count < 1:
        raise RuntimeError("Cannot verify recipient count; no message sent.")
    # Refuse paid/unlimited accounts: this project has an explicit free-only boundary.
    limit = quota.get("value")
    consumed = usage.get("totalUsage")
    if quota.get("type") != "limited" or not isinstance(limit, int) or limit > 200:
        return {**delivery, "status": "PLAN_NOT_FREE", "recipient_count": count}
    if not isinstance(consumed, int) or consumed + count > limit:
        return {**delivery, "status": "QUOTA_BLOCKED", "recipient_count": count}
    chunks = [text[i:i + 4500] for i in range(0, len(text), 4500)]
    if len(chunks) > 5:
        raise ValueError("Report exceeds LINE's single-request capacity.")
    key = delivery.get("retry_key")
    if not key:
        raise ValueError("Persist a retry key before sending.")
    request_fn("message/push", token,
               {"to": group, "messages": [{"type": "text", "text": c} for c in chunks]}, key)
    return {**delivery, "status": "SENT", "recipient_count": count}


def prepare(payload_hash):
    return {"status": "PENDING", "payload_hash": payload_hash,
            "retry_key": str(uuid.uuid4()), "prepared_at": datetime.now(timezone.utc).isoformat()}
