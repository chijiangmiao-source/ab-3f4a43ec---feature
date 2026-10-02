"""迁移结论的签发与冻结存储。

签发条件（全部满足才冻结结论）：
- 来源审计存在且结论为兼容；
- 新版发送端、接收端声明分别与来源对应声明完全结构等价
  （允许声明重排与类型名整体替换，取得稳定选出的完整名称双射）。

冻结规则与审计一致：
- 相同 migration_id + 完全相同迁移请求（来源审计标识 + 新版契约）：
  返回原冻结结论，不重新计算、不改变 frozen_at；
- 相同 migration_id 但改换来源或新版契约：拒绝（409），绝不改写原结论；
- 任何拒绝都不写入记录；结论持久化为 JSON 文件，重启后仍可重开。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .equivalence import check_migration_equivalence
from .models import (
    AuditConclusion,
    ContractConflictError,
    MigrationConclusion,
    MigrationNotFoundError,
    MigrationPayload,
    MigrationRejectedError,
    Payload,
)


def migration_fingerprint(payload: MigrationPayload) -> str:
    """迁移请求的稳定指纹：迁移标识 + 来源审计标识 + 新版契约。"""
    blob = json.dumps(
        payload.fingerprint_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MigrationStore:
    """迁移结论存储：与审计同根目录，独立 migrations/ 子目录，互不干扰。"""

    def __init__(self, directory: str | os.PathLike[str]):
        self.dir = Path(directory) / "migrations"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, migration_id: str) -> Path:
        # migration_id 已由 parser 限定为 [A-Za-z0-9_-]，无路径穿越风险。
        return self.dir / f"{migration_id}.json"

    def get(self, migration_id: str) -> MigrationConclusion:
        record = self._read_raw(migration_id)
        if record is None:
            raise MigrationNotFoundError(migration_id)
        return _migration_from_json(record["conclusion"])

    def _read_raw(self, migration_id: str) -> dict | None:
        path = self._path(migration_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def submit(
        self,
        payload: MigrationPayload,
        source: tuple[Payload, AuditConclusion] | None,
    ) -> tuple[MigrationConclusion, bool]:
        """提交（或幂等重传）迁移请求。返回 (结论, 是否本次新建)。"""
        fingerprint = migration_fingerprint(payload)
        with self._lock:
            existing = self._read_raw(payload.migration_id)
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise ContractConflictError(payload.migration_id)
                # 相同请求重传：读取原冻结结论，绝不改写。
                return _migration_from_json(existing["conclusion"]), False

            if source is None:
                raise MigrationRejectedError(
                    "source-audit-not-found",
                    f"来源审计 {payload.source_audit_id!r} 不存在",
                    {"source_audit_id": payload.source_audit_id},
                )
            source_payload, source_conclusion = source
            if not source_conclusion.compatible:
                raise MigrationRejectedError(
                    "source-audit-incompatible",
                    f"来源审计 {payload.source_audit_id!r} 的结论为不兼容，不能迁移",
                    {"source_audit_id": payload.source_audit_id},
                )

            sender_mapping, receiver_mapping = check_migration_equivalence(
                source_payload.sender_types,
                source_payload.receiver_types,
                source_payload.root_name or "",
                payload.new_sender_types,
                payload.new_receiver_types,
                payload.new_root_name or "",
            )

            conclusion = MigrationConclusion(
                migration_id=payload.migration_id,
                source_audit_id=payload.source_audit_id,
                source_root=source_payload.root_name or "",
                new_root=payload.new_root_name or "",
                sender_mapping=sender_mapping,
                receiver_mapping=receiver_mapping,
                source_contract_fingerprint=source_conclusion.contract_fingerprint,
                migration_fingerprint=fingerprint,
                frozen_at=_utc_now(),
            )
            record = {
                "request_fingerprint": fingerprint,
                # 冻结原始迁移请求，便于重开页面时回放与审计。
                "request": payload.fingerprint_dict(),
                "conclusion": conclusion.to_json(),
            }
            tmp = self._path(payload.migration_id).with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path(payload.migration_id))
            return conclusion, True


def _migration_from_json(data: dict) -> MigrationConclusion:
    return MigrationConclusion(
        migration_id=data["migration_id"],
        source_audit_id=data["source_audit_id"],
        source_root=data["source_root"],
        new_root=data["new_root"],
        sender_mapping=dict(data["sender_mapping"]),
        receiver_mapping=dict(data["receiver_mapping"]),
        source_contract_fingerprint=data["source_contract_fingerprint"],
        migration_fingerprint=data["migration_fingerprint"],
        frozen_at=data["frozen_at"],
    )
