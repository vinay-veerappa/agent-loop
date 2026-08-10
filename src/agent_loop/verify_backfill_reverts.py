"""Prove the Phase B backfill tests are falsifiable.

A test that has never been seen to fail is an assertion of faith. For each of
the six T1-T3 acceptance tests, revert *only* the production line(s) that test
exists to pin, re-run the suite, and require that the named test goes red.

The source file is restored from an in-memory copy after every case, including
on exception, so a crash cannot leave the tree patched.

Usage:  python -m scripts.agent_loop.verify_backfill_reverts
"""

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ADDONS = REPO / "scripts" / "ninjatrader" / "addons"
ADDON = ADDONS / "RiskGuardAddOn.cs"
COPIER = ADDONS / "TradeCopierEngine.cs"
TEST_CMD = [
    "dotnet", "run",
    "--project", str(REPO / "ninjatrader-addon" / "RiskGuardTests.csproj"),
    "--nologo", "-v", "q",
]

# (case name, test header substring, old, new)
CASES = [
    (
        "T2 auto-stop sized from live position",
        "T2: auto-stop is sized from the live position",
        "                int stopQuantity = (int)positionForQuantity.Quantity;",
        "                int stopQuantity = action.Quantity;",
    ),
    (
        "T2 ValidateInvariant admits a scaled-down position",
        "T2: a scaled-down position is still admitted",
        "                int liveQuantity = (int)position.Quantity;\n"
        "                if (liveQuantity <= 0)\n"
        "                    return false;",
        "                int liveQuantity = (int)position.Quantity;\n"
        "                if (liveQuantity <= 0)\n"
        "                    return false;\n"
        "                if (action.Quantity > liveQuantity)\n"
        "                    return false;",
    ),
    (
        "T2 rollback on submit failure",
        "T2: a failed auto-stop submit rolls the FSM back",
        '                    RollbackFsm($"Submit failed: {ex.Message}");',
        '                    // reverted: RollbackFsm($"Submit failed: {ex.Message}");',
    ),
    (
        "T1 cancelled stop clears the grace latch",
        "T1: a stop cancelled mid-position re-arms grace",
        "                                fsm.CoveredQuantity = 0;\n"
        "                                fsm.GraceEmitted = false;\n"
        "                                if (!fsm.GracePending)",
        "                                fsm.CoveredQuantity = 0;\n"
        "                                if (!fsm.GracePending)",
    ),
    (
        "T3 peak resets when the account is flat",
        "T3: a flat, profitable account emits no peak-giveback",
        "                    bool needsReset = stateModel.PeakOpenGain != 0.0\n"
        "                        || stateModel.PeakGivebackTriggered\n"
        "                        || !double.IsNaN(stateModel.PeakGivebackLastTriggerUnrealized);\n"
        "                    if (needsReset)\n"
        "                    {\n"
        "                        stateModel.PeakOpenGain = 0.0;\n"
        "                        stateModel.PeakGivebackTriggered = false;\n"
        "                        stateModel.PeakGivebackLastTriggerUnrealized = double.NaN;\n"
        "                        _stateDirty = true;\n"
        "                    }",
        "                    // reverted: flat no longer resets the peak",
    ),
    (
        "T3 flip resets the peak",
        "T3: a close+reverse flip does not carry",
        "                if (isFlip)\n"
        "                {\n"
        "                    PeakOpenGain = 0.0;\n"
        "                    PeakGivebackTriggered = false;\n"
        "                    PeakGivebackLastTriggerUnrealized = double.NaN;\n"
        "                }",
        "                // reverted: flip no longer resets the peak",
    ),
]

# Same shape, but reverted in TradeCopierEngine.cs instead. P1-21's subscribe pass is a new
# API, so there is no "before" revision of it to run the tests against; each case below
# reinstates one facet of the defect the pass exists to fix.
COPIER_CASES = [
    (
        "P1-21 subscribe pass runs more than once",
        "COPIER SUBS: a leader that connects after startup",
        "            int added = 0;\n            lock (_subscriptionLock)\n            {",
        # The defect verbatim: enumerate Account.All once and never again, so an account that
        # connects later is never subscribed.
        "            int added = 0;\n            lock (_subscriptionLock)\n            {\n"
        "                if (_subscribedAccounts.Count > 0) return 0;  // reverted: one-shot pass",
    ),
    (
        "P1-21 subscribe pass is idempotent",
        "COPIER SUBS: repeated subscribe passes attach exactly one handler",
        "                    acc.ExecutionUpdate -= OnAccountExecutionUpdate;\n"
        "                    acc.ExecutionUpdate += OnAccountExecutionUpdate;",
        "                    // reverted: no detach, so each pass adds another handler\n"
        "                    acc.ExecutionUpdate += OnAccountExecutionUpdate;",
    ),
    (
        "P1-21 teardown detaches handlers",
        "COPIER SUBS: teardown detaches every handler",
        # The ExecutionUpdate/OrderUpdate detach pair is unique to teardown; the subscribe pass
        # follows each `-=` with a `+=`.
        "                    acc.ExecutionUpdate -= OnAccountExecutionUpdate;\n"
        "                    acc.OrderUpdate -= OnAccountOrderUpdate;",
        "                    // reverted: handlers left attached across the AddOn reload",
    ),
    (
        "P1-22 the follower's fill is observed at all",
        "COPIER SLIP: a follower fill populates",
        "                ObserveFollowerFill(exec);",
        "                // reverted: follower fill dropped unmeasured, as before P1-22",
    ),
    (
        "P1-22 slippage is signed by the follower's side",
        "COPIER SLIP: a favourable fill is negative slippage",
        "            double ticks = pending.FollowerIsBuy ? rawTicks : -rawTicks;",
        "            double ticks = rawTicks;  // reverted: unsigned, so a good fill reads as slippage",
    ),
    (
        "P1-22 a quarantined relationship still copies exits",
        "COPIER SLIP: a slippage quarantine blocks entries but still copies exits",
        "                GetActiveRelationshipsForLeader(acctName, includeQuarantined: leaderIsExiting);",
        "                GetActiveRelationshipsForLeader(acctName, includeQuarantined: false);  // reverted",
    ),
    (
        "P1-22 pending copies are keyed by Order reference, not OrderId",
        "COPIER SLIP: a fill is matched by Order reference",
        "            public bool Equals(Order x, Order y) { return ReferenceEquals(x, y); }\n"
        "            public int GetHashCode(Order obj) { return System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(obj); }",
        "            public bool Equals(Order x, Order y) { return x != null && y != null && x.OrderId == y.OrderId; }  // reverted\n"
        "            public int GetHashCode(Order obj) { return obj == null || obj.OrderId == null ? 0 : obj.OrderId.GetHashCode(); }",
    ),
    (
        "S7 exit copies stay clamped to the follower's position under a concurrent burst",
        "S7 STRESS: copier fan-out under concurrent burst",
        # P0-5's clamp. Removing it is the defect S7 exists to catch at scale: every exit in the
        # burst is sized from the leader's raw quantity instead of what the follower holds.
        "                int positionSize = Math.Abs(currentFollowerPosition);\n"
        "                if (rawCopyQty > positionSize)\n"
        "                {\n"
        "                    isClamped = positionSize > 0;\n"
        "                    rawCopyQty = positionSize;\n"
        "                }",
        "                // reverted: exit sized from the leader's raw quantity (P0-5)",
    ),
    (
        "P0-9 the stop is offset from the follower's entry in the right direction",
        "BRACKET: the leader's stop distance is anchored to the follower's own fill",
        "                stopPrice = bracket.FollowerEntryPrice + bracket.StopOffset;",
        "                stopPrice = bracket.FollowerEntryPrice - bracket.StopOffset;  // reverted: inverted",
    ),
    (
        # The exact defect that shipped in 51892d54 and was found by asking what a StopLimit
        # conversion could break. Math.Abs discards the sign, so a stop trailed into profit is
        # mirrored onto the losing side of the follower's entry.
        "P0-9 the leader-to-stop offset stays signed",
        "BRACKET: a stop trailed into profit is mirrored above the follower's entry",
        "                stopPrice = bracket.FollowerEntryPrice + bracket.StopOffset;",
        "                stopPrice = bracket.FollowerSide == MarketPosition.Long\n"
        "                    ? bracket.FollowerEntryPrice - Math.Abs(bracket.StopOffset)\n"
        "                    : bracket.FollowerEntryPrice + Math.Abs(bracket.StopOffset);  // reverted: unsigned",
    ),
    (
        "P0-9 a short's trailed stop stays signed too",
        "BRACKET: a short's stop trailed into profit maps below the follower's entry",
        "                stopPrice = bracket.FollowerEntryPrice + bracket.StopOffset;",
        "                stopPrice = bracket.FollowerSide == MarketPosition.Long\n"
        "                    ? bracket.FollowerEntryPrice - Math.Abs(bracket.StopOffset)\n"
        "                    : bracket.FollowerEntryPrice + Math.Abs(bracket.StopOffset);  // reverted: unsigned",
    ),
    (
        "P0-9 a distance held before the fill is applied on the fill",
        "BRACKET: a leader stop seen before the follower fills",
        "            SyncFollowerStop(followerAcc, exec.Instrument, bracket);",
        "            // reverted: the anchor arrives and nothing re-syncs, so the held distance is lost",
    ),
    (
        "P0-9 a moved leader stop replaces rather than duplicates",
        "BRACKET: a leader moving its stop replaces",
        "                    if (stillLive) toCancel = bracket.WorkingStop;",
        "                    // reverted: old stop left working alongside the new one",
    ),
    (
        "P0-9 a flat follower has its mirrored stop cancelled",
        "BRACKET: a follower going flat has its mirrored stop cancelled",
        "                ReleaseFollowerBracket(followerAcc, instrumentName);\n"
        "                return;",
        "                return;  // reverted: orphan stop left working against a flat account",
    ),
    (
        "P0-9 no stop is mirrored across price-incomparable instruments",
        "BRACKET: no stop is mirrored across price-incomparable instruments",
        "                if (!ArePricesComparable(RootOf(order.Instrument.FullName), RootOf(targetInstrument.FullName)))",
        "                if (false)  // reverted: comparability no longer checked",
    ),
    (
        "P1-22 price-incomparable instruments are excluded",
        "COPIER SLIP: an unrelated mapped symbol records no slippage",
        "            if (!pending.PriceComparable || pending.FollowerTickSize <= 0\n"
        "                || pending.LeaderFillPrice <= 0 || exec.Price <= 0)\n"
        "                return;",
        "            if (pending.FollowerTickSize <= 0\n"
        "                || pending.LeaderFillPrice <= 0 || exec.Price <= 0)\n"
        "                return;  // reverted: comparability no longer checked",
    ),
]


def failures_in(output: str, header: str):
    """Return the [FAIL] lines belonging to the test whose header matches."""
    blocks = re.split(r"\n(?=\[TEST\] )", output)
    for block in blocks:
        if header in block.split("\n", 1)[0]:
            return [l.strip() for l in block.splitlines() if "[FAIL]" in l]
    return None  # test never ran


def run_suite():
    p = subprocess.run(TEST_CMD, cwd=REPO, capture_output=True, text=True, timeout=1800)
    return p.stdout + p.stderr


def main():
    sources = {ADDON: ADDON.read_text(encoding="utf-8"),
               COPIER: COPIER.read_text(encoding="utf-8")}
    cases = ([(ADDON, *c) for c in CASES] + [(COPIER, *c) for c in COPIER_CASES])
    results = []
    try:
        for path, name, header, old, new in cases:
            original = sources[path]
            if original.count(old) != 1:
                results.append((name, "SKIP", f"anchor matched {original.count(old)}x, expected 1"))
                continue

            path.write_text(original.replace(old, new), encoding="utf-8")
            out = run_suite()
            path.write_text(original, encoding="utf-8")

            fails = failures_in(out, header)
            if fails is None:
                results.append((name, "ERROR", "test did not run (build failure?)"))
            elif fails:
                results.append((name, "FALSIFIABLE", fails[0][:100]))
            else:
                results.append((name, "NOT FALSIFIABLE", "test still passed with the fix reverted"))
    finally:
        for path, text in sources.items():
            path.write_text(text, encoding="utf-8")

    print("\n" + "=" * 78)
    print("REVERT VERIFICATION")
    print("=" * 78)
    bad = 0
    for name, verdict, detail in results:
        mark = "ok  " if verdict == "FALSIFIABLE" else "BAD "
        if verdict != "FALSIFIABLE":
            bad += 1
        print(f"{mark} {verdict:<16} {name}\n        {detail}")
    print("=" * 78)
    print(f"{len(results) - bad}/{len(results)} tests proven to fail when their fix is reverted")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
