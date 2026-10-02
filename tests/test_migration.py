"""迁移签证的冻结存储与 HTTP 接口测试。"""
from __future__ import annotations

import json
import threading
import urllib.request
from urllib.error import HTTPError

import pytest

from app.api import build_server
from app.models import MigrationConflictError, MigrationRejectedError
from app.parser import parse_migration_request, parse_payload
from app.storage import AuditStore

INT = {"kind": "int"}
BOOL = {"kind": "bool"}
TEXT = {"kind": "text"}


def rec(*fields):
    return {"kind": "record", "fields": list(fields)}


def field(name, t, required=True):
    return {"name": name, "type": t, "required": required}


def variant(*tags):
    return {"kind": "variant", "tags": list(tags)}


def tag(label, t):
    return {"label": label, "type": t}


def ref(name):
    return {"kind": "ref", "name": name}


# 来源：兼容审计（发送端是接收端的子类型：发送端少可选字段、少接收端额外标签）。
SOURCE_SENDER = [
    {"name": "Cmd", "type": rec(
        field("id", INT), field("payload", ref("Payload")))},
    {"name": "Payload", "type": variant(
        tag("Nil", rec()),
        tag("Cons", rec(
            field("value", INT),
            field("next", ref("Payload"), required=False))))},
]
SOURCE_RECEIVER = [
    {"name": "Cmd", "type": rec(
        field("id", INT), field("payload", ref("Payload")),
        field("trace", BOOL, required=False))},
    {"name": "Payload", "type": variant(
        tag("Nil", rec()),
        tag("Cons", rec(
            field("value", INT),
            field("next", ref("Payload"), required=False))),
        tag("Reset", rec(field("at", INT))))},
]

# 新版：声明重排、类型名整体替换、字段/标签顺序变化，结构完全等价。
NEW_SENDER = [
    {"name": "Pl", "type": variant(
        tag("Cons", rec(
            field("next", ref("Pl"), required=False), field("value", INT))),
        tag("Nil", rec()))},
    {"name": "Command", "type": rec(
        field("payload", ref("Pl")), field("id", INT))},
]
NEW_RECEIVER = [
    {"name": "Command", "type": rec(
        field("trace", BOOL, required=False),
        field("payload", ref("Pl")), field("id", INT))},
    {"name": "Pl", "type": variant(
        tag("Reset", rec(field("at", INT))),
        tag("Cons", rec(
            field("value", INT),
            field("next", ref("Pl"), required=False))),
        tag("Nil", rec()))},
]


def audit_body(audit_id="SRC-AUDIT-1"):
    return {
        "audit_id": audit_id,
        "root_name": "Cmd",
        "sender_types": SOURCE_SENDER,
        "receiver_types": SOURCE_RECEIVER,
    }


def migration_body(
    migration_id="MIG-1",
    source_audit_id="SRC-AUDIT-1",
    new_root="Command",
    new_sender=None,
    new_receiver=None,
):
    return {
        "migration_id": migration_id,
        "source_audit_id": source_audit_id,
        "new_root_name": new_root,
        "new_sender_types": json.loads(json.dumps(
            NEW_SENDER if new_sender is None else new_sender)),
        "new_receiver_types": json.loads(json.dumps(
            NEW_RECEIVER if new_receiver is None else new_receiver)),
    }


@pytest.fixture()
def store(tmp_path):
    s = AuditStore(tmp_path / "data")
    s.submit(parse_payload(audit_body()))
    return s


# ---------------- 存储层 ----------------

def test_migration_issued_with_complete_bijections(store):
    m, created = store.submit_migration(parse_migration_request(migration_body()))
    assert created is True
    assert m.source_root == "Cmd" and m.new_root == "Command"
    assert [(p.source, p.target) for p in m.sender_bijection] == [
        ("Cmd", "Command"), ("Payload", "Pl")
    ]
    assert [(p.source, p.target) for p in m.receiver_bijection] == [
        ("Cmd", "Command"), ("Payload", "Pl")
    ]


def test_same_request_reopens_original_visa(store):
    m1, created1 = store.submit_migration(parse_migration_request(migration_body()))
    assert created1 is True
    m2, created2 = store.submit_migration(parse_migration_request(migration_body()))
    assert created2 is False
    assert m2.frozen_at == m1.frozen_at
    assert m2.migration_fingerprint == m1.migration_fingerprint


def test_changed_new_contract_conflicts_and_never_rewrites(store):
    m1, _ = store.submit_migration(parse_migration_request(migration_body()))
    changed = migration_body()
    changed["new_sender_types"][1]["type"]["fields"][1]["name"] = "identifier"
    with pytest.raises(MigrationConflictError):
        store.submit_migration(parse_migration_request(changed))
    m2 = store.get_migration("MIG-1")
    assert m2.frozen_at == m1.frozen_at
    assert [(p.source, p.target) for p in m2.sender_bijection] == [
        ("Cmd", "Command"), ("Payload", "Pl")
    ]


def test_changed_source_audit_conflicts(store):
    store.submit_migration(parse_migration_request(migration_body()))
    switched = migration_body(source_audit_id="SRC-AUDIT-OTHER")
    with pytest.raises(MigrationConflictError):
        store.submit_migration(parse_migration_request(switched))


def test_reject_missing_source(store):
    body = migration_body(migration_id="MIG-MISS", source_audit_id="NOPE")
    with pytest.raises(MigrationRejectedError) as ei:
        store.submit_migration(parse_migration_request(body))
    assert any("不存在" in r for r in ei.value.reasons)
    # 被拒绝的迁移不留冻结记录。
    from app.models import MigrationNotFoundError

    with pytest.raises(MigrationNotFoundError):
        store.get_migration("MIG-MISS")


def test_reject_incompatible_source(tmp_path):
    s = AuditStore(tmp_path / "data")
    bad = {
        "audit_id": "BAD-AUDIT", "root_name": "R",
        "sender_types": [{"name": "R", "type": rec(field("a", INT))}],
        "receiver_types": [{"name": "R", "type": rec(field("a", TEXT))}],
    }
    s.submit(parse_payload(bad))
    body = {
        "migration_id": "MIG-BAD", "source_audit_id": "BAD-AUDIT",
        "new_root_name": "R",
        "new_sender_types": bad["sender_types"],
        "new_receiver_types": bad["receiver_types"],
    }
    with pytest.raises(MigrationRejectedError) as ei:
        s.submit_migration(parse_migration_request(body))
    assert any("不兼容" in r for r in ei.value.reasons)


def test_reject_declaration_count_mismatch(store):
    body = migration_body(migration_id="MIG-COUNT")
    body["new_sender_types"] = body["new_sender_types"] + [
        {"name": "Orphan", "type": INT}
    ]
    with pytest.raises(MigrationRejectedError) as ei:
        store.submit_migration(parse_migration_request(body))
    assert any("数量不一致" in r for r in ei.value.reasons)


def test_reject_unreachable_count_mismatch(store):
    # 两侧总声明数相同（均 2 个），但新版根不再引用 Pl：
    # 来源不可达 0 个，新版不可达 1 个 -> 在数量检查阶段拒绝。
    body = migration_body(migration_id="MIG-UNREACH")
    body["new_sender_types"] = [
        NEW_SENDER[0],  # Pl（新版中成为孤立声明）
        {"name": "Command", "type": rec(field("id", INT))},
    ]
    with pytest.raises(MigrationRejectedError) as ei:
        store.submit_migration(parse_migration_request(body))
    assert any("不可达具名定义数量不一致" in r for r in ei.value.reasons)


def test_reject_structural_change(store):
    body = migration_body(migration_id="MIG-STRUCT")
    # 字段必需性变化。
    body["new_receiver_types"][0]["type"]["fields"][0]["required"] = True
    with pytest.raises(MigrationRejectedError) as ei:
        store.submit_migration(parse_migration_request(body))
    assert any("必需性" in r for r in ei.value.reasons)


def test_persistence_across_restart(tmp_path):
    directory = tmp_path / "data"
    s = AuditStore(directory)
    s.submit(parse_payload(audit_body()))
    m1, _ = s.submit_migration(parse_migration_request(migration_body()))
    reopened = AuditStore(directory).get_migration("MIG-1")
    assert reopened.frozen_at == m1.frozen_at
    assert len(reopened.sender_bijection) == 2
    # 原兼容审计与 API 行为保持可用。
    assert AuditStore(directory).get("SRC-AUDIT-1").compatible is True


# ---------------- HTTP 层 ----------------

@pytest.fixture()
def server(tmp_path):
    store = AuditStore(tmp_path / "data")
    srv = build_server("127.0.0.1", 0, store)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    yield f"http://{host}:{port}"
    srv.shutdown()
    srv.server_close()


def _request(url, method="GET", body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_api_full_migration_lifecycle(server):
    # 先冻结来源兼容审计。
    status, data = _request(f"{server}/api/audits", "POST", audit_body())
    assert status == 201 and data["conclusion"]["compatible"] is True

    body = migration_body()
    status, data = _request(f"{server}/api/migrations", "POST", body)
    assert status == 201, (status, data)
    m = data["migration"]
    assert m["issued"] is True
    assert len(m["sender_bijection"]) == 2
    frozen_at = m["frozen_at"]

    # 重传同一请求：200，原签证。
    status, data = _request(f"{server}/api/migrations", "POST", body)
    assert status == 200 and data["resubmitted_same_request"] is True
    assert data["migration"]["frozen_at"] == frozen_at

    # 重开。
    status, data = _request(f"{server}/api/migrations/MIG-1")
    assert status == 200 and data["migration"]["frozen_at"] == frozen_at


def test_api_migration_conflict_409(server):
    _request(f"{server}/api/audits", "POST", audit_body())
    body = migration_body()
    assert _request(f"{server}/api/migrations", "POST", body)[0] == 201

    changed = json.loads(json.dumps(body))
    changed["source_audit_id"] = "SRC-AUDIT-OTHER"
    status, data = _request(f"{server}/api/migrations", "POST", changed)
    assert status == 409 and data["error"] == "migration-conflict"

    changed = json.loads(json.dumps(body))
    changed["new_sender_types"][1]["type"]["fields"][1]["name"] = "id2"
    status, data = _request(f"{server}/api/migrations", "POST", changed)
    assert status == 409 and data["error"] == "migration-conflict"


def test_api_migration_rejections(server):
    _request(f"{server}/api/audits", "POST", audit_body())

    # 来源不存在。
    body = migration_body(migration_id="MIG-A", source_audit_id="NOPE")
    status, data = _request(f"{server}/api/migrations", "POST", body)
    assert status == 422 and data["error"] == "migration-rejected"
    assert any("不存在" in r for r in data["reasons"])

    # 结构不等价（字段改名）。
    body = migration_body(migration_id="MIG-B")
    body["new_sender_types"][1]["type"]["fields"][1]["name"] = "renamed"
    status, data = _request(f"{server}/api/migrations", "POST", body)
    assert status == 422 and any("字段" in r for r in data["reasons"])

    # 数量不一致。
    body = migration_body(migration_id="MIG-C")
    body["new_sender_types"].append({"name": "Orphan", "type": INT})
    status, data = _request(f"{server}/api/migrations", "POST", body)
    assert status == 422 and any("数量" in r for r in data["reasons"])

    # 被拒迁移不产生记录。
    assert _request(f"{server}/api/migrations/MIG-B")[0] == 404


def test_api_migration_validation_batched(server):
    status, data = _request(
        f"{server}/api/migrations", "POST",
        {"migration_id": "bad id", "new_sender_types": [],
         "new_receiver_types": "nope"},
    )
    assert status == 400
    assert len(data["issues"]) >= 2


def test_api_migration_404(server):
    status, data = _request(f"{server}/api/migrations/NOPE")
    assert status == 404 and data["error"] == "migration-not-found"


def test_original_audit_api_unchanged(server):
    _request(f"{server}/api/audits", "POST", audit_body())
    status, data = _request(f"{server}/api/audits/SRC-AUDIT-1")
    assert status == 200 and data["conclusion"]["compatible"] is True
