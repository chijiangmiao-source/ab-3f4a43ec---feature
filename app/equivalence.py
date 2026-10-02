"""结构等价判定与具名类型双射的稳定建立（迁移签证专用）。

来源审计的一侧声明与新版同一侧声明**完全结构等价**才允许迁移。
与协归子类型不同，这里是等价关系（同构），允许：

- 类型声明在列表中的顺序不同；
- 类型名被整体替换（来源每个具名类型在新版中有唯一对应）；

必须保持：

- 记录字段名集合与每个字段的必需性（字段按名匹配，顺序无关）；
- 变体标签集合与每个标签的载荷结构（标签按名匹配，顺序无关）；
- 基本类型 int / bool / text 仅与自身等价；
- 递归引用结构：每个具名引用必须解析到双射中唯一的对应具名类型。

双射分两段建立，结果对字段名/标签/类型名排序确定，因而可重放：

1. **可达定义**：从（来源根 -> 新版根）锚定，沿字段名/标签与具名引用
   结构性地唯一确定，环上比较对以“假设-复用”收口（协归）。
   一对多、多对一或任何结构偏差都判定为无法建立双射。
2. **不可达定义**：根不可达的孤立声明无法按位置锚定，对其做
   分区精化（双模拟标号），以可达双射为种子；每个等价分区两侧
   数量一致时，按类型名排序稳定配对，否则拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import NamedType, TypeExpr

PRIMITIVES = {"int", "bool", "text"}


@dataclass(frozen=True)
class _Node:
    path: str
    expr: TypeExpr


def reachable_names(decls: list[NamedType], root: str) -> set[str]:
    """从根出发可到达的全部具名定义（沿任意 ref，含别名与受保护引用）。"""
    by_name = {d.name: d.type for d in decls}
    seen: set[str] = set()
    stack = [root]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        refs: list[str] = []
        _collect_refs(by_name[name], refs)
        stack.extend(refs)
    return seen


def _collect_refs(t: TypeExpr, out: list[str]) -> None:
    if t.kind == "ref":
        out.append(t.ref_name or "")
    for f in t.fields or []:
        _collect_refs(f.type, out)
    for tg in t.tags or []:
        _collect_refs(tg.type, out)


# ============================================================ 可达定义

class _ReachableEquivalence:
    def __init__(
        self,
        source: list[NamedType],
        target: list[NamedType],
        source_root: str,
        target_root: str,
        side: str,
    ):
        self.src = {d.name: d.type for d in source}
        self.tgt = {d.name: d.type for d in target}
        self.src_root = source_root
        self.tgt_root = target_root
        self.side = side
        self.forward: dict[str, str] = {}   # 来源名 -> 新版名
        self.reverse: dict[str, str] = {}   # 新版名 -> 来源名
        self._assumed: set[tuple[str, str]] = set()
        self.reasons: list[str] = []

    def _reason(self, text: str) -> bool:
        msg = f"{self.side}：{text}"
        if msg not in self.reasons:
            self.reasons.append(msg)
        return False

    def _bind(self, s_name: str, t_name: str, context: str) -> bool:
        """把具名引用对记入双射；冲突即无法建立双射。"""
        known_t = self.forward.get(s_name)
        known_s = self.reverse.get(t_name)
        if known_t is not None and known_t != t_name:
            return self._reason(
                f"类型 {s_name} 在新版中同时对应 {known_t} 与 {t_name}"
                f"（{context}），无法形成唯一双射"
            )
        if known_s is not None and known_s != s_name:
            return self._reason(
                f"新版类型 {t_name} 同时对应来源 {known_s} 与 {s_name}"
                f"（{context}），无法形成唯一双射"
            )
        self.forward[s_name] = t_name
        self.reverse[t_name] = s_name
        return True

    def establish(self) -> bool:
        # 根类型对强制锚定，然后比较根的（别名展开后的）结构。
        if not self._bind(self.src_root, self.tgt_root, "根类型"):
            return False
        return self._check_named(
            _Node(self.src_root, self.src[self.src_root]),
            _Node(self.tgt_root, self.tgt[self.tgt_root]),
        )

    def _resolve(self, s: _Node, t: _Node, context: str):
        """同步沿别名链前进：每一跳的具名类型对都必须进入同一双射。"""
        while s.expr.kind == "ref" or t.expr.kind == "ref":
            if s.expr.kind != "ref" or t.expr.kind != "ref":
                self._reason(
                    f"{context}：别名展开不一致，来源为 {s.expr.kind}，"
                    f"新版为 {t.expr.kind}"
                )
                return None
            s_name = s.expr.ref_name or ""
            t_name = t.expr.ref_name or ""
            if not self._bind(s_name, t_name, context):
                return None
            s = _Node(s_name, self.src[s_name])
            t = _Node(t_name, self.tgt[t_name])
        return s, t

    def _check_named(self, s_node: _Node, t_node: _Node) -> bool:
        """比较一对具名类型的体（可能先穿过别名链），处理环上收口。"""
        resolved = self._resolve(
            s_node, t_node, f"类型 {s_node.path} ↔ {t_node.path}"
        )
        if resolved is None:
            return False
        s, t = resolved
        key = (s.path, t.path)
        if key in self._assumed:
            # 协归假设：递归比较对先假定成立，由外层展开的一致性兜底。
            return True
        self._assumed.add(key)
        try:
            ok = self._struct(s, t, f"类型 {s.path} ↔ {t.path}")
        finally:
            self._assumed.discard(key)
        return ok

    def _struct(self, s: _Node, t: _Node, context: str) -> bool:
        sk, tk = s.expr.kind, t.expr.kind
        if sk in PRIMITIVES or tk in PRIMITIVES:
            if sk != tk:
                return self._reason(
                    f"{context}：基础类型不一致，来源 {sk}，新版 {tk}"
                )
            return True
        if sk != tk:
            return self._reason(f"{context}：结构种类不一致，来源 {sk}，新版 {tk}")
        if sk == "record":
            return self._record(s, t, context)
        return self._variant(s, t, context)

    def _record(self, s: _Node, t: _Node, context: str) -> bool:
        s_fields = {f.name: f for f in s.expr.fields or []}
        t_fields = {f.name: f for f in t.expr.fields or []}
        ok = True
        missing = sorted(set(s_fields) - set(t_fields))
        extra = sorted(set(t_fields) - set(s_fields))
        if missing:
            self._reason(f"{context}：来源字段 {missing} 在新版记录中缺失")
            ok = False
        if extra:
            self._reason(f"{context}：新版记录出现来源没有的字段 {extra}")
            ok = False
        # 字段名相同的部分：必需性与值类型必须等价；按字段名稳定遍历。
        for name in sorted(set(s_fields) & set(t_fields)):
            sf, tf = s_fields[name], t_fields[name]
            fctx = f"{context} 字段 {name}"
            if sf.required != tf.required:
                src_req = "必需" if sf.required else "可选"
                tgt_req = "必需" if tf.required else "可选"
                self._reason(
                    f"{fctx}：必需性变化，来源为{src_req}，新版为{tgt_req}"
                )
                ok = False
                continue
            s_child = _Node(f"{s.path}.{name}", sf.type)
            t_child = _Node(f"{t.path}.{name}", tf.type)
            if not self._type_at(s_child, t_child, f"{context} 字段 {name}"):
                ok = False
        return ok

    def _variant(self, s: _Node, t: _Node, context: str) -> bool:
        s_tags = {g.label: g for g in s.expr.tags or []}
        t_tags = {g.label: g for g in t.expr.tags or []}
        ok = True
        missing = sorted(set(s_tags) - set(t_tags))
        extra = sorted(set(t_tags) - set(s_tags))
        if missing:
            self._reason(f"{context}：来源变体标签 {missing} 在新版中缺失")
            ok = False
        if extra:
            self._reason(f"{context}：新版出现来源没有的变体标签 {extra}")
            ok = False
        for label in sorted(set(s_tags) & set(t_tags)):
            s_child = _Node(f"{s.path}[{label}]", s_tags[label].type)
            t_child = _Node(f"{t.path}[{label}]", t_tags[label].type)
            if not self._type_at(s_child, t_child, f"{context} 标签 {label}"):
                ok = False
        return ok

    def _type_at(self, s: _Node, t: _Node, context: str) -> bool:
        """比较任意位置的类型：具名引用对入双射，其余逐结构比较。"""
        if s.expr.kind == "ref" or t.expr.kind == "ref":
            if s.expr.kind != "ref" or t.expr.kind != "ref":
                return self._reason(
                    f"{context}：引用结构不一致，来源为 {s.expr.kind}，"
                    f"新版为 {t.expr.kind}"
                )
            s_name = s.expr.ref_name or ""
            t_name = t.expr.ref_name or ""
            if not self._bind(s_name, t_name, context):
                return False
            return self._check_named(
                _Node(s_name, self.src[s_name]),
                _Node(t_name, self.tgt[t_name]),
            )
        return self._struct(s, t, context)


# ============================================================ 不可达定义

def _shape_signature(t: TypeExpr) -> tuple:
    """忽略具名引用目标的结构形状（候选粗筛，不区分引用指向）。"""
    if t.kind in PRIMITIVES:
        return ("P", t.kind)
    if t.kind == "record":
        return (
            "R",
            tuple(
                sorted(
                    (f.name, f.required, _shape_signature(f.type))
                    for f in t.fields or []
                )
            ),
        )
    if t.kind == "variant":
        return (
            "V",
            tuple(
                sorted(
                    (g.label, _shape_signature(g.type)) for g in t.tags or []
                )
            ),
        )
    return ("REF",)


def _match_unreachable(
    source: list[NamedType],
    target: list[NamedType],
    src_unreachable: set[str],
    tgt_unreachable: set[str],
    reachable_forward: dict[str, str],
    side: str,
) -> tuple[dict[str, str] | None, list[str]]:
    """为根不可达的孤立声明寻找保持引用结构的同构双射。

    双模拟等价 + 同组数量并不足以推出同构（如 2-环与两个自环互模拟却
    不存在保边双射），因此这里直接做带回溯的同构搜索：每尝试一对
    (来源名, 新版名) 就沿字段/标签与具名引用闭包，闭包内任何一对多、
    多对一或结构偏差都使该分支失败。候选按类型名排序，先成功的完整
    配对即为稳定结果；引用到可达定义时以可达双射为固定种子。
    """
    if len(src_unreachable) != len(tgt_unreachable):
        return None, [
            f"{side}：不可达具名定义数量不一致，来源 {len(src_unreachable)} 个，"
            f"新版 {len(tgt_unreachable)} 个，无法建立完整双射"
        ]
    if not src_unreachable:
        return {}, []

    src_by = {d.name: d.type for d in source}
    tgt_by = {d.name: d.type for d in target}
    seed_reverse = {v: k for k, v in reachable_forward.items()}
    src_shapes = {n: _shape_signature(src_by[n]) for n in src_unreachable}
    tgt_shapes = {n: _shape_signature(tgt_by[n]) for n in tgt_unreachable}
    src_names = sorted(src_unreachable)

    def closure(
        s0: str, t0: str, forward: dict[str, str], reverse: dict[str, str]
    ) -> tuple[dict[str, str], dict[str, str]] | None:
        """在既有映射上尝试 (s0->t0) 并闭包其全部引用约束。"""
        fw: dict[str, str] = {}
        rv: dict[str, str] = {}

        def bind(s_name: str, t_name: str) -> bool:
            known_t = forward.get(s_name, fw.get(s_name))
            known_s = reverse.get(t_name, rv.get(t_name))
            if known_t is not None and known_t != t_name:
                return False
            if known_s is not None and known_s != s_name:
                return False
            if fw.get(s_name) != t_name:
                fw[s_name] = t_name
                rv[t_name] = s_name
                stack.append((s_name, t_name))
            return True

        def check_ref(s_name: str, t_name: str) -> bool:
            if s_name in reachable_forward:
                # 来源可达引用：新版必须指向种子中的对应可达定义。
                return t_name == reachable_forward[s_name]
            if t_name in seed_reverse:
                return False  # 来源不可达 -> 新版却指向可达定义
            if s_name not in src_by or t_name not in tgt_by:
                return False
            return bind(s_name, t_name)

        def compare(st: TypeExpr, tt: TypeExpr) -> bool:
            if st.kind == "ref" or tt.kind == "ref":
                if st.kind != "ref" or tt.kind != "ref":
                    return False
                return check_ref(st.ref_name or "", tt.ref_name or "")
            if st.kind in PRIMITIVES or tt.kind in PRIMITIVES:
                return st.kind == tt.kind
            if st.kind != tt.kind:
                return False
            if st.kind == "record":
                s_fields = {f.name: f for f in st.fields or []}
                t_fields = {f.name: f for f in tt.fields or []}
                if set(s_fields) != set(t_fields):
                    return False
                for name in s_fields:
                    sf, tf = s_fields[name], t_fields[name]
                    if sf.required != tf.required:
                        return False
                    if not compare(sf.type, tf.type):
                        return False
                return True
            s_tags = {g.label: g for g in st.tags or []}
            t_tags = {g.label: g for g in tt.tags or []}
            if set(s_tags) != set(t_tags):
                return False
            for label in s_tags:
                if not compare(s_tags[label].type, t_tags[label].type):
                    return False
            return True

        stack: list[tuple[str, str]] = [(s0, t0)]
        if not bind(s0, t0):
            return None
        while stack:
            s_name, t_name = stack.pop()
            if not compare(src_by[s_name], tgt_by[t_name]):
                return None
        return fw, rv

    def search(
        forward: dict[str, str], reverse: dict[str, str]
    ) -> dict[str, str] | None:
        if len(forward) == len(src_names):
            return forward
        # MRV：选择可行候选最少的未绑定来源类型，候选数为 0 立即剪枝。
        best_name: str | None = None
        best_options: list[tuple[str, dict[str, str], dict[str, str]]] = []
        for s_name in src_names:
            if s_name in forward:
                continue
            options = []
            for t_name in sorted(tgt_unreachable):
                if t_name in reverse:
                    continue
                if src_shapes[s_name] != tgt_shapes[t_name]:
                    continue
                result = closure(s_name, t_name, forward, reverse)
                if result is not None:
                    options.append((t_name, result[0], result[1]))
            if not options:
                return None
            if best_name is None or len(options) < len(best_options):
                best_name = s_name
                best_options = options
        for t_name, add_fw, add_rv in best_options:  # 已按新版名排序
            merged_fw = {**forward, **add_fw}
            merged_rv = {**reverse, **add_rv}
            found = search(merged_fw, merged_rv)
            if found is not None:
                return found
        return None

    mapping = search({}, {})
    if mapping is None:
        return None, [
            f"{side}：不可达具名类型声明之间不存在保持引用结构的完整双射"
            f"（来源 {sorted(src_unreachable)}，新版 {sorted(tgt_unreachable)}）"
        ]
    return dict(sorted(mapping.items())), []


# ============================================================ 入口

def establish_bijection(
    source: list[NamedType],
    target: list[NamedType],
    source_root: str,
    target_root: str,
    side: str,
) -> tuple[dict[str, str] | None, list[str]]:
    """在一侧声明之间建立来源 -> 新版的具名类型完整双射。

    前置条件（由调用方检查）：声明数量一致、根可达定义数量一致。
    返回 (双射, 拒绝原因)；双射为 None 表示无法建立。
    """
    eq = _ReachableEquivalence(
        source, target, source_root, target_root, side
    )
    reachable_ok = eq.establish()
    if not reachable_ok:
        return None, eq.reasons

    src_reach = reachable_names(source, source_root)
    tgt_reach = reachable_names(target, target_root)
    src_unreach = {d.name for d in source} - src_reach
    tgt_unreach = {d.name for d in target} - tgt_reach

    unreach_map, unreach_reasons = _match_unreachable(
        source,
        target,
        src_unreach,
        tgt_unreach,
        eq.forward,
        side,
    )
    if unreach_reasons:
        return None, unreach_reasons

    # 兜底：每个来源具名类型都应有唯一对应。
    full = dict(eq.forward)
    full.update(unreach_map or {})
    missing = sorted({d.name for d in source} - set(full))
    if missing:
        return None, [f"{side}：以下来源具名类型在新版中无唯一对应：{missing}"]
    return dict(sorted(full.items())), []
