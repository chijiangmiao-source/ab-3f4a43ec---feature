"""迁移签证结构等价与名称双射测试。"""
from __future__ import annotations

from app.parser import _Builder

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


def build(raw, side="x"):
    return _Builder([]).build_decl_list(raw, side)


def bijection(src, tgt, sroot, troot):
    from app.equivalence import establish_bijection

    return establish_bijection(build(src), build(tgt), sroot, troot, "测试侧")


def test_rename_and_reorder_declarations_fields_and_tags():
    src = [
        {"name": "Cmd", "type": rec(
            field("id", INT), field("p", ref("Payload")),
            field("note", TEXT, required=False))},
        {"name": "Payload", "type": variant(
            tag("Nil", rec()),
            tag("Cons", rec(field("head", INT),
                            field("tail", ref("Payload"), required=False))))},
    ]
    tgt = [
        {"name": "Pl", "type": variant(
            tag("Cons", rec(field("tail", ref("Pl"), required=False),
                            field("head", INT))),
            tag("Nil", rec()))},
        {"name": "Command", "type": rec(
            field("note", TEXT, required=False),
            field("p", ref("Pl")), field("id", INT))},
    ]
    mapping, reasons = bijection(src, tgt, "Cmd", "Command")
    assert mapping == {"Cmd": "Command", "Payload": "Pl"}, reasons


def test_field_rename_is_rejected():
    src = [{"name": "R", "type": rec(field("a", INT))}]
    tgt = [{"name": "R2", "type": rec(field("b", INT))}]
    mapping, reasons = bijection(src, tgt, "R", "R2")
    assert mapping is None
    assert any("字段" in r for r in reasons)


def test_required_change_is_rejected():
    src = [{"name": "R", "type": rec(field("a", INT, True))}]
    tgt = [{"name": "R2", "type": rec(field("a", INT, False))}]
    mapping, reasons = bijection(src, tgt, "R", "R2")
    assert mapping is None
    assert any("必需性" in r for r in reasons)


def test_variant_label_change_is_rejected():
    src = [{"name": "R", "type": variant(tag("A", INT))}]
    tgt = [{"name": "R2", "type": variant(tag("B", INT))}]
    mapping, reasons = bijection(src, tgt, "R", "R2")
    assert mapping is None
    assert any("标签" in r for r in reasons)


def test_primitive_change_is_rejected():
    src = [{"name": "R", "type": rec(field("a", INT))}]
    tgt = [{"name": "R2", "type": rec(field("a", BOOL))}]
    mapping, reasons = bijection(src, tgt, "R", "R2")
    assert mapping is None
    assert any("基础类型" in r for r in reasons)


def test_record_vs_variant_kind_change_is_rejected():
    src = [{"name": "R", "type": rec(field("a", INT))}]
    tgt = [{"name": "R2", "type": variant(tag("a", INT))}]
    mapping, reasons = bijection(src, tgt, "R", "R2")
    assert mapping is None
    assert any("结构种类" in r for r in reasons)


def test_many_to_one_reference_mapping_is_rejected():
    # 来源两个不同具名类型在新版中被同一个类型承担：无法双射。
    src = [
        {"name": "R", "type": rec(field("a", ref("A")), field("b", ref("B")))},
        {"name": "A", "type": INT},
        {"name": "B", "type": BOOL},
    ]
    tgt = [
        {"name": "Root", "type": rec(field("a", ref("X")), field("b", ref("X")))},
        {"name": "X", "type": INT},
    ]
    mapping, reasons = bijection(src, tgt, "R", "Root")
    assert mapping is None
    assert any("双射" in r for r in reasons)


def test_recursive_rename_resolves_via_bijection():
    src = [{"name": "List", "type": variant(
        tag("Nil", rec()),
        tag("Cons", rec(field("head", INT), field("tail", ref("List"), False))))}]
    tgt = [{"name": "Seq", "type": variant(
        tag("Cons", rec(field("head", INT), field("tail", ref("Seq"), False))),
        tag("Nil", rec()))}]
    mapping, reasons = bijection(src, tgt, "List", "Seq")
    assert mapping == {"List": "Seq"}, reasons


def test_deep_primitive_change_through_recursion_is_rejected():
    src = [{"name": "List", "type": variant(
        tag("Nil", rec()),
        tag("Cons", rec(field("head", INT), field("tail", ref("List"), False))))}]
    tgt = [{"name": "Seq", "type": variant(
        tag("Nil", rec()),
        tag("Cons", rec(field("head", TEXT), field("tail", ref("Seq"), False))))}]
    mapping, reasons = bijection(src, tgt, "List", "Seq")
    assert mapping is None
    assert any("基础类型" in r for r in reasons)


def test_mutual_recursion_renamed():
    src = [
        {"name": "A", "type": rec(field("b", ref("B")))},
        {"name": "B", "type": rec(field("a", ref("A"), required=False))},
    ]
    tgt = [
        {"name": "Beta", "type": rec(field("a", ref("Alpha"), required=False))},
        {"name": "Alpha", "type": rec(field("b", ref("Beta")))},
    ]
    mapping, reasons = bijection(src, tgt, "A", "Alpha")
    assert mapping == {"A": "Alpha", "B": "Beta"}, reasons


def test_alias_chain_rename():
    src = [
        {"name": "A", "type": ref("B")},
        {"name": "B", "type": rec(field("x", ref("A"), required=False))},
    ]
    tgt = [
        {"name": "Y", "type": rec(field("x", ref("Z"), required=False))},
        {"name": "Z", "type": ref("Y")},
    ]
    mapping, reasons = bijection(src, tgt, "A", "Z")
    assert mapping == {"A": "Z", "B": "Y"}, reasons


def test_alias_resolution_shape_mismatch_is_rejected():
    src = [
        {"name": "A", "type": ref("B")},
        {"name": "B", "type": rec(field("x", INT))},
    ]
    tgt = [
        {"name": "Z", "type": ref("Y")},
        {"name": "Y", "type": INT},
    ]
    mapping, reasons = bijection(src, tgt, "A", "Z")
    assert mapping is None
    assert any("基础类型" in r or "结构种类" in r for r in reasons)


def test_unreachable_declarations_matched_completely():
    src = [
        {"name": "R", "type": INT},
        {"name": "Dead1", "type": rec(field("v", BOOL))},
        {"name": "Dead2", "type": variant(tag("Q", TEXT))},
    ]
    tgt = [
        {"name": "Root", "type": INT},
        {"name": "X2", "type": variant(tag("Q", TEXT))},
        {"name": "X1", "type": rec(field("v", BOOL))},
    ]
    mapping, reasons = bijection(src, tgt, "R", "Root")
    assert mapping == {"R": "Root", "Dead1": "X1", "Dead2": "X2"}, reasons


def test_unreachable_partition_count_mismatch_is_rejected():
    # 总数一致，但不可达的结构分组两侧数量不同 -> 无法完整双射。
    src = [
        {"name": "R", "type": INT},
        {"name": "Dead1", "type": INT},
        {"name": "Dead2", "type": BOOL},
    ]
    tgt = [
        {"name": "Root", "type": INT},
        {"name": "X1", "type": INT},
        {"name": "X2", "type": INT},
    ]
    mapping, reasons = bijection(src, tgt, "R", "Root")
    assert mapping is None
    assert any("不可达" in r for r in reasons)


def test_unreachable_referencing_reachable_uses_seed():
    src = [
        {"name": "R", "type": rec(field("x", INT))},
        {"name": "Dead", "type": rec(field("r", ref("R")))},
    ]
    tgt = [
        {"name": "D", "type": rec(field("r", ref("Root")))},
        {"name": "Root", "type": rec(field("x", INT))},
    ]
    mapping, reasons = bijection(src, tgt, "R", "Root")
    assert mapping == {"R": "Root", "Dead": "D"}, reasons


def test_unreachable_two_cycle_vs_two_self_loops_rejected():
    # 两个声明互模拟却不同构：A->B->A 的 2-环 vs X->X、Y->Y 两个自环。
    # 双模拟分区无法区分，但保边双射不存在，必须拒绝。
    src = [
        {"name": "R", "type": INT},
        {"name": "A", "type": rec(field("n", ref("B")))},
        {"name": "B", "type": rec(field("n", ref("A")))},
    ]
    tgt = [
        {"name": "Root", "type": INT},
        {"name": "X", "type": rec(field("n", ref("X")))},
        {"name": "Y", "type": rec(field("n", ref("Y")))},
    ]
    mapping, reasons = bijection(src, tgt, "R", "Root")
    assert mapping is None
    assert any("双射" in r for r in reasons)


def test_unreachable_two_cycle_isomorphic():
    src = [
        {"name": "R", "type": INT},
        {"name": "A", "type": rec(field("n", ref("B")))},
        {"name": "B", "type": rec(field("n", ref("A")))},
    ]
    tgt = [
        {"name": "Root", "type": INT},
        {"name": "Y", "type": rec(field("n", ref("X")))},
        {"name": "X", "type": rec(field("n", ref("Y")))},
    ]
    mapping, reasons = bijection(src, tgt, "R", "Root")
    assert mapping == {"R": "Root", "A": "X", "B": "Y"}, reasons


def test_bijection_is_complete_over_all_named_types():
    src = [
        {"name": "R", "type": rec(field("a", ref("A")))},
        {"name": "A", "type": variant(tag("X", ref("B")), tag("Y", rec()))},
        {"name": "B", "type": INT},
    ]
    tgt = [
        {"name": "Beta", "type": INT},
        {"name": "Alpha", "type": variant(tag("Y", rec()), tag("X", ref("Beta")))},
        {"name": "Root", "type": rec(field("a", ref("Alpha")))},
    ]
    mapping, reasons = bijection(src, tgt, "R", "Root")
    assert mapping == {"R": "Root", "A": "Alpha", "B": "Beta"}, reasons
    assert set(mapping) == {"R", "A", "B"}
    assert len(set(mapping.values())) == 3
