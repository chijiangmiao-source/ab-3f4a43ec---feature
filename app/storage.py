"""审计结论与迁移签证的冻结存储。

规则：
- 相同 audit_id + 完全相同契约（规范化 JSON 的 SHA-256）：返回原冻结结论，
  不重新计算、不改变 frozen_at；
- 相同 audit_id 但契约指纹变化：拒绝（409），绝不改写原结论；
- 迁移签证以 migration_id 冻结：相同请求重传读取原签证；
  改换来源审计标识或新版契约 → 409，绝不改写原签证；
- 结论持久化为 JSON 文件，容器重启后仍可重开。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    AuditConclusion,
    AuditNotFoundError,
    ContractConflictError,
    MigrationConclusion,
    MigrationConflictError,
    MigrationNotFoundError,
    MigrationPayload,
    MigrationRejectedError,
    NamePair,
    Payload,
    RecyclePoint,
    Mismatch,
)
from .equivalence import establish_bijection, reachable_names
from .parser import parse_frozen_contract
from .subtype import check_compatibility


def canonical_fingerprint(payload: Payload) -> str:
    """契约的稳定指纹：紧凑、排序键、ensure_ascii=False 不影响字节稳定性。"""
    blob = json.dumps(
        payload.fingerprint_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def migration_fingerprint(payload: MigrationPayload) -> str:
    """迁移请求（来源标识 + 新版两侧契约 + 新版根）的稳定指纹。"""
    blob = json.dumps(
        payload.fingerprint_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditStore:
    def __init__(self, directory: str | os.PathLike[str]):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mig_dir = self.dir / "migrations"
        self.mig_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._mig_lock = threading.Lock()

    def _path(self, audit_id: str) -> Path:
        # audit_id 已由 parser 限定为 [A-Za-z0-9_-]，无路径穿越风险。
        return self.dir / f"{audit_id}.json"

    def _mig_path(self, migration_id: str) -> Path:
        return self.mig_dir / f"{migration_id}.json"

    def get(self, audit_id: str) -> AuditConclusion:
        record = self._read_raw(audit_id)
        if record is None:
            raise AuditNotFoundError(audit_id)
        return _conclusion_from_json(record["conclusion"])

    def _read_raw(self, audit_id: str) -> dict | None:
        path = self._path(audit_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def submit(self, payload: Payload) -> tuple[AuditConclusion, bool]:
        """提交（或幂等重传）。返回 (结论, 是否本次新建)。"""
        fingerprint = canonical_fingerprint(payload)
        with self._lock:
            existing = self._read_raw(payload.audit_id)
            if existing is not None:
                if existing["contract_fingerprint"] != fingerprint:
                    raise ContractConflictError(payload.audit_id)
                # 相同契约重传：读取原冻结结论，绝不改写。
                return _conclusion_from_json(existing["conclusion"]), False

            ok, mismatch, recycled = check_compatibility(
                payload.sender_types, payload.receiver_types, payload.root_name or ""
            )
            conclusion = AuditConclusion(
                audit_id=payload.audit_id,
                compatible=ok,
                root=payload.root_name or "",
                mismatch=mismatch,
                recycled=recycled,
                contract_fingerprint=fingerprint,
                frozen_at=_utc_now(),
            )
            record = {
                "contract_fingerprint": fingerprint,
                # 冻结原始契约，便于重开页面时回放与审计。
                "contract": payload.fingerprint_dict(),
                "conclusion": conclusion.to_json(),
            }
            tmp = self._path(payload.audit_id).with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path(payload.audit_id))
            return conclusion, True

    # ---------------- 迁移签证 ----------------

    def _read_migration_raw(self, migration_id: str) -> dict | None:
        path = self._mig_path(migration_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def get_migration(self, migration_id: str) -> MigrationConclusion:
        record = self._read_migration_raw(migration_id)
        if record is None:
            raise MigrationNotFoundError(migration_id)
        return _migration_from_json(record["conclusion"])

    def submit_migration(
        self, payload: MigrationPayload
    ) -> tuple[MigrationConclusion, bool]:
        """签发（或幂等重开）迁移签证。返回 (签证, 是否本次新建)。

        拒绝条件（MigrationRejectedError，不写任何记录）：
        - 来源审计不存在，或来源结论不兼容（未判定兼容不得迁移）；
        - 新版任一侧声明数量 / 不可达定义数量与来源不一致；
        - 任一侧无法在根锚定下建立完整结构等价双射。

        冲突条件（MigrationConflictError，绝不改写已冻结签证）：
        同一 migration_id 改换来源审计标识或新版契约。
        """
        fingerprint = migration_fingerprint(payload)
        with self._mig_lock:
            existing = self._read_migration_raw(payload.migration_id)
            if existing is not None:
                if existing["migration_fingerprint"] != fingerprint:
                    raise MigrationConflictError(payload.migration_id)
                return _migration_from_json(existing["conclusion"]), False

            conclusion = self._issue_migration(payload, fingerprint)

            record = {
                "migration_fingerprint": fingerprint,
                "source_audit_id": payload.source_audit_id,
                # 冻结迁移请求，便于按标识重开与审计。
                "request": payload.fingerprint_dict(),
                "conclusion": conclusion.to_json(),
            }
            tmp = self._mig_path(payload.migration_id).with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._mig_path(payload.migration_id))
            return conclusion, True

    def _issue_migration(
        self, payload: MigrationPayload, fingerprint: str
    ) -> MigrationConclusion:
        reasons: list[str] = []

        source_raw = self._read_raw(payload.source_audit_id)
        if source_raw is None:
            raise MigrationRejectedError(
                [f"来源审计 {payload.source_audit_id!r} 不存在，无法迁移"]
            )
        source_conclusion = _conclusion_from_json(source_raw["conclusion"])
        if not source_conclusion.compatible:
            raise MigrationRejectedError(
                [
                    f"来源审计 {payload.source_audit_id!r} 的结论为不兼容"
                    f"（首个违约：{source_conclusion.mismatch.code}），"
                    "仅已判定兼容的冻结审计可以迁移"
                ]
            )

        source_contract = parse_frozen_contract(source_raw["contract"])
        source_root = source_contract.root_name or source_conclusion.root

        # 声明数量必须一致（每个具名类型都要有对应，不允许整体增减）。
        self._check_counts(
            source_contract.sender_types,
            payload.new_sender_types,
            source_root,
            payload.new_root_name,
            "发送端",
            reasons,
        )
        self._check_counts(
            source_contract.receiver_types,
            payload.new_receiver_types,
            source_root,
            payload.new_root_name,
            "接收端",
            reasons,
        )
        if reasons:
            raise MigrationRejectedError(reasons)

        # 两侧分别与来源对应声明做完全结构等价判定。
        sender_map, sender_reasons = establish_bijection(
            source_contract.sender_types,
            payload.new_sender_types,
            source_root,
            payload.new_root_name,
            "发送端",
        )
        receiver_map, receiver_reasons = establish_bijection(
            source_contract.receiver_types,
            payload.new_receiver_types,
            source_root,
            payload.new_root_name,
            "接收端",
        )
        if sender_map is None or receiver_map is None:
            raise MigrationRejectedError(
                (sender_reasons or []) + (receiver_reasons or [])
            )

        return MigrationConclusion(
            migration_id=payload.migration_id,
            source_audit_id=payload.source_audit_id,
            source_root=source_root,
            new_root=payload.new_root_name,
            sender_bijection=[NamePair(s, t) for s, t in sender_map.items()],
            receiver_bijection=[NamePair(s, t) for s, t in receiver_map.items()],
            source_audit_fingerprint=source_conclusion.contract_fingerprint,
            migration_fingerprint=fingerprint,
            frozen_at=_utc_now(),
        )

    @staticmethod
    def _check_counts(
        source: list,
        target: list,
        source_root: str,
        target_root: str,
        side: str,
        reasons: list[str],
    ) -> None:
        if len(source) != len(target):
            reasons.append(
                f"{side}：具名声明数量不一致，来源 {len(source)} 个，"
                f"新版 {len(target)} 个"
            )
            return
        # 总数量相同时，从根不可达的孤立定义数量也必须一致，
        # 否则必有来源具名类型在等价双射中落空。
        src_unreach = len(source) - len(reachable_names(source, source_root))
        tgt_unreach = len(target) - len(reachable_names(target, target_root))
        if src_unreach != tgt_unreach:
            reasons.append(
                f"{side}：不可达具名定义数量不一致，来源 {src_unreach} 个，"
                f"新版 {tgt_unreach} 个（声明总数均为 {len(source)} 个）"
            )


def _conclusion_from_json(data: dict) -> AuditConclusion:
    mm = data.get("mismatch")
    mismatch = None
    if mm:
        mismatch = Mismatch(
            path=mm["path"],
            code=mm["code"],
            message=mm["message"],
            detail=mm.get("detail", {}),
        )
    recycled = [
        RecyclePoint(
            path=r["path"], pair=r["pair"], first_seen_at=r["first_seen_at"]
        )
        for r in data.get("recycled", [])
    ]
    return AuditConclusion(
        audit_id=data["audit_id"],
        compatible=data["compatible"],
        root=data["root"],
        mismatch=mismatch,
        recycled=recycled,
        contract_fingerprint=data["contract_fingerprint"],
        frozen_at=data["frozen_at"],
    )


def _migration_from_json(data: dict) -> MigrationConclusion:
    def pairs(key: str) -> list[NamePair]:
        return [
            NamePair(source=p["source"], target=p["target"])
            for p in data.get(key, [])
        ]

    return MigrationConclusion(
        migration_id=data["migration_id"],
        source_audit_id=data["source_audit_id"],
        source_root=data["source_root"],
        new_root=data["new_root"],
        sender_bijection=pairs("sender_bijection"),
        receiver_bijection=pairs("receiver_bijection"),
        source_audit_fingerprint=data["source_audit_fingerprint"],
        migration_fingerprint=data["migration_fingerprint"],
        frozen_at=data["frozen_at"],
    )
