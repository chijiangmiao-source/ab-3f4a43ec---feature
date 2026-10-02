#!/usr/bin/env python3
"""单次验收组件（verify）：

按顺序执行，任一步失败即以非零退出码报告：
  1. 复核递归兼容、字段缺失、额外发送变体、可空性变化等裁决（直连引擎）；
  2. 代码测试（pytest）；
  3. 构建检查（全量字节码编译 + 关键模块导入）；
  4. 审计接口冒烟（/health、提交、幂等重传、契约冲突 409、重开）；
  5. 迁移签证冒烟（改名/重排双射、重传、改换来源/契约 409、
     结构不等价/来源缺失 422、重启后重开）。

接口冒烟默认在进程内临时起服；当设置环境变量
AUDIT_SMOKE_BASE_URL（如 compose 中指向 http://web:8080）时，
直接对正在运行的容器做冒烟。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api import build_server  # noqa: E402
from app.parser import parse_payload  # noqa: E402
from app.storage import AuditStore  # noqa: E402
from app.subtype import check_compatibility  # noqa: E402

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


class Failure(Exception):
    pass


def check(step, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {step}{(' — ' + detail) if detail else ''}")
    if not condition:
        raise Failure(step)


# ---------------------------------------------------------------- 阶段 1

def engine_verdict(body):
    p = parse_payload(body)
    ok, mismatch, recycled = check_compatibility(
        p.sender_types, p.receiver_types, p.root_name
    )
    return ok, mismatch, recycled


def phase_engine_recheck():
    print("[1/4] 复核递归兼容裁决核心结果")

    list_decl = [
        {"name": "List", "type": variant(
            tag("Nil", rec()),
            tag("Cons", rec(
                field("head", INT),
                field("tail", ref("List"), required=False),
            )),
        )}
    ]
    ok, _, recycled = engine_verdict({
        "audit_id": "verify-list", "root_name": "List",
        "sender_types": list_decl, "receiver_types": list_decl,
    })
    check("递归列表自相容（协归，无深度截断）", ok)
    check(
        "递归处标出已复用比较对 List <= List",
        any("List <= List" in e.pair for e in recycled),
        f"{len(recycled)} 个复用点",
    )

    ok, mismatch, _ = engine_verdict({
        "audit_id": "verify-missing",
        "sender_types": [{"name": "R", "type": rec(field("a", INT))}],
        "receiver_types": [{"name": "R", "type": rec(
            field("a", INT), field("b", TEXT))}],
    })
    check("接收端必需字段缺失被拒", not ok and mismatch.code == "field-missing")
    check("首个违约路径稳定为 R.b", mismatch.path == "R.b", mismatch.path)

    ok, mismatch, _ = engine_verdict({
        "audit_id": "verify-extra-tag",
        "sender_types": [{"name": "R", "type": variant(
            tag("A", INT), tag("B", INT))}],
        "receiver_types": [{"name": "R", "type": variant(tag("A", INT))}],
    })
    check("额外发送变体被拒", not ok and mismatch.code == "extra-tag")
    check("首个违约标签稳定为 R[B]", mismatch.path == "R[B]", mismatch.path)

    ok, mismatch, _ = engine_verdict({
        "audit_id": "verify-nullability",
        "sender_types": [{"name": "R", "type": rec(field("a", INT, False))}],
        "receiver_types": [{"name": "R", "type": rec(field("a", INT, True))}],
    })
    check("必需->可选可空性变化被拒",
          not ok and mismatch.code == "optional-required")

    # 深层递归类型不一致：int vs text，必须沿递归裁决到基本类型。
    s_list = [{"name": "List", "type": variant(
        tag("Nil", rec()),
        tag("Cons", rec(field("head", INT),
                        field("tail", ref("List"), False))),
    )}]
    r_list = [{"name": "List", "type": variant(
        tag("Nil", rec()),
        tag("Cons", rec(field("head", TEXT),
                        field("tail", ref("List"), False))),
    )}]
    ok, mismatch, _ = engine_verdict({
        "audit_id": "verify-deep", "root_name": "List",
        "sender_types": s_list, "receiver_types": r_list,
    })
    check("递归深层基本类型违约被发现",
          not ok and mismatch.code == "primitive-mismatch",
          getattr(mismatch, "path", ""),
    )


# ---------------------------------------------------------------- 阶段 2/3

def phase_tests():
    print("[2/5] 执行代码测试（pytest）")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT,
    )
    check("pytest 全部通过", proc.returncode == 0, f"exit={proc.returncode}")


def phase_build():
    print("[3/5] 构建检查（字节码编译与模块导入）")
    proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "app", "scripts", "tests"],
        cwd=ROOT,
    )
    check("compileall 成功", proc.returncode == 0)
    proc = subprocess.run(
        [sys.executable, "-c",
         "import app.main, app.api, app.storage, app.subtype, app.parser"],
        cwd=ROOT,
    )
    check("关键模块均可导入", proc.returncode == 0)


# ---------------------------------------------------------------- 阶段 4

class _LocalServer:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = AuditStore(Path(self.tmp.name) / "data")
        self.server = build_server("127.0.0.1", 0, store)
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.base_url

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()


def http(method, url, body=None, timeout=5):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_health(base_url, attempts=30):
    for _ in range(attempts):
        try:
            status, data = http("GET", f"{base_url}/health")
            if status == 200 and data.get("status") == "ok":
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def phase_api_smoke(base_url):
    print(f"[4/5] 审计接口冒烟（{base_url}）")
    check("健康检查 /health 可达", wait_health(base_url))

    stamp = str(int(time.time() * 1000))
    ok_id = f"SMOKE-OK-{stamp}"

    recursive = {
        "audit_id": ok_id,
        "root_name": "List",
        "sender_types": [{"name": "List", "type": variant(
            tag("Nil", rec()),
            tag("Cons", rec(field("head", INT),
                            field("tail", ref("List"), False))),
        )}],
        "receiver_types": [{"name": "List", "type": variant(
            tag("Nil", rec()),
            tag("Cons", rec(field("head", INT),
                            field("tail", ref("List"), False))),
        )}],
    }
    status, data = http("POST", f"{base_url}/api/audits", recursive)
    check("递归契约提交得 201 且兼容",
          status == 201 and data["conclusion"]["compatible"] is True,
          f"status={status}")

    status, data = http("POST", f"{base_url}/api/audits", recursive)
    check("相同契约重传得 200 且标记幂等",
          status == 200 and data.get("resubmitted_same_contract") is True,
          f"status={status}")
    frozen_at = data["conclusion"]["frozen_at"]

    changed = json.loads(json.dumps(recursive))
    changed["receiver_types"][0]["type"]["tags"][1]["type"]["fields"][0]["type"] = TEXT
    status, data = http("POST", f"{base_url}/api/audits", changed)
    check("改变契约得 409 且拒绝改写",
          status == 409 and data["error"] == "contract-conflict",
          f"status={status}")

    status, data = http("GET", f"{base_url}/api/audits/{ok_id}")
    check("重开仍读原冻结兼容结论",
          status == 200
          and data["conclusion"]["compatible"] is True
          and data["conclusion"]["frozen_at"] == frozen_at,
          f"status={status}")

    bad_id = f"SMOKE-MISSING-{stamp}"
    missing = {
        "audit_id": bad_id,
        "sender_types": [{"name": "R", "type": rec(field("a", INT))}],
        "receiver_types": [{"name": "R", "type": rec(
            field("a", INT), field("b", TEXT))}],
    }
    status, data = http("POST", f"{base_url}/api/audits", missing)
    check("字段缺失契约返回不兼容与稳定路径",
          status == 201
          and data["conclusion"]["compatible"] is False
          and data["conclusion"]["mismatch"]["path"] == "R.b",
          f"status={status}")

    tag_id = f"SMOKE-TAG-{stamp}"
    extra_tag = {
        "audit_id": tag_id,
        "sender_types": [{"name": "R", "type": variant(
            tag("A", INT), tag("B", INT))}],
        "receiver_types": [{"name": "R", "type": variant(tag("A", INT))}],
    }
    status, data = http("POST", f"{base_url}/api/audits", extra_tag)
    check("额外发送变体返回 extra-tag",
          status == 201
          and data["conclusion"]["mismatch"]["code"] == "extra-tag",
          f"status={status}")

    bad_body = {"audit_id": "bad id", "sender_types": [], "receiver_types": []}
    status, data = http("POST", f"{base_url}/api/audits", bad_body)
    check("非法契约一次返回多条问题",
          status == 400 and len(data.get("issues", [])) >= 2,
          f"issues={len(data.get('issues', []))}")


def phase_migration_smoke(base_url):
    print("[5/5] 迁移签证接口冒烟（改名/重排/冻结/拒绝）")
    stamp = str(int(time.time() * 1000))

    # 来源：冻结一份兼容审计（发送端是接收端子类型）。
    src_sender = [
        {"name": "Cmd", "type": rec(field("id", INT), field("p", ref("Payload")))},
        {"name": "Payload", "type": variant(
            tag("Nil", rec()),
            tag("Cons", rec(field("head", INT),
                            field("tail", ref("Payload"), False))))},
    ]
    src_receiver = [
        {"name": "Cmd", "type": rec(
            field("id", INT), field("p", ref("Payload")),
            field("trace", BOOL, False))},
        {"name": "Payload", "type": variant(
            tag("Nil", rec()),
            tag("Cons", rec(field("head", INT),
                            field("tail", ref("Payload"), False))),
            tag("Z", INT))},
    ]
    src_id = f"SMOKE-MIGSRC-{stamp}"
    status, data = http("POST", f"{base_url}/api/audits", {
        "audit_id": src_id, "root_name": "Cmd",
        "sender_types": src_sender, "receiver_types": src_receiver,
    })
    check("来源兼容审计冻结成功",
          status == 201 and data["conclusion"]["compatible"] is True,
          f"status={status}")

    # 新版：类型名整体替换、声明/字段/标签重排，结构完全等价。
    new_sender = [
        {"name": "Pl", "type": variant(
            tag("Cons", rec(field("tail", ref("Pl"), False), field("head", INT))),
            tag("Nil", rec()))},
        {"name": "Command", "type": rec(field("p", ref("Pl")), field("id", INT))},
    ]
    new_receiver = [
        {"name": "Command", "type": rec(
            field("trace", BOOL, False), field("id", INT), field("p", ref("Pl")))},
        {"name": "Pl", "type": variant(
            tag("Z", INT), tag("Nil", rec()),
            tag("Cons", rec(field("head", INT),
                            field("tail", ref("Pl"), False))))},
    ]
    mig_id = f"SMOKE-MIG-{stamp}"
    request = {
        "migration_id": mig_id, "source_audit_id": src_id,
        "new_root_name": "Command",
        "new_sender_types": new_sender, "new_receiver_types": new_receiver,
    }
    status, data = http("POST", f"{base_url}/api/migrations", request)
    check("改名重排迁移得 201 且签发双射",
          status == 201
          and {p["source"]: p["target"] for p in data["migration"]["sender_bijection"]}
          == {"Cmd": "Command", "Payload": "Pl"}
          and {p["source"]: p["target"] for p in data["migration"]["receiver_bijection"]}
          == {"Cmd": "Command", "Payload": "Pl"},
          f"status={status}")
    frozen_at = data["migration"]["frozen_at"]

    status, data = http("POST", f"{base_url}/api/migrations", request)
    check("相同迁移请求重传得 200 且读取原签证",
          status == 200
          and data.get("resubmitted_same_request") is True
          and data["migration"]["frozen_at"] == frozen_at,
          f"status={status}")

    # 改换来源 -> 409。
    switched = json.loads(json.dumps(request))
    switched["source_audit_id"] = f"{src_id}-OTHER"
    status, data = http("POST", f"{base_url}/api/migrations", switched)
    check("改换来源审计得 409", status == 409, f"status={status}")

    # 改换新版契约（字段改名）-> 409，原签证不变。
    changed = json.loads(json.dumps(request))
    changed["new_sender_types"][1]["type"]["fields"][1]["name"] = "id2"
    status, data = http("POST", f"{base_url}/api/migrations", changed)
    check("改换新版契约得 409", status == 409, f"status={status}")
    status, data = http("GET", f"{base_url}/api/migrations/{mig_id}")
    check("重开仍读原冻结签证",
          status == 200 and data["migration"]["frozen_at"] == frozen_at,
          f"status={status}")

    # 新迁移标识下的不等价新版 -> 422，且不留记录。
    bad_mig = json.loads(json.dumps(request))
    bad_mig["migration_id"] = f"SMOKE-MIGBAD-{stamp}"
    bad_mig["new_sender_types"][1]["type"]["fields"][1]["type"] = TEXT
    status, data = http("POST", f"{base_url}/api/migrations", bad_mig)
    check("结构不等价迁移得 422 且给出原因",
          status == 422
          and data.get("error") == "migration-rejected"
          and len(data.get("reasons", [])) >= 1,
          f"status={status}")
    status, _ = http("GET", f"{base_url}/api/migrations/{bad_mig['migration_id']}")
    check("被拒迁移不留冻结记录", status == 404, f"status={status}")

    # 来源不存在 -> 422。
    miss = json.loads(json.dumps(request))
    miss["migration_id"] = f"SMOKE-MIGMISS-{stamp}"
    miss["source_audit_id"] = f"NOPE-{stamp}"
    status, data = http("POST", f"{base_url}/api/migrations", miss)
    check("来源不存在得 422",
          status == 422 and any("不存在" in r for r in data.get("reasons", [])),
          f"status={status}")


def main() -> int:
    base_url = os.environ.get("AUDIT_SMOKE_BASE_URL")
    phases = [
        phase_engine_recheck,
        phase_tests,
        phase_build,
    ]
    try:
        for phase in phases:
            phase()
        if base_url:
            phase_api_smoke(base_url.rstrip("/"))
            phase_migration_smoke(base_url.rstrip("/"))
        else:
            with _LocalServer() as url:
                phase_api_smoke(url)
                phase_migration_smoke(url)
    except Failure as exc:
        print(f"\nVERIFY FAILED at: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 冒烟基础设施错误也算验收失败
        print(f"\nVERIFY ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print("\nVERIFY OK: 递归兼容复核、测试、构建检查、接口冒烟全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
