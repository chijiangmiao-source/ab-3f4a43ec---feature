"""迁移签发、冻结与 HTTP 接口测试。"""
from __future__ import annotations

import json
import threading
import urllib.request
from urllib.error import HTTPError

import pytest

from app.api import build_server
from app.migration import MigrationStore
from app.models import (
    ContractConflictError,
    MigrationNotFoundError,
    MigrationRejectedError,
)
from app.parser import parse_migration_payload, parse_payload
from app.storage import AuditStore

INT = {"kind": "int"}
TEXT = {"kind": "text"}


def list_decl(name):
    return [{
        "name": name,
        "type": {"kind": "variant", "tags": [
            {"label": "Nil", "type": {"kind": "record", "fields": []}},
            {"label": "Cons", "type": {"kind": "record", "fields": [
                {"name": "head", "type": INT, "required": True},
                {"name": "tail", "type": {"kind": "ref", "name": name},
                 "required": False},
            ]}},
        ]},
    }]


def source_contract(audit_id="AUDIT-SRC-1"):
    return {
        "audit_id": audit_id,
        "root_name": "List",
        "sender_types": list_decl("List"),
        "receiver_types": list_decl("List"),
    }


def migration_request(migration_id="MIG-1", source="AUDIT-SRC-1"):
    # 类型名整体替换 List→Chain，声明结构不变：与来源完全结构等价。
    return {
        "migration_id": migration_id,
        "source_audit_id": source,
        "new_root_name": "Chain",
        "new_sender_types": list_decl("Chain"),
        "new_receiver_types": list_decl("Chain"),
    }


@pytest.fixture()
def stores(tmp_path):
    audits = AuditStore(tmp_path / "data")
    return audits, MigrationStore(tmp_path / "data")


def _submit_source(audits, audit_id="AUDIT-SRC-1", compatible=True):
    contract = source_contract(audit_id)
    if not compatible:
        contract["receiver_types"] = [{
            "name": "List",
            "type": {"kind": "variant", "tags": [
                {"label": "Nil", "type": {"kind": "record", "fields": []}},
            ]},
        }]
    audits.submit(parse_payload(contract))
    return contract


def test_migration_issued_on_exact_rename(stores):
    audits, migrations = stores
    _submit_source(audits)
    conclusion, created = migrations.submit(
        parse_migration_payload(migration_request()),
        audits.get_frozen_contract("AUDIT-SRC-1"),
    )
    assert created is True
    assert conclusion.sender_mapping == {"List": "Chain"}
    assert conclusion.receiver_mapping == {"List": "Chain"}
    assert conclusion.source_root == "List"
    assert conclusion.new_root == "Chain"
    assert conclusion.source_contract_fingerprint


def test_migration_resubmit_returns_frozen_conclusion(stores):
    audits, migrations = stores
    _submit_source(audits)
    c1, created1 = migrations.submit(
        parse_migration_payload(migration_request()),
        audits.get_frozen_contract("AUDIT-SRC-1"),
    )
    c2, created2 = migrations.submit(
        parse_migration_payload(migration_request()),
        audits.get_frozen_contract("AUDIT-SRC-1"),
    )
    assert created1 is True and created2 is False
    assert c2.frozen_at == c1.frozen_at
    assert c2.migration_fingerprint == c1.migration_fingerprint


def test_migration_conflict_on_changed_new_contract(stores):
    audits, migrations = stores
    _submit_source(audits)
    c1, _ = migrations.submit(
        parse_migration_payload(migration_request()),
        audits.get_frozen_contract("AUDIT-SRC-1"),
    )
    changed = migration_request()
    changed["new_sender_types"] = list_decl("Renamed")  # 仍是等价改名，但请求已变
    changed["new_receiver_types"] = list_decl("Renamed")
    changed["new_root_name"] = "Renamed"
    with pytest.raises(ContractConflictError):
        migrations.submit(
            parse_migration_payload(changed),
            audits.get_frozen_contract("AUDIT-SRC-1"),
        )
    # 原冻结结论不被改写。
    again = migrations.get("MIG-1")
    assert again.new_root == "Chain"
    assert again.frozen_at == c1.frozen_at


def test_migration_conflict_on_changed_source(stores):
    audits, migrations = stores
    _submit_source(audits)
    _submit_source(audits, audit_id="AUDIT-SRC-2")
    migrations.submit(
        parse_migration_payload(migration_request()),
        audits.get_frozen_contract("AUDIT-SRC-1"),
    )
    changed = migration_request(source="AUDIT-SRC-2")
    with pytest.raises(ContractConflictError):
        migrations.submit(
            parse_migration_payload(changed),
            audits.get_frozen_contract("AUDIT-SRC-2"),
        )


def test_migration_rejected_when_source_missing(stores):
    audits, migrations = stores
    with pytest.raises(MigrationRejectedError) as ei:
        migrations.submit(parse_migration_payload(migration_request()), None)
    assert ei.value.reason == "source-audit-not-found"


def test_migration_rejected_when_source_incompatible(stores):
    audits, migrations = stores
    _submit_source(audits, compatible=False)
    with pytest.raises(MigrationRejectedError) as ei:
        migrations.submit(
            parse_migration_payload(migration_request()),
            audits.get_frozen_contract("AUDIT-SRC-1"),
        )
    assert ei.value.reason == "source-audit-incompatible"


def test_migration_rejected_on_count_mismatch(stores):
    audits, migrations = stores
    _submit_source(audits)
    req = migration_request()
    req["new_sender_types"] = list_decl("Chain") + [
        {"name": "Extra", "type": INT}
    ]
    with pytest.raises(MigrationRejectedError) as ei:
        migrations.submit(
            parse_migration_payload(req),
            audits.get_frozen_contract("AUDIT-SRC-1"),
        )
    assert ei.value.reason == "declaration-count-mismatch"


def test_migration_rejected_on_unreachable_mismatch(stores):
    audits, migrations = stores
    # 来源：根 R 可达全部；另附一个不可达定义 U。
    contract = {
        "audit_id": "AUDIT-SRC-1",
        "root_name": "R",
        "sender_types": [
            {"name": "R", "type": {"kind": "record", "fields": [
                {"name": "x", "type": INT, "required": True},
            ]}},
            {"name": "U", "type": {"kind": "record", "fields": [
                {"name": "y", "type": TEXT, "required": True},
            ]}},
        ],
        "receiver_types": [
            {"name": "R", "type": {"kind": "record", "fields": [
                {"name": "x", "type": INT, "required": True},
            ]}},
            {"name": "U", "type": {"kind": "record", "fields": [
                {"name": "y", "type": TEXT, "required": True},
            ]}},
        ],
    }
    audits.submit(parse_payload(contract))
    # 新版：数量相同但 U2 自根可达，不可达定义数量不一致。
    req = {
        "migration_id": "MIG-1",
        "source_audit_id": "AUDIT-SRC-1",
        "new_root_name": "R2",
        "new_sender_types": [
            {"name": "R2", "type": {"kind": "record", "fields": [
                {"name": "x", "type": INT, "required": True},
                {"name": "u", "type": {"kind": "ref", "name": "U2"},
                 "required": False},
            ]}},
            {"name": "U2", "type": {"kind": "record", "fields": [
                {"name": "y", "type": TEXT, "required": True},
            ]}},
        ],
        "new_receiver_types": [
            {"name": "R2", "type": {"kind": "record", "fields": [
                {"name": "x", "type": INT, "required": True},
                {"name": "u", "type": {"kind": "ref", "name": "U2"},
                 "required": False},
            ]}},
            {"name": "U2", "type": {"kind": "record", "fields": [
                {"name": "y", "type": TEXT, "required": True},
            ]}},
        ],
    }
    with pytest.raises(MigrationRejectedError) as ei:
        migrations.submit(
            parse_migration_payload(req),
            audits.get_frozen_contract("AUDIT-SRC-1"),
        )
    assert ei.value.reason == "unreachable-definitions-mismatch"


def test_migration_rejected_when_structure_changed(stores):
    audits, migrations = stores
    _submit_source(audits)
    req = migration_request()
    req["new_sender_types"][0]["type"]["tags"][1]["type"]["fields"][0]["name"] = "renamed"
    with pytest.raises(MigrationRejectedError) as ei:
        migrations.submit(
            parse_migration_payload(req),
            audits.get_frozen_contract("AUDIT-SRC-1"),
        )
    assert ei.value.reason == "name-bijection-not-found"


def test_rejection_freezes_nothing_and_retry_can_succeed(stores):
    audits, migrations = stores
    _submit_source(audits)
    bad = migration_request()
    bad["new_sender_types"][0]["type"]["tags"][1]["type"]["fields"][0]["name"] = "renamed"
    with pytest.raises(MigrationRejectedError):
        migrations.submit(
            parse_migration_payload(bad),
            audits.get_frozen_contract("AUDIT-SRC-1"),
        )
    with pytest.raises(MigrationNotFoundError):
        migrations.get("MIG-1")
    # 同一迁移标识修正后可正常签发（拒绝未冻结任何记录）。
    conclusion, created = migrations.submit(
        parse_migration_payload(migration_request()),
        audits.get_frozen_contract("AUDIT-SRC-1"),
    )
    assert created is True and conclusion.sender_mapping == {"List": "Chain"}


def test_migration_persistence_across_store_instances(tmp_path):
    directory = tmp_path / "data"
    audits = AuditStore(directory)
    _submit_source(audits)
    MigrationStore(directory).submit(
        parse_migration_payload(migration_request()),
        audits.get_frozen_contract("AUDIT-SRC-1"),
    )
    again = MigrationStore(directory).get("MIG-1")
    assert again.sender_mapping == {"List": "Chain"}
    assert again.source_audit_id == "AUDIT-SRC-1"


def test_migration_id_may_equal_audit_id_without_interference(stores):
    audits, migrations = stores
    _submit_source(audits, audit_id="SHARED-ID")
    req = migration_request(migration_id="SHARED-ID", source="SHARED-ID")
    conclusion, created = migrations.submit(
        parse_migration_payload(req), audits.get_frozen_contract("SHARED-ID")
    )
    assert created is True
    # 两个命名空间互不影响：审计结论与迁移结论都可重开。
    assert audits.get("SHARED-ID").compatible is True
    assert migrations.get("SHARED-ID").new_root == "Chain"


# ---------- HTTP 接口 ----------


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


def test_api_migration_lifecycle(server):
    status, data = _request(f"{server}/api/audits", "POST", source_contract())
    assert status == 201 and data["conclusion"]["compatible"] is True

    req = migration_request()
    status, data = _request(f"{server}/api/migrations", "POST", req)
    assert status == 201
    c = data["conclusion"]
    assert c["equivalent"] is True
    assert c["sender_mapping"] == {"List": "Chain"}
    assert c["receiver_mapping"] == {"List": "Chain"}
    assert c["source_root"] == "List" and c["new_root"] == "Chain"

    # 相同请求重传：200，读取原冻结结论。
    status, data = _request(f"{server}/api/migrations", "POST", req)
    assert status == 200 and data["resubmitted_same_request"] is True
    assert data["conclusion"]["frozen_at"] == c["frozen_at"]

    # 改换新版契约：409，原结论不改写。
    changed = json.loads(json.dumps(req))
    changed["new_sender_types"][0]["type"]["tags"][0]["label"] = "Empty"
    status, data = _request(f"{server}/api/migrations", "POST", changed)
    assert status == 409 and data["error"] == "migration-conflict"

    # 改换来源审计：409。
    changed = json.loads(json.dumps(req))
    changed["source_audit_id"] = "AUDIT-OTHER"
    status, data = _request(f"{server}/api/migrations", "POST", changed)
    assert status == 409 and data["error"] == "migration-conflict"

    # 重开：仍是原结论。
    status, data = _request(f"{server}/api/migrations/MIG-1")
    assert status == 200
    assert data["conclusion"]["frozen_at"] == c["frozen_at"]
    assert data["conclusion"]["sender_mapping"] == {"List": "Chain"}

    # 原兼容审计及其 API 行为保持可用。
    status, data = _request(f"{server}/api/audits/AUDIT-SRC-1")
    assert status == 200 and data["conclusion"]["compatible"] is True


def test_api_migration_source_missing(server):
    status, data = _request(
        f"{server}/api/migrations", "POST", migration_request()
    )
    assert status == 404
    assert data["error"] == "migration-rejected"
    assert data["reason"] == "source-audit-not-found"


def test_api_migration_source_incompatible(server):
    _submit = source_contract()
    _submit["receiver_types"] = [{
        "name": "List",
        "type": {"kind": "variant", "tags": [
            {"label": "Nil", "type": {"kind": "record", "fields": []}},
        ]},
    }]
    status, data = _request(f"{server}/api/audits", "POST", _submit)
    assert status == 201 and data["conclusion"]["compatible"] is False

    status, data = _request(
        f"{server}/api/migrations", "POST", migration_request()
    )
    assert status == 422
    assert data["reason"] == "source-audit-incompatible"


def test_api_migration_not_equivalent(server):
    status, _ = _request(f"{server}/api/audits", "POST", source_contract())
    assert status == 201
    req = migration_request()
    req["new_receiver_types"][0]["type"]["tags"][1]["type"]["fields"][0]["type"] = TEXT
    status, data = _request(f"{server}/api/migrations", "POST", req)
    assert status == 422
    assert data["error"] == "migration-rejected"
    assert data["reason"] == "name-bijection-not-found"
    # 拒绝不写入记录：重开 404。
    status, data = _request(f"{server}/api/migrations/MIG-1")
    assert status == 404 and data["error"] == "migration-not-found"


def test_api_migration_count_mismatch(server):
    status, _ = _request(f"{server}/api/audits", "POST", source_contract())
    assert status == 201
    req = migration_request()
    req["new_sender_types"] = list_decl("Chain") + [
        {"name": "Extra", "type": INT}
    ]
    status, data = _request(f"{server}/api/migrations", "POST", req)
    assert status == 422 and data["reason"] == "declaration-count-mismatch"


def test_api_migration_validation_issues_batched(server):
    bad = {
        "migration_id": "bad id",
        "source_audit_id": "AUDIT-SRC-1",
        "new_sender_types": [],
        "new_receiver_types": [{"name": "1x", "type": {"kind": "wat"}}],
    }
    status, data = _request(f"{server}/api/migrations", "POST", bad)
    assert status == 400
    assert data["error"] == "validation-failed"
    assert len(data["issues"]) >= 2


def test_api_migration_reopen_missing(server):
    status, data = _request(f"{server}/api/migrations/NOPE")
    assert status == 404 and data["error"] == "migration-not-found"
