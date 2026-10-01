import json

CONTRACT = "contracts/outage_cover.py"
FEED = "https://status.kraken.com/api/v2/incidents.json"
SCOPE = "BTC spot order placement or matching on Kraken"

T0 = "2026-10-01T00:00:00+00:00"        # deploy / buy time
START = 1790899200                     # 2026-10-02T00:00:00Z
END = START + 86400                    # 24h window
AFTER = "2026-10-03T01:00:00+00:00"    # resolve time
GEN = 10**18


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def incident(id_, name, start, end, body, components=()):
    return {
        "id": id_, "name": name, "impact": "major",
        "created_at": iso(start), "started_at": iso(start),
        "resolved_at": iso(end) if end else None,
        "components": [{"name": c} for c in components],
        "incident_updates": [{"body": body}],
    }


HALT = incident("halt01", "Spot trading halted", START + 3600, START + 3600 + 90 * 60,
                "Order placement and matching are unavailable for all spot markets.", ["Spot Trading"])
WMTX = incident("wmtx01", "WMTX funding paused", START + 100, START + 7200,
                "Withdrawals and deposits are paused for WMTX. All other funding is normal.", ["WMTX - Base"])
OLD = incident("old001", "Old incident", START - 10 * 86400, START - 9 * 86400, "resolved long ago")


def feed(*incs):
    return {"status": 200, "body": json.dumps({"incidents": list(incs)})}


def llm(decisions):
    return json.dumps({"decisions": decisions})


def setup_pool(vm, deploy, alice, bob, capital=10 * GEN):
    vm.warp(T0)
    c = deploy(CONTRACT)
    vm.sender = alice
    vm.value = capital
    pid = c.open_pool("Kraken", FEED, SCOPE, START, END, 60, 5)
    vm.sender = bob
    vm.value = 1 * GEN
    assert c.buy_cover(pid) == 5 * GEN
    vm.value = 0
    return c, pid


def test_open_pool_validation(direct_vm, direct_deploy, direct_alice):
    direct_vm.warp(T0)
    c = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    direct_vm.value = GEN
    with direct_vm.expect_revert("Statuspage"):
        c.open_pool("Kraken", "https://evil.example/feed", SCOPE, START, END, 60, 5)
    with direct_vm.expect_revert("window"):
        c.open_pool("Kraken", FEED, SCOPE, START - 10**6, END, 60, 5)
    with direct_vm.expect_revert("trigger"):
        c.open_pool("Kraken", FEED, SCOPE, START, START + 600, 60, 5)
    with direct_vm.expect_revert("multiple"):
        c.open_pool("Kraken", FEED, SCOPE, START, END, 60, 1)
    direct_vm.value = 0
    with direct_vm.expect_revert("capital"):
        c.open_pool("Kraken", FEED, SCOPE, START, END, 60, 5)


def test_capacity_and_buy_window(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c, pid = setup_pool(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.sender = direct_charlie
    direct_vm.value = 2 * GEN          # would need 10 GEN of payout, only 5 left
    with direct_vm.expect_revert("not enough capital"):
        c.buy_cover(pid)
    direct_vm.value = 1 * GEN
    assert c.buy_cover(pid) == 5 * GEN
    assert c.get_pool(pid)["capacity_left"] == 0
    direct_vm.warp(iso(START + 1))
    with direct_vm.expect_revert("before the window"):
        c.buy_cover(pid)


def test_resolve_triggered_then_claim(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, pid = setup_pool(direct_vm, direct_deploy, direct_alice, direct_bob)
    with direct_vm.expect_revert("not ended"):
        c.resolve(pid)

    direct_vm.warp(AFTER)
    direct_vm.mock_web(r"status\.kraken\.com", feed(HALT, WMTX, OLD))
    direct_vm.mock_llm(r"Covered scope", llm([
        {"id": "halt01", "impairs_scope": True, "reason": "spot matching down"},
        {"id": "wmtx01", "impairs_scope": False, "reason": "unrelated asset funding"},
    ]))
    assert c.resolve(pid) == "triggered"
    p = c.get_pool(pid)
    assert p["impaired_minutes"] == 90
    assert [e["id"] for e in p["evidence"]] == ["halt01"]

    # same evidence -> a validator agrees
    assert direct_vm.run_validator() is True

    direct_vm.sender = direct_bob
    assert c.claim(pid) == 5 * GEN
    with direct_vm.expect_revert("nothing to claim"):
        c.claim(pid)

    direct_vm.sender = direct_alice
    assert c.withdraw_capital(pid) == 10 * GEN + 1 * GEN - 5 * GEN


def test_resolve_not_triggered_underwriter_keeps_premium(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, pid = setup_pool(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.warp(AFTER)
    direct_vm.mock_web(r"status\.kraken\.com", feed(WMTX, OLD))
    direct_vm.mock_llm(r"Covered scope", llm([{"id": "wmtx01", "impairs_scope": False, "reason": "other asset"}]))
    assert c.resolve(pid) == "not_triggered"
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("did not trigger"):
        c.claim(pid)
    direct_vm.sender = direct_alice
    assert c.withdraw_capital(pid) == 11 * GEN
    with direct_vm.expect_revert("already withdrawn"):
        c.withdraw_capital(pid)


def test_validator_rejects_different_classification(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, pid = setup_pool(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.warp(AFTER)
    direct_vm.mock_web(r"status\.kraken\.com", feed(HALT, WMTX))
    direct_vm.mock_llm(r"Covered scope", llm([
        {"id": "halt01", "impairs_scope": True, "reason": "x"},
        {"id": "wmtx01", "impairs_scope": False, "reason": "y"},
    ]))
    c.resolve(pid)

    # a dishonest leader that drops the qualifying incident is rejected
    assert direct_vm.run_validator(leader_result={"incidents": [], "minutes": 0}) is False

    # a validator whose model disagrees on the classification rejects too
    direct_vm.clear_mocks()
    direct_vm.mock_web(r"status\.kraken\.com", feed(HALT, WMTX))
    direct_vm.mock_llm(r"Covered scope", llm([
        {"id": "halt01", "impairs_scope": True, "reason": "x"},
        {"id": "wmtx01", "impairs_scope": True, "reason": "y"},
    ]))
    assert direct_vm.run_validator() is False


def test_minutes_merge_overlaps_and_open_incident(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, pid = setup_pool(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.warp(AFTER)
    a = incident("a", "Degraded matching", START + 0, START + 1800, "matching degraded")
    b = incident("b", "Matching outage", START + 1200, START + 2400, "matching down")
    still_open = incident("c", "API outage", END - 600, None, "orders failing")
    direct_vm.mock_web(r"status\.kraken\.com", feed(a, b, still_open))
    direct_vm.mock_llm(r"Covered scope", llm([
        {"id": i, "impairs_scope": True, "reason": "r"} for i in ("a", "b", "c")
    ]))
    c.resolve(pid)
    # a+b merge to 40 min, c is clipped at window end to 10 min
    assert c.get_pool(pid)["impaired_minutes"] == 50


def test_feed_that_no_longer_covers_window(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, pid = setup_pool(direct_vm, direct_deploy, direct_alice, direct_bob)
    direct_vm.warp(AFTER)
    newer = [incident(f"n{i:02}", "x", END + 3600 + i, END + 7200, "x") for i in range(50)]
    direct_vm.mock_web(r"status\.kraken\.com", feed(*newer))
    with direct_vm.expect_revert("no longer covers"):
        c.resolve(pid)
