"""CF-42: a settled decision is scoped to the ticket (and profile) that settled it.

Measured in tvDownloadOHLC 2026-09-26: ticket T7 (a Rust exit-policy state
machine, profile rust-spine) was reviewed and arbitrated under five "already
settled decisions" about C# alias mapping and PerTickerMatrix, persisted by an
unrelated copier ticket CM2 months earlier. load_settled read the whole store,
so every ticket in the repo inherited every other ticket's settlements -- and a
finding that "restates a settled decision" is an arbiter REJECT criterion, so a
foreign settlement that happens to match a real finding silences it.
"""
from agent_loop.memory import inject_settled, load_settled, save_settled


def test_another_tickets_settlement_is_not_injected(tmp_path):
    save_settled(tmp_path, "CM2", ["Alias mapping is out of scope."], profile="nt8")
    assert inject_settled((), tmp_path, "T7", "rust-spine") == []


def test_the_same_ticket_still_gets_its_own_settlement(tmp_path):
    # Negative control: scoping must not switch the memory off.
    save_settled(tmp_path, "T7", ["Stop is sized to pos."], profile="rust-spine")
    assert inject_settled((), tmp_path, "T7", "rust-spine") == ["Stop is sized to pos."]


def test_a_colliding_ticket_id_under_another_profile_is_not_injected(tmp_path):
    # Ticket ids are per tickets FILE and collide across files (T7 existed
    # before this T7); the profile separates them.
    save_settled(tmp_path, "T7", ["Old T7 decision."], profile="nt8-riskguard")
    assert inject_settled((), tmp_path, "T7", "rust-spine") == []


def test_a_legacy_entry_without_a_profile_matches_on_the_ticket_only(tmp_path):
    save_settled(tmp_path, "T7", ["Legacy decision."])
    assert inject_settled((), tmp_path, "T7", "rust-spine") == ["Legacy decision."]
    assert inject_settled((), tmp_path, "T8", "rust-spine") == []


def test_profile_settled_is_always_kept(tmp_path):
    save_settled(tmp_path, "CM2", ["Foreign."], profile="nt8")
    assert inject_settled(("Hand-curated.",), tmp_path, "T7", "rust-spine") == ["Hand-curated."]


def test_load_without_a_ticket_still_reads_the_whole_store_for_audit(tmp_path):
    save_settled(tmp_path, "A", ["one"], profile="p")
    save_settled(tmp_path, "B", ["two"], profile="q")
    assert sorted(load_settled(tmp_path)) == ["one", "two"]
    assert load_settled(tmp_path, ticket_id="A", profile="p") == ["one"]
