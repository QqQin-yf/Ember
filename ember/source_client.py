"""Exact, provider-validated historical path reads; never fall back to ranges."""
from __future__ import annotations
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from .source_refs import message_seq

SOURCE_URL = os.environ.get("EMBER_SOURCE_URL", "")
SOURCE_TOKEN = os.environ.get("EMBER_SOURCE_TOKEN", "")
SOURCE_TIMEOUT = float(os.environ.get("EMBER_SOURCE_TIMEOUT", "8"))

class SourceUnavailable(Exception):
    """Safe failure classification; never retain provider body or credentials."""
    def __init__(self, message, code="source_unavailable", http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status

def configured() -> bool:
    return bool(SOURCE_URL)


def fetch_source_increment(cursor=None, *, instance='default', limit=100):
    """Provider-owned opaque cursor over completed, path-validated source turns."""
    if not SOURCE_URL:
        raise SourceUnavailable('Source not configured','source_unconfigured')
    query=urllib.parse.urlencode({'cursor':json.dumps(cursor),'limit':limit})
    request=urllib.request.Request(SOURCE_URL.rstrip('/')+'/api/source/increment?'+query)
    request.add_header('X-Heart-Instance',instance)
    if SOURCE_TOKEN:
        request.add_header('Authorization','Bearer '+SOURCE_TOKEN)
    try:
        with _open_source(request) as response:
            data=json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        status=exc.code
        exc.close()
        raise SourceUnavailable('Source increment rejected','source_increment_rejected',status) from None
    except (OSError,ValueError) as exc:
        raise SourceUnavailable('Source increment unavailable','source_unreachable') from None
    if (not isinstance(data,dict) or 'cursor' not in data or not isinstance(data.get('turns'),list)
            or type(data.get('has_more')) is not bool or (data['has_more'] and data['cursor']==cursor)):
        raise SourceUnavailable('Invalid source increment','source_increment_invalid')
    revoked = data.get('revoked', [])
    if not isinstance(revoked, list) or any(
            not isinstance(row, dict) or not isinstance(row.get('session_id'), str)
            or not row['session_id'] or message_seq(row.get('message_id')) is None for row in revoked):
        raise SourceUnavailable('Invalid source revocations', 'source_increment_invalid')
    for row in data['turns']:
        if (not isinstance(row,dict) or not isinstance(row.get('session_id'),str) or not row['session_id']
                or not isinstance(row.get('messages'),list) or len(row['messages']) not in (1,2)
                or type(row.get('user_is_system',False)) is not bool):
            raise SourceUnavailable('Invalid source turn','source_increment_invalid')
        for message in row['messages']:
            if (not isinstance(message,dict) or message_seq(message.get('message_id')) is None
                    or message.get('role') not in ('user','assistant','system') or not isinstance(message.get('content'),str)):
                raise SourceUnavailable('Invalid source message','source_increment_invalid')
    return data

def _open_source(request):
    # Loopback and Tailscale providers must not be sent to a public HTTP proxy.
    import ipaddress
    host = urllib.parse.urlsplit(request.full_url).hostname or ""
    direct = host == "localhost"
    try:
        address = ipaddress.ip_address(host)
        direct = direct or address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        pass
    if direct:
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=SOURCE_TIMEOUT)
    return urllib.request.urlopen(request, timeout=SOURCE_TIMEOUT)


def fetch_source_path(session_id: str, message_ids: list, *,
                      instance: str = "default", write_turn: bool = False) -> list[dict]:
    ids = [message_seq(value) for value in message_ids]
    if not session_id or not ids or None in ids or len(set(ids)) != len(ids):
        raise SourceUnavailable("Exact source identities required")
    if not SOURCE_URL:
        raise SourceUnavailable("EMBER_SOURCE_URL not configured", "source_unconfigured")
    ids.sort(key=int)
    params = {"session_id": session_id, "message_ids": ",".join(ids)}
    if write_turn:
        params["write_turn"] = "1"
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(SOURCE_URL.rstrip("/") + "/api/source/path?" + query)
    req.add_header("X-Heart-Instance", instance)
    if SOURCE_TOKEN:
        req.add_header("Authorization", "Bearer " + SOURCE_TOKEN)
    try:
        with _open_source(req) as response:
            if response.status != 200:
                raise SourceUnavailable("Source provider rejected path")
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        code = {401: "source_auth_failed", 403: "source_instance_unknown",
                404: "source_session_unknown"}.get(status, "source_unavailable")
        with exc:
            if status == 409:
                try:
                    payload = json.loads(exc.read())
                    if payload.get("code") in {"source_path_unknown", "source_unavailable"}:
                        code = payload["code"]
                except (ValueError, AttributeError):
                    pass
        raise SourceUnavailable("Source path rejected (HTTP %d)" % status, code, status) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Do not copy a URL, token or provider body into tool errors/logs.
        raise SourceUnavailable("Source path unavailable", "source_unreachable") from exc
    rows = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise SourceUnavailable("Source response identities incomplete")
    out = []
    for row, seq in zip(rows, ids):
        if (not isinstance(row, dict) or message_seq(row.get("message_id")) != seq
                or row.get("role") not in ("user", "assistant")
                or not isinstance(row.get("content"), str)):
            raise SourceUnavailable("Source response identity/shape mismatch")
        out.append(dict(row, message_id=int(seq)))
    return out


def fetch_source_branch(session_id: str, anchor: str, *, instance: str, before: str | None, limit: int):
    if not SOURCE_URL:
        raise SourceUnavailable("Source provider is not configured", "source_unconfigured")
    params = {"session_id": session_id, "anchor": anchor, "limit": limit}
    if before is not None:
        params["before"] = before
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(SOURCE_URL.rstrip("/") + "/api/source/branch?" + query)
    req.add_header("X-Heart-Instance", instance)
    if SOURCE_TOKEN:
        req.add_header("Authorization", "Bearer " + SOURCE_TOKEN)
    try:
        with _open_source(req) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        code = {401: "source_auth_failed", 403: "source_instance_unknown",
                404: "source_session_unknown", 409: "source_path_unknown"}.get(exc.code, "source_unreachable")
        exc.close()
        raise SourceUnavailable("Source branch rejected", code) from exc
    except (OSError, ValueError) as exc:
        raise SourceUnavailable("Source branch unavailable", "source_unreachable") from exc
    rows = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > limit or data.get("anchor") != anchor:
        raise SourceUnavailable("Source branch identity mismatch", "source_path_unknown")
    seen = set()
    for row in rows:
        seq = message_seq(row.get("message_id")) if isinstance(row, dict) else None
        if (seq is None or seq in seen or row.get("role") not in ("user", "assistant")
                or not isinstance(row.get("content"), str)):
            raise SourceUnavailable("Source branch identity mismatch", "source_path_unknown")
        seen.add(seq)
    cursor = data.get("next_before")
    if "next_before" not in data or (cursor is not None and
            (len(rows) != limit or message_seq(cursor) != message_seq(rows[-1]["message_id"]))):
        raise SourceUnavailable("Source branch cursor mismatch", "source_path_unknown")
    return rows, message_seq(cursor)


def iter_source_branch(session_id: str, anchor: str, *, instance: str):
    """Yield newest-first pages from a fixed branch, bounded across the whole search."""
    before = None
    seen = set()
    while len(seen) < 5000:
        limit = min(200, 5000 - len(seen))
        rows, cursor = fetch_source_branch(session_id, anchor, instance=instance, before=before, limit=limit)
        ids = {message_seq(row["message_id"]) for row in rows}
        if seen & ids:
            raise SourceUnavailable("Source branch repeated a message", "source_path_unknown")
        seen.update(ids)
        yield rows
        if cursor is None:
            return
        before = cursor
    raise SourceUnavailable("Source branch search limit reached", "cc_history_limit")
