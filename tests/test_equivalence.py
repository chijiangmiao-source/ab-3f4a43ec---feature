"""改名等价（完整名称双射）引擎测试。"""
from __future__ import annotations

import pytest

from app.equivalence import (
    check_migration_equivalence,
    find_name_bijection,
    reachable_names,
)
from app.models import MigrationRejectedError
from app.models import TypeExpr as T
from app.models import FieldDecl as F
from app.models import TagDecl as G
from app.models import NamedType as N

INT, BOOL, TEXT = T("int"), T("bool"), T("text")


def rec(*fields: F) -> T:
    return T(kind="record", fields=list(fields))


def var(*tags: G) -> T:
    return T(kind="variant", tags=list(tags))


def ref(name: str) -> T:
    return T(kind="ref", ref_name=name)


def list_decl(name: str, head: T = INT) -> T:
    return var(
        G("Nil", rec()),
        G("Cons", rec(F("head", head), F("tail", ref(name), required=False))),
    )


def test_renamed_recursive_type_bijection():
    old = {"List": list_decl("List")}
    new = {"Chain": list_decl("Chain")}
    assert find_name_bijection(old, new, "List", "Chain") == {"List": "Chain"}


def test_declaration_order_is_irrelevant():
    old = {
        "A": rec(F("b", ref("B"))),
        "B": rec(F("a", ref("A"), required=False)),
    }
    # 新版：改名 + 书写顺序不影响结果（find 接收 dict，此处验证映射内容）。
    new = {"Y": rec(F("a", ref("X"), required=False)), "X": rec(F("b", ref("Y")))}
    assert find_name_bijection(old, new, "A", "X") == {"A": "X", "B": "Y"}


def test_mutual_recursion_renamed():
    old = {
        "A": rec(F("b", ref("B"))),
        "B": rec(F("a", ref("A"), required=False)),
    }
    new = {
        "Alpha": rec(F("b", ref("Beta"))),
        "Beta": rec(F("a", ref("Alpha"), required=False)),
    }
    assert find_name_bijection(old, new, "A", "Alpha") == {
        "A": "Alpha",
        "B": "Beta",
    }


def test_field_name_change_breaks_bijection():
    old = {"R": rec(F("x", INT))}
    new = {"R2": rec(F("y", INT))}
    assert find_name_bijection(old, new, "R", "R2") is None


def test_requiredness_change_breaks_bijection():
    old = {"R": rec(F("x", INT, required=True))}
    new = {"R2": rec(F("x", INT, required=False))}
    assert find_name_bijection(old, new, "R", "R2") is None


def test_variant_label_change_breaks_bijection():
    old = {"R": var(G("A", INT))}
    new = {"R2": var(G("B", INT))}
    assert find_name_bijection(old, new, "R", "R2") is None


def test_primitive_change_breaks_bijection():
    old = {"R": rec(F("x", INT))}
    new = {"R2": rec(F("x", TEXT))}
    assert find_name_bijection(old, new, "R", "R2") is None


def test_field_order_is_preserved():
    # 仅允许类型声明重排；记录字段顺序属于结构，必须保持。
    old = {"R": rec(F("a", INT), F("b", TEXT))}
    new = {"R2": rec(F("b", TEXT), F("a", INT))}
    assert find_name_bijection(old, new, "R", "R2") is None


def test_count_mismatch_returns_none():
    old = {"R": rec(F("x", INT)), "U": rec(F("y", TEXT))}
    new = {"R2": rec(F("x", INT))}
    assert find_name_bijection(old, new, "R", "R2") is None


def test_root_correspondence_is_enforced():
    # 结构上存在双射 R->U2, U->R2，但根必须对应：R->R2 不成立则整体失败。
    old = {"R": INT, "U": TEXT}
    new = {"R2": TEXT, "U2": INT}
    assert find_name_bijection(old, new, "R", "R2") is None
    assert find_name_bijection(old, new, "R", "U2") == {"R": "U2", "U": "R2"}


def test_stable_selection_among_multiple_bijections():
    # 两个不可达的同构类型：双射不唯一，稳定选出字典序最小者，
    # 且与新旧声明的书写顺序无关。
    old = {"R": rec(F("x", INT)), "A": TEXT, "B": TEXT}
    new1 = {"Q": rec(F("x", INT)), "X": TEXT, "Y": TEXT}
    new2 = {"Y": TEXT, "Q": rec(F("x", INT)), "X": TEXT}
    expected = {"A": "X", "B": "Y", "R": "Q"}
    assert find_name_bijection(old, new1, "R", "Q") == expected
    assert find_name_bijection(old, new2, "R", "Q") == expected


def test_unreachable_definitions_covered_by_bijection():
    # 不可达定义也参与等价：来源的不可达定义被改名后仍须对应。
    old = {"R": rec(F("x", INT)), "U": rec(F("y", TEXT))}
    new_ok = {"R2": rec(F("x", INT)), "U2": rec(F("y", TEXT))}
    new_bad = {"R2": rec(F("x", INT)), "U2": rec(F("y", BOOL))}
    assert find_name_bijection(old, new_ok, "R", "R2") == {"R": "R2", "U": "U2"}
    assert find_name_bijection(old, new_bad, "R", "R2") is None


def test_reachable_names_closure():
    decls = {
        "A": rec(F("b", ref("B"))),
        "B": rec(F("c", ref("C"), required=False)),
        "C": INT,
        "D": TEXT,  # 不可达
    }
    assert reachable_names(decls, "A") == {"A", "B", "C"}
    assert reachable_names(decls, "D") == {"D"}


# ---------- check_migration_equivalence 的拒绝原因 ----------


def _migration(source_sender, source_receiver, new_sender, new_receiver,
               source_root="R", new_root="R2"):
    return check_migration_equivalence(
        source_sender, source_receiver, source_root,
        new_sender, new_receiver, new_root,
    )


def test_equivalence_issued_with_both_mappings():
    s_old = [N("R", rec(F("p", ref("P")))), N("P", list_decl("P"))]
    s_new = [N("Q", list_decl("Q")), N("R2", rec(F("p", ref("Q"))))]
    r_old = [N("R", rec(F("p", ref("P")))), N("P", list_decl("P"))]
    r_new = [N("R2", rec(F("p", ref("Q")))), N("Q", list_decl("Q"))]
    sender_map, receiver_map = _migration(s_old, r_old, s_new, r_new)
    assert sender_map == {"P": "Q", "R": "R2"}
    assert receiver_map == {"P": "Q", "R": "R2"}


def test_declaration_count_mismatch_rejected():
    s_old = [N("R", rec(F("x", INT)))]
    s_new = [N("R2", rec(F("x", INT))), N("Extra", INT)]
    with pytest.raises(MigrationRejectedError) as ei:
        _migration(s_old, s_old, s_new, s_new)
    assert ei.value.reason == "declaration-count-mismatch"
    assert ei.value.detail["source_count"] == 1
    assert ei.value.detail["new_count"] == 2


def test_unreachable_definitions_mismatch_rejected():
    # 声明数量相同，但自根不可达的定义数量不同。
    s_old = [N("R", rec(F("x", INT))), N("U", rec(F("y", TEXT)))]
    s_new = [
        N("R2", rec(F("x", INT), F("u", ref("U2"), required=False))),
        N("U2", rec(F("y", TEXT))),
    ]
    with pytest.raises(MigrationRejectedError) as ei:
        _migration(s_old, s_old, s_new, s_new)
    assert ei.value.reason == "unreachable-definitions-mismatch"
    assert ei.value.detail["source_unreachable"] == 1
    assert ei.value.detail["new_unreachable"] == 0


def test_no_bijection_rejected_with_reason():
    s_old = [N("R", rec(F("x", INT)))]
    s_new = [N("R2", rec(F("renamed", INT)))]
    with pytest.raises(MigrationRejectedError) as ei:
        _migration(s_old, s_old, s_new, s_new)
    assert ei.value.reason == "name-bijection-not-found"


def test_receiver_side_failure_reports_receiver():
    ok = [N("R", rec(F("x", INT)))]
    renamed_ok = [N("R2", rec(F("x", INT)))]
    renamed_bad = [N("R2", rec(F("x", BOOL)))]
    with pytest.raises(MigrationRejectedError) as ei:
        _migration(ok, ok, renamed_ok, renamed_bad)
    assert ei.value.reason == "name-bijection-not-found"
    assert ei.value.detail["side"] == "接收端"
