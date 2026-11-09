"""Trust exact generation/provenance identities, not legacy max-reply mappings."""
from __future__ import annotations
import json
import os
from . import source_client
from .source_refs import message_seq


def generation_messages(generations, sid: str, *, completed_only=False) -> list[dict]:
    if not os.path.exists(generations.path):
        return []
    with generations._db() as db:
        rows = db.execute("SELECT g.*,s.metadata_json FROM generations g JOIN recall_snapshots s "
                          "USING(recall_id) WHERE g.session_id=? AND s.state='ready' "
                          "ORDER BY g.turn", (sid,)).fetchall()
    messages = []
    for row in rows:
        if completed_only and row["state"] != "done":
            continue
        context = json.loads(row["metadata_json"])["request_context"]
        if context.get("session_id") != sid:
            continue
        user = message_seq(context.get("user_message_id"))
        assistant = message_seq(row["assistant_message_id"]) if row["state"] == "done" else None
        if user:
            messages.append({"turn": row["turn"], "role": "user", "message_id": user,
                             "recall_id": row["recall_id"]})
        if assistant:
            messages.append({"turn": row["turn"], "role": "assistant", "message_id": assistant,
                             "recall_id": row["recall_id"]})
    return messages


def read_messages(sid: str, identities: list[dict], instance: str) -> list[dict]:
    if not identities:
        raise source_client.SourceUnavailable("Source identity evidence missing")
    by_seq = {}
    for item in identities:
        seq = message_seq(item.get("message_id"))
        if seq is None or seq in by_seq:
            raise source_client.SourceUnavailable("Source identity ambiguous")
        by_seq[seq] = item
    rows = source_client.fetch_source_path(sid, list(by_seq), instance=instance)
    out = []
    # Check even injected/mock clients: no partial response or unexpected rows.
    if len(rows) != len(by_seq):
        raise source_client.SourceUnavailable("Source identities incomplete")
    seen = set()
    for row in rows:
        seq = message_seq(row.get("message_id"))
        identity = by_seq.get(seq)
        if (not identity or seq in seen or identity["role"] != row.get("role")
                or not isinstance(row.get("content"), str)):
            raise source_client.SourceUnavailable("Source identity mismatch")
        seen.add(seq)
        out.append(dict(row, turn=identity["turn"], _source_session_id=sid,
                        _source_message_id=seq, _source_recall_id=identity.get("recall_id")))
    return out


class SourceCorpus(list):
    """List-compatible transient source rows with failure context, no raw body."""
    def __init__(self, rows=(), issue=None):
        super().__init__(rows)
        self.issue = issue


class QuoteSourceError(ValueError):
    def __init__(self, code, *, field=None, candidate_count=None):
        self.code = code
        self.field = field
        self.candidate_count = candidate_count
        messages = {
            "cc_history_limit": "引文太久远，在本分支最近的 5000 条已入库消息里找不到。",
            "cc_quote_not_in_history": "在已入库的 main 历史中找不到引文。如果引用的是本轮原话，请在下一轮写入。",
            "source_unconfigured": "原文服务未配置，历史原文不可用，请检查原文连接配置，不必反复重摘。",
            "source_unreachable": "原文服务连接失败或超时，暂时无法核验历史摘录；请恢复原文连接后重试。",
            "source_auth_failed": "原文服务鉴权失败，暂时无法核验历史摘录；请检查原文凭据。",
            "source_identity_missing": "缺少可靠的历史消息编号，暂时无法核验这段摘录。",
            "source_path_unknown": "部分来源不在本轮实际消息路径上，无法将其作为本轮出处。",
            "source_session_unknown": "当前实例无权读取来源会话，或来源会话已不存在。",
            "source_instance_unknown": "原文服务拒绝了当前实例身份，暂时无法核验历史摘录。",
            "quote_not_found": "quote_spans 未能定位：在已核验的可用原文中未找到起尾摘录，请核对引用来源。",
            "quote_ambiguous": "摘录去除格式后匹配到多条原文消息，无法唯一确定出处；请增加能区分场景的原文文字。",
        }
        super().__init__(messages.get(code, "历史原文未完整取得，暂时无法核验摘录；请检查原文读取状态。"))

    def as_result(self):
        hints = {
            "source_unconfigured": "请检查原文连接配置；恢复前不要反复重摘或重试写入。",
            "source_unreachable": "请检查原文服务连通性；恢复后再重新调用，不必改写摘录。",
            "source_auth_failed": "请检查原文服务令牌配置，不要修改摘录来重试。",
            "source_instance_unknown": "请检查实例配置和原文服务的实例归属规则。",
            "source_session_unknown": "请检查来源会话是否存在、是否属于当前实例；不要猜测其他会话。",
            "source_identity_missing": "需要可核验的消息编号；不要用相邻轮次或当前窗口代替历史出处。",
            "source_path_unknown": "请核对消息是否来自同一真实分支；不要改用范围读取。",
            "quote_not_found": "请从已核验原文中逐字重摘起尾句，保持原场景范围。",
            "quote_ambiguous": "请扩长该端摘录，加入同一条原文中的独特文字，再重新调用；不要只调整标点或 emoji。",
        }
        result = {"status": "failed", "code": self.code, "error": str(self),
                  "hint": hints.get(self.code, "请检查原文读取条件；未恢复前不要反复重摘。")}
        if self.code == "quote_not_found":
            result["field"] = "quote_spans"
        if self.field is not None:
            result["field"] = self.field
        if self.candidate_count is not None:
            result["candidate_count"] = self.candidate_count
        if self.field == 'event.focus' and self.code in ('quote_not_found','quote_ambiguous'):
            result['error']=('focus 未能在来源区间定位。' if self.code=='quote_not_found' else 'focus 的格式容错匹配有歧义。')
            result['hint']='请从该来源区间逐字重摘一句最重要的原话；可扩长摘录以唯一定位，不要填写消息编号。'
        return result


def legacy_messages(ledger, sid):
    """Exact individual hook receipts only; never use max-reply/range inference.

    A role/turn with conflicting raw receipts is withheld. These identities are
    candidates only: the provider still has to prove membership in the path.
    """
    records = {}
    try:
        stream = open(ledger._path(), encoding="utf-8")
    except FileNotFoundError:
        return []
    with stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            turn = row.get("ember_turn")
            if row.get("ember_session") != sid or not isinstance(turn, int) or turn < 1:
                continue
            item = records.setdefault(turn, {"user": set(), "assistant": set()})
            for role, field in (("user", "user_message_id"), ("assistant", "assistant_message_id")):
                seq = message_seq(row.get(field))
                if seq:
                    item[role].add(seq)
    out = []
    issue = None
    for turn, item in sorted(records.items()):
        if any(len(ids) > 1 for ids in item.values()):
            issue = "source_identity_missing"
            continue
        for role, ids in item.items():
            if ids:
                out.append({"turn": turn, "role": role, "message_id": next(iter(ids))})
    return SourceCorpus(out, issue)


def current_path_corpus(sid, identities, anchor, instance, max_requests=64):
    """Prove known identities against the real current user anchor.

    Split only on explicit path/missing-row rejection. Unavailable/auth errors
    stop immediately. At most max_requests; never use min/max/current leaf.
    """
    grouped = {}
    for item in identities:
        grouped.setdefault(item["message_id"], []).append(item)
    unique = []
    issue = None
    for seq, group in grouped.items():
        positions = {(m["turn"], m["role"]) for m in group}
        if len(positions) != 1:
            issue = "source_identity_missing"
            continue
        unique.append(group[0])
    if not unique:
        return SourceCorpus(issue="source_identity_missing")
    if anchor is None:
        return SourceCorpus(read_messages(sid, unique, instance), issue)
    anchor_seq = anchor["message_id"]
    unique = [m for m in unique if int(m["message_id"]) < int(anchor_seq)]
    accepted = []
    budget = max_requests
    def fetch(batch):
        nonlocal budget, issue
        if budget <= 0:
            issue = "source_read_limit"
            return
        budget -= 1
        try:
            result = read_messages(sid, batch + [anchor], instance)
            accepted.extend(m for m in result if m["_source_message_id"] != anchor_seq)
        except source_client.SourceUnavailable as exc:
            if exc.http_status != 409 or exc.code not in {"source_path_unknown", "source_unavailable"}:
                raise
            issue = exc.code
            if len(batch) > 1:
                mid = len(batch) // 2
                fetch(batch[:mid])
                fetch(batch[mid:])
    fetch(unique)
    accepted.sort(key=lambda m: int(m["_source_message_id"]))
    return SourceCorpus(accepted, issue)
