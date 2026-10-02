"""审计与迁移签证 HTTP API 及静态页面（仅用标准库）。

接口：
- GET  /health                         健康检查 -> {"status":"ok"}
- POST /api/audits                     提交/重传审计载荷
- GET  /api/audits/{audit_id}          重开冻结结论
- POST /api/migrations                 提交/重传迁移签证请求
- GET  /api/migrations/{migration_id}  重开冻结迁移签证
- GET  /                               页面
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .models import (
    AuditNotFoundError,
    ContractConflictError,
    MigrationConflictError,
    MigrationNotFoundError,
    MigrationRejectedError,
    ValidationError,
)
from .parser import parse_migration_request, parse_payload
from .storage import AuditStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 2 * 1024 * 1024  # 2 MiB：每套至多 24 个类型，足够。


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "PayloadAudit/1.0"
    store: AuditStore  # 由工厂注入到类

    def log_message(self, fmt: str, *args) -> None:  # 安静的容器日志
        if os.environ.get("AUDIT_HTTP_LOG"):
            super().log_message(fmt, *args)

    # ---------- 工具 ----------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, filename: str, content_type: str) -> None:
        path = STATIC_DIR / filename
        if not path.is_file():
            self._send_json(404, {"error": "not-found"})
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------
    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        route = urlsplit(self.path)
        if route.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if route.path.startswith("/api/audits/"):
            audit_id = route.path.rsplit("/", 1)[-1]
            try:
                conclusion = self.store.get(audit_id)
            except AuditNotFoundError:
                self._send_json(404, {"error": "audit-not-found", "audit_id": audit_id})
                return
            self._send_json(200, {"conclusion": conclusion.to_json()})
            return
        if route.path.startswith("/api/migrations/"):
            migration_id = route.path.rsplit("/", 1)[-1]
            try:
                conclusion = self.store.get_migration(migration_id)
            except MigrationNotFoundError:
                self._send_json(
                    404,
                    {"error": "migration-not-found", "migration_id": migration_id},
                )
                return
            self._send_json(200, {"migration": conclusion.to_json()})
            return
        if route.path in ("/", "/index.html"):
            self._send_static("index.html", "text/html; charset=utf-8")
            return
        if route.path == "/app.js":
            self._send_static("app.js", "application/javascript; charset=utf-8")
            return
        if route.path == "/styles.css":
            self._send_static("styles.css", "text/css; charset=utf-8")
            return
        self._send_json(404, {"error": "not-found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/audits":
            self._post_audit()
            return
        if path == "/api/migrations":
            self._post_migration()
            return
        self._send_json(404, {"error": "not-found"})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send_json(413, {"error": "body-size-invalid"})
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid-json"})
            return None

    def _post_audit(self) -> None:
        data = self._read_body()
        if data is None:
            return

        try:
            payload = parse_payload(data)
        except ValidationError as exc:
            # 所有契约问题一次反馈。
            self._send_json(400, {"error": "validation-failed", "issues": exc.issues})
            return

        try:
            conclusion, created = self.store.submit(payload)
        except ContractConflictError:
            self._send_json(
                409,
                {
                    "error": "contract-conflict",
                    "audit_id": payload.audit_id,
                    "message": (
                        "审计标识已冻结另一契约：改变任一契约将被拒绝，"
                        "原结论不会被改写"
                    ),
                },
            )
            return

        self._send_json(
            201 if created else 200,
            {
                "conclusion": conclusion.to_json(),
                "frozen": True,
                "resubmitted_same_contract": not created,
            },
        )

    def _post_migration(self) -> None:
        data = self._read_body()
        if data is None:
            return

        try:
            payload = parse_migration_request(data)
        except ValidationError as exc:
            self._send_json(400, {"error": "validation-failed", "issues": exc.issues})
            return

        try:
            migration, created = self.store.submit_migration(payload)
        except MigrationRejectedError as exc:
            # 来源缺失/不兼容、数量不一致、无法建立双射：明确拒绝，不写记录。
            self._send_json(
                422,
                {
                    "error": "migration-rejected",
                    "migration_id": payload.migration_id,
                    "reasons": exc.reasons,
                },
            )
            return
        except MigrationConflictError:
            self._send_json(
                409,
                {
                    "error": "migration-conflict",
                    "migration_id": payload.migration_id,
                    "message": (
                        "迁移标识已冻结另一来源审计或新版契约："
                        "改换来源或新版契约将被拒绝，原签证不会被改写"
                    ),
                },
            )
            return

        self._send_json(
            201 if created else 200,
            {
                "migration": migration.to_json(),
                "frozen": True,
                "resubmitted_same_request": not created,
            },
        )


def build_server(host: str, port: int, store: AuditStore) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"store": store})
    server = ThreadingHTTPServer((host, port), handler)
    return server
