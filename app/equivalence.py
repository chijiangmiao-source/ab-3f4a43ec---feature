"""改名等价判定：两套具名类型声明的完全结构等价。

仅允许两种变化：
- 类型声明在列表中的先后顺序（重排）；
- 类型名的整体替换（重命名），且必须在全部具名声明间形成完整双射。

必须保持不变：记录字段名及其声明顺序、字段必需性、变体标签及其声明
顺序、基础类型，以及 ref 所指的对应关系——递归结构在重命名像下完全
一致（覆盖全部具名声明，含自根不可达的定义）。

根类型对应关系（来源根 -> 新版根）作为种子固定进双射；双射不唯一时
按（来源名、候选新名）字典序回溯取首个完整解，与声明书写顺序无关，
即“稳定选出”。
"""
from __future__ import annotations

from .models import MigrationRejectedError, NamedType, TypeExpr


def _collect_refs(t: TypeExpr, out: set[str]) -> None:
    if t.kind == "ref":
        out.add(t.ref_name or "")
    for f in t.fields or []:
        _collect_refs(f.type, out)
    for tg in t.tags or []:
        _collect_refs(tg.type, out)


def reachable_names(decls: dict[str, TypeExpr], root: str) -> set[str]:
    """自根沿 ref 可达的具名声明集合。"""
    seen: set[str] = set()
    stack = [root]
    while stack:
        name = stack.pop()
        if name in seen or name not in decls:
            continue
        seen.add(name)
        refs: set[str] = set()
        _collect_refs(decls[name], refs)
        stack.extend(refs)
    return seen


def _rename_equal(old: TypeExpr, new: TypeExpr, mapping: dict[str, str]) -> bool:
    """在完整双射 mapping 下，new 是否恰为 old 的重命名像。"""
    if old.kind != new.kind:
        return False
    if old.kind == "ref":
        return mapping.get(old.ref_name or "") == (new.ref_name or "")
    if old.kind == "record":
        of, nf = old.fields or [], new.fields or []
        if len(of) != len(nf):
            return False
        return all(
            a.name == b.name
            and a.required == b.required
            and _rename_equal(a.type, b.type, mapping)
            for a, b in zip(of, nf)
        )
    if old.kind == "variant":
        og, ng = old.tags or [], new.tags or []
        if len(og) != len(ng):
            return False
        return all(
            a.label == b.label and _rename_equal(a.type, b.type, mapping)
            for a, b in zip(og, ng)
        )
    return True  # int / bool / text


def _consistent_under_partial(
    old: TypeExpr, new: TypeExpr, mapping: dict[str, str], used: set[str]
) -> bool:
    """部分双射下的必要条件：存在某个完备化使 new 成为 old 的重命名像。"""
    if old.kind != new.kind:
        return False
    if old.kind == "ref":
        old_target = old.ref_name or ""
        new_target = new.ref_name or ""
        if old_target in mapping:
            return mapping[old_target] == new_target
        # old_target 尚未映射：new_target 必须仍空闲，才可能补上这一映射。
        return new_target not in used
    if old.kind == "record":
        of, nf = old.fields or [], new.fields or []
        if len(of) != len(nf):
            return False
        return all(
            a.name == b.name
            and a.required == b.required
            and _consistent_under_partial(a.type, b.type, mapping, used)
            for a, b in zip(of, nf)
        )
    if old.kind == "variant":
        og, ng = old.tags or [], new.tags or []
        if len(og) != len(ng):
            return False
        return all(
            a.label == b.label
            and _consistent_under_partial(a.type, b.type, mapping, used)
            for a, b in zip(og, ng)
        )
    return True


def find_name_bijection(
    old: dict[str, TypeExpr],
    new: dict[str, TypeExpr],
    old_root: str,
    new_root: str,
) -> dict[str, str] | None:
    """求 old -> new 的完整名称双射；不存在返回 None。

    根类型对应关系作为种子固定；其余按（来源名、候选新名）字典序回溯，
    首个完整解即稳定选出的双射，与声明书写顺序无关。
    """
    if len(old) != len(new):
        return None
    if old_root not in old or new_root not in new:
        return None

    mapping: dict[str, str] = {old_root: new_root}
    used: set[str] = {new_root}
    if not _consistent_under_partial(old[old_root], new[new_root], mapping, used):
        return None

    remaining = sorted(n for n in old if n not in mapping)
    candidates = sorted(new)

    def search(i: int) -> bool:
        if i == len(remaining):
            return all(
                _rename_equal(old[name], new[mapping[name]], mapping)
                for name in mapping
            )
        old_name = remaining[i]
        for new_name in candidates:
            if new_name in used:
                continue
            if not _consistent_under_partial(
                old[old_name], new[new_name], mapping, used
            ):
                continue
            mapping[old_name] = new_name
            used.add(new_name)
            if search(i + 1):
                return True
            del mapping[old_name]
            used.discard(new_name)
        return False

    if not search(0):
        return None
    return {name: mapping[name] for name in sorted(mapping)}


def check_side_equivalence(
    side: str,
    old: list[NamedType],
    new: list[NamedType],
    old_root: str,
    new_root: str,
) -> dict[str, str]:
    """裁决一侧（发送端或接收端）的改名等价，返回稳定选出的完整名称双射。"""
    old_map = {d.name: d.type for d in old}
    new_map = {d.name: d.type for d in new}

    if len(old_map) != len(new_map):
        raise MigrationRejectedError(
            "declaration-count-mismatch",
            f"{side}声明数量不一致：来源 {len(old_map)} 个，新版 {len(new_map)} 个",
            {
                "side": side,
                "source_count": len(old_map),
                "new_count": len(new_map),
            },
        )

    old_unreachable = len(old_map) - len(reachable_names(old_map, old_root))
    new_unreachable = len(new_map) - len(reachable_names(new_map, new_root))
    if old_unreachable != new_unreachable:
        raise MigrationRejectedError(
            "unreachable-definitions-mismatch",
            f"{side}不可达定义数量不一致：来源 {old_unreachable} 个，"
            f"新版 {new_unreachable} 个",
            {
                "side": side,
                "source_unreachable": old_unreachable,
                "new_unreachable": new_unreachable,
            },
        )

    mapping = find_name_bijection(old_map, new_map, old_root, new_root)
    if mapping is None:
        raise MigrationRejectedError(
            "name-bijection-not-found",
            f"{side}无法建立保持结构的完整名称双射：字段名、必需性、变体标签、"
            "基础类型或递归引用对应关系不一致",
            {"side": side},
        )
    return mapping


def check_migration_equivalence(
    source_sender: list[NamedType],
    source_receiver: list[NamedType],
    source_root: str,
    new_sender: list[NamedType],
    new_receiver: list[NamedType],
    new_root: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """分别裁决新版发送端、接收端与来源对应声明的改名等价。"""
    sender_mapping = check_side_equivalence(
        "发送端", source_sender, new_sender, source_root, new_root
    )
    receiver_mapping = check_side_equivalence(
        "接收端", source_receiver, new_receiver, source_root, new_root
    )
    return sender_mapping, receiver_mapping
