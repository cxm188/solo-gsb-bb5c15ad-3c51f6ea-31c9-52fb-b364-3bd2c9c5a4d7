"""内容协商逻辑与 HTTP 接口测试。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.negotiation import (
    AcceptSyntaxError,
    audit,
    negotiate,
    parse_accept,
)


client = TestClient(app)


def post(accept, candidates):
    return client.post(
        "/negotiate", json={"accept": accept, "candidates": candidates}
    )


def post_audit(accept, candidates):
    return client.post(
        "/audit-accept", json={"accept": accept, "candidates": candidates}
    )


# ---------- parse_accept ----------


def test_parse_default_q_and_case_insensitive():
    ranges = parse_accept("Application/JSON, text/*;q=0.5, */*; q=0.1")
    assert [(r.raw, r.q, r.specificity) for r in ranges] == [
        ("application/json", 1.0, 2),
        ("text/*", 0.5, 1),
        ("*/*", 0.1, 0),
    ]


@pytest.mark.parametrize(
    "header",
    [
        "",
        "   ",
        "application",
        "application/",
        "/json",
        "*/json",
        "application/j*son",
        "application/json;q",
        "application/json;q=",
        "application/json;q=1.5",
        "application/json;q=-0.1",
        "application/json;q=0.1234",
        "application/json;q=1.001",
        "application/json;level=1",
        "application/json;q=0.5;charset=utf-8",
        "application/json,",
        ",application/json",
        "application/json,,*/*",
        "app lication/json",
        "application/json; q=abc",
    ],
)
def test_parse_invalid(header):
    with pytest.raises(AcceptSyntaxError):
        parse_accept(header)


def test_parse_q_boundaries_valid():
    ranges = parse_accept("a/b;q=0, c/d;q=1, e/f;q=0.999, g/h;q=1.000")
    assert [r.q for r in ranges] == [0.0, 1.0, 0.999, 1.0]


# ---------- negotiate 核心规则 ----------


def test_most_specific_range_wins():
    # application/json 同时命中精确和 */*，取精确范围的 q。
    idx, results, _ = negotiate(
        "*/*;q=0.1, application/json;q=0.5",
        [("application/json", 0)],
    )
    assert idx == 0
    r = results[0]
    assert r.matched_range == "application/json"
    assert r.q == 0.5
    assert r.specificity == 2
    assert r.eliminated_reason is None


def test_equal_specificity_earliest_in_header_wins():
    # 两个同具体度的精确范围，取头中最早项。
    idx, results, _ = negotiate(
        "application/json;q=0.3, application/json;q=0.9",
        [("application/json", 0)],
    )
    assert results[0].q == 0.3
    assert idx == 0


def test_q_zero_excludes_even_if_wildcard_accepts():
    # 精确 q=0 优先于 */* 的 q=1（最具体范围决定）。
    idx, results, _ = negotiate(
        "*/*;q=1, text/html;q=0",
        [("text/html", 0), ("application/json", 0)],
    )
    html, json_ = results
    assert html.eligible is False
    assert html.eliminated_reason == "q_zero"
    assert json_.eligible is True
    assert idx == 1


def test_ordering_q_then_specificity_then_priority_then_input():
    idx, results, _ = negotiate(
        "*/*;q=0.5, application/*;q=0.8, application/json;q=0.8",
        [
            ("application/xml", 10),   # q0.8, spec1,  p10
            ("application/json", 1),  # q0.8, spec2,  p1  -> 胜
            ("text/plain", 100),      # q0.5
        ],
    )
    assert idx == 1
    assert [r.eliminated_reason for r in results] == ["outranked", None, "outranked"]


def test_q_dominates_specificity_and_priority():
    idx, results, _ = negotiate(
        "application/json;q=0.1, */*;q=0.9",
        [("application/json", 100), ("text/plain", 0)],
    )
    # text/plain q=0.9（via */*）胜过精确匹配但 q=0.1 的 json
    assert idx == 1
    assert results[0].matched_range == "application/json"
    assert results[1].matched_range == "*/*"


def test_tie_input_order():
    idx, _, _ = negotiate(
        "*/*",
        [("a/b", 0), ("c/d", 0)],
    )
    assert idx == 0


def test_no_match():
    idx, results, _ = negotiate("text/html", [("application/json", 0)])
    assert idx is None
    assert results[0].eliminated_reason == "no_match"
    assert results[0].matched_range is None


def test_all_q_zero():
    idx, results, _ = negotiate("*/*;q=0", [("a/b", 0), ("c/d", 0)])
    assert idx is None
    assert all(r.eliminated_reason == "q_zero" for r in results)


def test_concrete_candidate_required():
    with pytest.raises(AcceptSyntaxError):
        negotiate("*/*", [("application/*", 0)])
    with pytest.raises(AcceptSyntaxError):
        negotiate("*/*", [("application/json; charset=utf-8", 0)])


# ---------- HTTP 接口 ----------


def test_endpoint_success():
    resp = post(
        "application/json;q=0.9, text/*;q=0.5, */*;q=0.1",
        [
            {"media_type": "text/html", "server_priority": 10},
            {"media_type": "application/json", "server_priority": 1},
            {"media_type": "image/png", "server_priority": 5},
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["selected"]["media_type"] == "application/json"
    assert body["selected"]["matched_range"] == "application/json"
    assert body["selected"]["q"] == 0.9
    reasons = {c["media_type"]: c["eliminated_reason"] for c in body["candidates"]}
    assert reasons["text/html"] == "outranked"
    assert reasons["image/png"] == "outranked"
    assert len(body["parsed_accept"]) == 3


def test_endpoint_400_bad_accept():
    resp = post("application/json;q=2.0", [{"media_type": "a/b"}])
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


def test_endpoint_400_wildcard_candidate():
    resp = post("*/*", [{"media_type": "text/*"}])
    assert resp.status_code == 400


def test_endpoint_400_empty_candidates():
    resp = post("*/*", [])
    assert resp.status_code == 400


def test_endpoint_400_malformed_body():
    resp = client.post("/negotiate", json={"accept": "*/*"})
    assert resp.status_code == 400


def test_endpoint_406():
    resp = post("*/*;q=0", [{"media_type": "a/b", "server_priority": 0}])
    assert resp.status_code == 406
    body = resp.json()
    assert body["error"] == "not_acceptable"
    assert body["candidates"][0]["eliminated_reason"] == "q_zero"


def test_endpoint_case_insensitive():
    resp = post(
        "APPLICATION/JSON",
        [{"media_type": "Application/Json", "server_priority": 0}],
    )
    assert resp.status_code == 200
    assert resp.json()["selected"]["media_type"] == "application/json"


def test_health():
    assert client.get("/health").status_code == 200


# ---------- audit 核心规则 ----------


def test_audit_lists_hits_effective_and_shadowed():
    audits, ranges, shadowed = audit(
        "application/json, application/*;q=0.5, */*;q=0.1",
        [("application/json", 1), ("text/html", 2)],
    )

    json_audit, html_audit = audits
    assert [r.index for r in json_audit.hits] == [0, 1, 2]
    assert [r.raw for r in json_audit.hits] == [
        "application/json",
        "application/*",
        "*/*",
    ]
    assert json_audit.effective.index == 0
    assert json_audit.effective.q == 1.0
    assert json_audit.eligible is True
    assert json_audit.eliminated_reason is None
    assert json_audit.conflicts == []

    assert [r.index for r in html_audit.hits] == [2]
    assert html_audit.effective.index == 2
    assert html_audit.eligible is True

    # application/* 从未生效（json 命中更精确范围，html 不命中该类型）。
    assert [r.index for r in shadowed] == [1]
    assert shadowed[0].raw == "application/*"


def test_audit_shadowed_wildcard_when_all_concrete():
    audits, _, shadowed = audit(
        "application/json;q=0.5, text/html;q=0.8, */*;q=0.1",
        [("application/json", 0), ("text/html", 0)],
    )
    assert [a.effective.raw for a in audits] == ["application/json", "text/html"]
    assert [r.raw for r in shadowed] == ["*/*"]


def test_audit_conflict_same_specificity_different_q():
    audits, _, _ = audit(
        "application/json;q=0.3, application/json;q=0.9",
        [("application/json", 0)],
    )
    conflicts = audits[0].conflicts
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.specificity == 2
    assert conflict.specificity_name == "exact"
    # 冲突范围按头中索引升序，并携带头部索引。
    assert [(r.index, r.q) for r in conflict.ranges] == [(0, 0.3), (1, 0.9)]
    # 生效范围仍是同具体度中最早出现者。
    assert audits[0].effective.index == 0
    assert audits[0].effective.q == 0.3


def test_audit_no_conflict_when_same_specificity_same_q():
    audits, _, _ = audit(
        "application/json;q=0.5, application/json;q=0.5",
        [("application/json", 0)],
    )
    assert audits[0].conflicts == []


def test_audit_conflict_on_wildcard_specificity():
    audits, _, _ = audit(
        "*/*;q=0.2, */*;q=0.7",
        [("text/html", 0)],
    )
    conflicts = audits[0].conflicts
    assert len(conflicts) == 1
    assert conflicts[0].specificity == 0
    assert [(r.index, r.q) for r in conflicts[0].ranges] == [(0, 0.2), (1, 0.7)]


def test_audit_conflicts_grouped_per_specificity():
    # 精确组与通配组各有一组冲突；application/* 单独一组不构成冲突。
    audits, _, _ = audit(
        "application/json;q=0.3, application/json;q=0.9, "
        "application/*;q=0.5, */*;q=0.1, */*;q=0.2",
        [("application/json", 0)],
    )
    conflicts = audits[0].conflicts
    assert [c.specificity for c in conflicts] == [2, 0]
    assert [(r.index, r.q) for r in conflicts[0].ranges] == [(0, 0.3), (1, 0.9)]
    assert [(r.index, r.q) for r in conflicts[1].ranges] == [(3, 0.1), (4, 0.2)]


def test_audit_q_zero_is_effective_and_not_shadowed():
    audits, _, shadowed = audit(
        "*/*;q=1, text/html;q=0",
        [("text/html", 0), ("application/json", 0)],
    )
    html, json_ = audits
    assert html.eligible is False
    assert html.eliminated_reason == "q_zero"
    assert html.effective.index == 1
    assert html.effective.q == 0.0
    # q=0 的 text/html 对 html 生效，不是 shadowed；*/* 对 json 生效。
    assert shadowed == []
    assert json_.eligible is True
    assert json_.effective.raw == "*/*"


def test_audit_no_match_ranges_are_shadowed():
    audits, ranges, shadowed = audit(
        "text/html, image/png",
        [("application/json", 0)],
    )
    assert audits[0].hits == []
    assert audits[0].effective is None
    assert audits[0].eligible is False
    assert audits[0].eliminated_reason == "no_match"
    assert [r.index for r in shadowed] == [0, 1]


def test_audit_stable_ordering():
    accept = "*/*;q=0.1, application/*;q=0.5, application/json;q=0.9"
    audits, _, _ = audit(
        accept,
        [("text/html", 0), ("application/json", 0), ("application/xml", 0)],
    )
    # 候选按输入顺序。
    assert [a.index for a in audits] == [0, 1, 2]
    # hits 按具体度降序、同具体度按头中索引升序。
    assert [r.index for r in audits[1].hits] == [2, 1, 0]
    assert [r.index for r in audits[2].hits] == [1, 0]
    assert [r.index for r in audits[0].hits] == [0]


def test_audit_invalid_input_raises():
    with pytest.raises(AcceptSyntaxError):
        audit("application/json;q=2.0", [("a/b", 0)])
    with pytest.raises(AcceptSyntaxError):
        audit("*/*", [("text/*", 0)])


# ---------- POST /audit-accept 接口 ----------


def test_audit_endpoint_success():
    resp = post_audit(
        "application/json;q=0.3, application/json;q=0.9, "
        "application/*;q=0.5, */*;q=0.1",
        [
            {"media_type": "application/json", "server_priority": 1},
            {"media_type": "text/html", "server_priority": 5},
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    # 审计不返回单一选定项。
    assert "selected" not in body

    json_cand, html_cand = body["candidates"]
    assert [h["index"] for h in json_cand["hits"]] == [0, 1, 2, 3]
    assert json_cand["effective"]["index"] == 0
    assert json_cand["eligible"] is True
    assert json_cand["eliminated_reason"] is None
    assert len(json_cand["conflicts"]) == 1
    conflict = json_cand["conflicts"][0]
    assert conflict["specificity"] == 2
    assert [(r["index"], r["q"]) for r in conflict["ranges"]] == [
        (0, 0.3),
        (1, 0.9),
    ]

    assert [h["index"] for h in html_cand["hits"]] == [3]
    assert html_cand["effective"]["index"] == 3

    # application/* 从未生效；重复的 application/json（index 1）也从未生效。
    assert [r["index"] for r in body["shadowed"]] == [1, 2]
    assert [r["index"] for r in body["parsed_accept"]] == [0, 1, 2, 3]


def test_audit_endpoint_effective_range_shape():
    resp = post_audit(
        "*/*;q=0.1",
        [{"media_type": "a/b", "server_priority": 2}],
    )
    body = resp.json()
    cand = body["candidates"][0]
    assert cand["hits"] == [
        {
            "index": 0,
            "range": "*/*",
            "q": 0.1,
            "specificity": 0,
            "specificity_name": "wildcard",
        }
    ]
    assert cand["effective"] == cand["hits"][0]
    assert cand["conflicts"] == []
    assert body["shadowed"] == []


def test_audit_endpoint_no_match_effective_null():
    resp = post_audit(
        "text/html",
        [{"media_type": "application/json", "server_priority": 0}],
    )
    assert resp.status_code == 200
    cand = resp.json()["candidates"][0]
    assert cand["hits"] == []
    assert cand["effective"] is None
    assert cand["eligible"] is False
    assert cand["eliminated_reason"] == "no_match"
    assert [r["range"] for r in resp.json()["shadowed"]] == ["text/html"]


def test_audit_endpoint_406_q_zero():
    resp = post_audit(
        "*/*;q=0",
        [{"media_type": "a/b", "server_priority": 0}],
    )
    assert resp.status_code == 406
    body = resp.json()
    assert body["error"] == "not_acceptable"
    assert body["candidates"][0]["eliminated_reason"] == "q_zero"
    assert body["candidates"][0]["effective"]["q"] == 0.0
    assert body["shadowed"] == []


def test_audit_endpoint_406_all_no_match():
    resp = post_audit(
        "text/html",
        [{"media_type": "application/json", "server_priority": 0}],
    )
    assert resp.status_code == 406
    assert resp.json()["error"] == "not_acceptable"


def test_audit_endpoint_200_when_any_eligible():
    # 一个候选 q=0、另一个可用：仍是 200（审计不做选择）。
    resp = post_audit(
        "*/*;q=1, text/html;q=0",
        [
            {"media_type": "text/html", "server_priority": 0},
            {"media_type": "application/json", "server_priority": 0},
        ],
    )
    assert resp.status_code == 200
    reasons = [c["eliminated_reason"] for c in resp.json()["candidates"]]
    assert reasons == ["q_zero", None]


def test_audit_endpoint_400_bad_accept():
    resp = post_audit("application/json;q=2.0", [{"media_type": "a/b"}])
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


def test_audit_endpoint_400_wildcard_candidate():
    resp = post_audit("*/*", [{"media_type": "text/*"}])
    assert resp.status_code == 400


def test_audit_endpoint_400_empty_candidates():
    resp = post_audit("*/*", [])
    assert resp.status_code == 400


def test_audit_endpoint_400_malformed_body():
    resp = client.post("/audit-accept", json={"accept": "*/*"})
    assert resp.status_code == 400
