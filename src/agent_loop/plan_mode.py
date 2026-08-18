"""
plan_mode.py
============
Plan mode: input is a defect description, output is a ticket JSON with
regions and acceptance test names. The LLM uses the graph (passive
injection, phase 3) to localize the defect and propose regions.

The output is reviewed by the panel + arbiter for completeness before
being promoted to a ticket file.

Phase 6 of the execution plan.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import arbiter as arbiter_mod
from . import config
from . import gates, profiles, regions, workspace
from .context import build_intent_context, build_layout_context
from .memory import inject_settled
from .providers import Completion, ProviderError, chat


PLAN_SYSTEM = """You are a senior software engineer planning a fix for a defect.
Your job is to analyze the defect, localize it in the codebase using the
context provided, and produce a ticket JSON that describes the fix.

OUTPUT FORMAT - obey exactly:
<<<TICKET>>>
{{
  "id": "TICKET_ID",
  "title": "short title",
  "defect": "what is wrong",
  "spec": "what the fix should do",
  "regions": [
    {{"id": "REGION_ID", "file": "path/to/file.py", "anchor": "unique anchor string in the file"}}
  ],
  "expect_green": ["test_name_that_should_pass_after_fix"]
}}
<<<END TICKET>>>
<<<NOTES>>>
- why these regions, why these tests
<<<END NOTES>>>
"""


FEATURE_SYSTEM = """You are a senior software engineer planning a NEW FEATURE.

The code does not exist yet. Your job is to break the feature into the SMALLEST
parts that can each be built and verified on their own, in order, and to emit one
ticket per part.

Every part goes through the same test-first cycle as a defect fix: its
`expect_green` names the tests that must FAIL before the part is built and PASS
after. A part with no acceptance tests cannot be gated and will be rejected.

Each region carries an `op`:
  "op": "create"   the file does not exist yet; the whole file will be written.
                   Give NO anchor.
  "op": "insert"   add new code after the anchor; the anchored code stays.
  "op": "replace"  rewrite the anchored block (the default if op is omitted).

A later part MAY touch a file an earlier part creates -- say so with
`depends_on`. Two parts must not create the same file.

OUTPUT FORMAT - obey exactly, one block per part, in build order:
<<<TICKET>>>
{{
  "id": "F1",
  "title": "short title",
  "defect": "what is missing today",
  "spec": "what this part must do",
  "depends_on": [],
  "regions": [
    {{"id": "R1", "file": "path/to/new_file.py", "op": "create"}}
  ],
  "expect_green": ["tests/path/test_x.py::test_the_new_behaviour"]
}}
<<<END TICKET>>>
<<<TICKET>>>
{{ ...the next part... }}
<<<END TICKET>>>
<<<FEATURE_ACCEPTANCE>>>
["test_feature_works_end_to_end", "test_feature_edge_case"]
<<<END FEATURE_ACCEPTANCE>>>
<<<NOTES>>>
- why this decomposition, and why this order
<<<END NOTES>>>

The FEATURE_ACCEPTANCE block is optional but recommended: it names
integration-level tests that verify the WHOLE feature works after all
parts are done. These run against the scratch branch HEAD (which has
all parts' code). If the feature is small enough that per-part
expect_green tests suffice, omit the block.
"""


def run_plan(
    repo: Path,
    defect_description: str,
    profile: profiles.Profile,
    implementer: str,
    reviewers: Sequence[str],
    arbiter_model: str = "",
    max_rounds: int = 4,
    fast_plan: bool = False,
    feature: bool = False,
) -> Dict[str, Any]:
    """Run plan mode: defect -> ticket JSON (reviewed by panel+arbiter).

    Args:
        repo: the repo root
        defect_description: the defect to analyze
        profile: the language profile
        implementer: the model to use for planning
        reviewers: the panel models
        arbiter_model: the arbiter model (empty = skip arbitration)
        max_rounds: max revision rounds
        fast_plan: if True, use a single reviewer instead of the full panel

    Returns:
        a result dict with the final ticket JSON and verdict
    """
    tid = "PLAN"
    art = repo / "logs" / "agent_loop" / tid
    art.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ticket": tid, "rounds": [], "plan": None, "verdict": ""}

    # Phase 5: inject auto-extracted settled decisions
    effective_settled = inject_settled(profile.settled, repo)

    heading = "Feature to plan" if feature else "Defect to analyze"
    prompt = f"# {heading}\n\n{defect_description}\n\n"

    # The codebase this defect lives in. Plan mode is asked to LOCALISE and emit
    # regions whose anchors must resolve against the tree, and until O31 it did
    # that having never been shown the tree: this module imported
    # `build_context_slice` and never called it, because that function takes the
    # regions plan mode exists to produce. `in=319` tokens on a live run.
    code_context = build_intent_context(repo, profile, defect_description)
    if code_context:
        prompt += code_context + "\n"

    # A feature names nothing that exists yet, so the symbol search above finds
    # nothing by construction -- and the consequence is not a thinner prompt but a
    # wrong one. On the first live run, asked for a `--json` flag, plan mode
    # returned four well-formed parts that created every file under a `patchgate/`
    # package which does not exist, because nothing had told it the code lives in
    # `src/agent_loop/`. The layout is the context a feature actually needs.
    if feature:
        layout = build_layout_context(repo, profile)
        if layout:
            prompt += layout + "\n"

    prompt += "## Context\n"
    prompt += f"Language: {profile.language}\n"
    prompt += f"File suffixes: {', '.join(profile.file_suffixes)}\n"
    prompt += f"Build: {profile.build_cmd or '(none)'}\n"
    prompt += f"Test: {profile.test_cmd or '(none)'}\n"
    # Where tests are ALLOWED to live. Without this the model invents a path:
    # on the O7 run it emitted `expect_green: tests/test_review_mode.py::...`
    # for a repo whose tests are all in tests/acceptance/, which is the only
    # place the test-first machinery may write. A convention the model is never
    # shown is a convention it cannot honour.
    if profile.test_sources:
        # A1: tell the planner the EXACT test file path for each part, so
        # the runner's test generation (default_test_path) and the planner's
        # expect_green entries agree. Without this the planner invents a path
        # (e.g. tests/acceptance/test_trailing_stop.py) while the runner
        # generates tests/acceptance/test_F1Generated.py — names_match
        # does a whole-identifier regex and won't find the planner's nodeid
        # in the runner's failure output → TICKET_REJECTED → zero parts run.
        from .test_mode import default_test_path
        # We don't know part IDs yet (the planner generates them), so show
        # the PATTERN: test_<PART_ID>Generated.<ext> under the first glob.
        first_glob = profile.test_sources[0]
        if "*" in first_glob:
            pattern = first_glob.replace("*", "<PART_ID>Generated", 1)
        else:
            pattern = first_glob
        prompt += (
            f"Tests live in: {', '.join(profile.test_sources)}\n"
            "Every path in `expect_green` MUST match one of those patterns.\n"
            f"The runner generates tests at: {pattern}\n"
            f"Use that exact path (with the part's ID substituted) in `expect_green`.\n"
            f"For part F1, the test file is: {default_test_path(profile, 'F1')}\n"
        )

    history = [
        {"role": "system", "content": FEATURE_SYSTEM if feature else PLAN_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    final = "MAX_ROUNDS_EXHAUSTED"
    # The last plan that PARSED, whether or not it validated. A run that
    # exhausts its rounds used to write nothing but `result.json`, discarding a
    # plan that was often one bad anchor away from usable -- and the first round
    # of a feature plan can cost several minutes, so the only recovery was to
    # re-run the whole thing or hand-parse `rN_plan_raw.txt`.
    last_parsed: Any = None

    for rnd in range(1, max_rounds + 1):
        try:
            # Phase 4: compact history before the implementer call
            if rnd > 1:
                from .compaction import compact_history, history_token_count
                before = history_token_count(history)
                history = compact_history(history, rnd, profile)
                after = history_token_count(history)
                if after < before:
                    print(f"           [compaction] {before} -> {after} tokens")
            _c = config.get().mode("plan")
            out = chat(implementer, history, max_tokens=_c.max_tokens, think=_c.think)
        except ProviderError as exc:
            result["rounds"].append({"round": rnd, "error": str(exc)})
            final = "IMPLEMENTER_UNREACHABLE"
            break

        raw = out.text
        (art / f"r{rnd}_plan_raw.txt").write_text(raw, encoding="utf-8")
        print(f"  round {rnd}: plan {out.usage_line()}")

        # Parse the ticket JSON from the response. A feature is decomposed, so
        # every <<<TICKET>>> block counts and document order is build order.
        tickets = _parse_tickets(raw)
        ticket = tickets[0] if tickets else None
        if not ticket:
            history += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "Your output did not contain a parseable <<<TICKET>>> block. Re-emit with the correct format."},
            ]
            continue

        # D1: parse feature_acceptance (optional, feature plans only).
        feature_acceptance = _parse_feature_acceptance(raw) if feature else []

        last_parsed = tickets if feature else ticket

        # Verify regions resolve. For a feature the plan is ordered and later
        # parts legitimately touch files earlier parts create, so validation walks
        # the parts in sequence and carries those files forward -- see
        # _validate_feature_plan.
        if feature:
            resolved_notes: List[str] = []
            problem = _validate_feature_plan(repo, tickets, profile, resolved_notes)
            if problem:
                print(f"           plan rejected: {problem}")
                history += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": f"{problem} Fix the plan and re-emit."},
                ]
                result["error"] = problem
                continue
            regs = []
            print(f"           plan: {len(tickets)} part(s), regions check OK")
        else:
            try:
                regs = regions.extract(repo, ticket.get("regions", []), profile)
                print(f"           regions: {len(regs)} resolved OK")
            except regions.RegionError as exc:
                history += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": f"Region extraction failed: {exc}. Fix the anchors and re-emit."},
                ]
                continue

        # If fast_plan: skip panel+arbiter, just accept
        if fast_plan:
            result["plan"] = tickets if feature else ticket
            result.pop("error", None)
            result["verdict"] = "APPROVE"
            final = "APPROVE"
            print("           [fast-plan] accepted without panel review")
            break

        # Panel review of the plan.
        #
        # A FEATURE plan must be reviewed WHOLE. `ticket` is only the first part,
        # and reviewing part 1 of 4 cannot see the thing most worth reviewing in a
        # decomposed plan: whether the parts compose -- ordering, a later part
        # anchoring into a file an earlier one creates, duplicated or contradictory
        # work across parts. Observed live: a 4-part plan drew 13 findings, all
        # about part 1, and parts 2-4 were never seen by any reviewer.
        from .loop import review_panel, PanelResult
        if feature:
            plan_block = (
                f"## Proposed plan -- {len(tickets)} ORDERED parts, all of them\n"
                f"```json\n{json.dumps(tickets, indent=2)}\n```\n\n"
                "Each part is gated separately and lands in order.\n\n"
                "## Resolved regions, validated in order\n"
                + ("\n".join(f"- {n}" for n in resolved_notes) or "- (none)")
                + "\n\nAn anchor marked `deferred` targets a file an earlier part "
                "creates, so it cannot be resolved until that part lands -- the "
                "loop resolves it at the moment it runs that part.\n\n"
                "Review the plan AS A WHOLE: do the parts compose, is the order "
                "right, does any part duplicate or contradict another, and does "
                "each part's acceptance test actually gate that part?\n"
            )
        else:
            plan_block = (
                f"## Proposed ticket\n```json\n{json.dumps(ticket, indent=2)}\n```\n\n"
                f"## Resolved regions\n"
                + "\n".join(f"- {r.id}: {r.file} lines {r.lines_1based}" for r in regs)
                + "\n\nReview this plan: are the regions correct? Are the acceptance "
                "tests adequate?\n"
            )
        review_prompt = (
            f"# Plan review for defect: {defect_description[:200]}\n\n" + plan_block
        )
        panel = review_panel(
            reviewers, review_prompt, profile.reviewer_system, art, rnd,
            deadline_secs=config.get().loop.panel_deadline_secs,
        )
        desc = ", ".join(f"{v.model.split(':')[0]}={v.status}({v.blockers})" for v in panel.votes)
        print(f"           [panel] {panel.verdict or 'INVALID'}  [{desc}]")

        if panel.unanimous_approve:
            result["plan"] = tickets if feature else ticket
            result.pop("error", None)
            result["verdict"] = "APPROVE"
            final = "APPROVE"
            break

        if not panel.valid:
            final = "PANEL_UNREACHABLE"
            break

        # Arbiter
        if arbiter_model:
            from .loop import Finding
            all_findings = [f for v in panel.votes if v.counted for f in v.finding_list]
            adj = arbiter_mod.adjudicate(
                arbiter_model, ticket, all_findings,
                "plan review", "", settled=effective_settled,
                rules=profile.arbiter_rules,
            )
            (art / f"r{rnd}_arbiter.txt").write_text(adj.raw or adj.error, encoding="utf-8")
            if adj.ok:
                print(f"           [arbiter] {adj.summary()}")
                if adj.recommendation == arbiter_mod.SHIP:
                    # `tickets if feature else ticket`, matching the panel-approve
                    # and fast-plan paths. This branch shipped the BARE FIRST
                    # PART: a live 4-part feature plan validated all four, then
                    # wrote a plan.json containing one, with nothing in the output
                    # saying three had been dropped. Every arbiter-shipped feature
                    # plan since --feature existed was truncated this way.
                    result["plan"] = tickets if feature else ticket
                    # And drop any rejection from an earlier round. It used to
                    # survive into result.json beside a success verdict, so the
                    # file reported ARBITER_SHIP and an anchor error together.
                    result.pop("error", None)
                    result["verdict"] = "ARBITER_SHIP"
                    final = "ARBITER_SHIP"
                    break
                if adj.recommendation == arbiter_mod.ESCALATE:
                    final = "ESCALATED"
                    break

            feedback = panel.findings
            history += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Plan review returned {panel.verdict}.\n\nFINDINGS:\n{feedback}\n\nFix the plan and re-emit."},
            ]
        else:
            feedback = panel.findings
            history += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Plan review returned {panel.verdict}.\n\nFINDINGS:\n{feedback}\n\nFix the plan and re-emit."},
            ]

    result["verdict"] = final
    (art / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if result["plan"]:
        # The WRAPPER shape, not the bare ticket. Plan mode's output exists to be
        # fed to `--mode test` and `--tickets`, and a bare object made both raise
        # KeyError: 'tickets' (O33). The loader accepts either shape now, but
        # writing the canonical one keeps the documented pipeline honest and
        # makes the file paste-able into a hand-written ticket set.
        plan = result["plan"]
        plan_wrapper = {"tickets": plan if isinstance(plan, list) else [plan]}
        # D1: include feature_acceptance if the planner emitted it.
        if feature and feature_acceptance:
            plan_wrapper["feature_acceptance"] = feature_acceptance
        (art / "plan.json").write_text(
            json.dumps(plan_wrapper, indent=2),
            encoding="utf-8",
        )
        print(f"  PLAN -> {art / 'plan.json'}")
    elif last_parsed is not None:
        # Deliberately the SAME wrapper shape as plan.json, and deliberately
        # under a different NAME: it is directly usable via --tickets once the
        # rejection is fixed by hand, but nothing downstream can mistake a
        # rejected plan for an approved one.
        (art / "plan_rejected.json").write_text(
            json.dumps(
                {"tickets": last_parsed if isinstance(last_parsed, list) else [last_parsed]},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  REJECTED PLAN -> {art / 'plan_rejected.json'}  ({result.get('error', final)})")
    return result


_TICKET_RE = re.compile(r"<<<TICKET>>>\s*(\{.*?\})\s*<<<END\s*TICKET>>>", re.DOTALL)
_FEATURE_ACCEPTANCE_RE = re.compile(
    r"<<<FEATURE_ACCEPTANCE>>>\s*(.*?)\s*<<<END\s*FEATURE_ACCEPTANCE>>>", re.DOTALL
)


def _parse_ticket(raw: str) -> Optional[Dict[str, Any]]:
    """Parse the FIRST <<<TICKET>>> block from the raw response."""
    tickets = _parse_tickets(raw)
    return tickets[0] if tickets else None


def _parse_tickets(raw: str) -> List[Dict[str, Any]]:
    """Every <<<TICKET>>> block, in document order.

    Document order is build order for a feature plan: the user's requirement is
    that a feature is broken into smaller parts, and the parts are sequenced.
    An unparseable block is skipped rather than aborting the set -- one malformed
    part should not discard the four that parsed.
    """
    out: List[Dict[str, Any]] = []
    for m in _TICKET_RE.finditer(raw):
        try:
            t = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(t, dict):
            out.append(t)
    return out


def _parse_feature_acceptance(raw: str) -> List[str]:
    """D1: parse the <<<FEATURE_ACCEPTANCE>>> block.

    Returns a list of integration-level test names, or [] if the block
    is absent. These run after all parts are done, against the scratch
    branch HEAD.
    """
    m = _FEATURE_ACCEPTANCE_RE.search(raw)
    if not m:
        return []
    try:
        names = json.loads(m.group(1))
        if isinstance(names, list):
            return [str(n) for n in names]
    except json.JSONDecodeError:
        pass
    return []


def _validate_feature_plan(
    repo: Path, tickets: List[Dict[str, Any]], profile: profiles.Profile,
    resolved: Optional[List[str]] = None,
) -> str:
    """Check a decomposed feature plan. Returns "" when it is usable.

    Two rules, both from the requirement that a feature is ordered parts each
    going through the same TDD cycle:

    1. **Every part names acceptance tests.** A part with no `expect_green` cannot
       be gated by the ladder, so the plan is unusable however well it reads. This
       is the whole content of "a feature should also go through the same TDD
       cycle" -- without it, "feature mode" would be the one path into the loop
       that skips the check the loop exists to apply.

    2. **Regions resolve in ORDER, against the tree plus what earlier parts
       create.** Part 2 legitimately inserts into a file part 1 creates. Checking
       the whole plan against the tree as it is now would reject every feature
       that builds on itself, which is every real one. The relaxation is scoped:
       a path no earlier part creates must still exist, or a typo sails through.
    """
    if not tickets:
        return "The plan contained no parseable tickets."

    def _note(tid: str, spec: Dict[str, Any], where: str) -> None:
        """Record WHERE a region landed, for the panel.

        Both reviewers blocked a live feature plan on "the Resolved regions block
        in this review request is empty" -- true, because the feature branch set
        `regs = []` and the prompt rendered an empty section. A reviewer asked to
        judge regions, and shown none, is being asked to rubber-stamp.
        """
        if resolved is not None:
            resolved.append(f"{tid}/{spec.get('id')}: {spec.get('file')} {where}")

    will_exist: set = set()
    seen_ids: set = set()
    for t in tickets:
        tid = t.get("id") or "(unnamed)"
        if tid in seen_ids:
            return f"Part {tid} is declared twice; each part needs a unique id."
        seen_ids.add(tid)

        if not (t.get("expect_green") or []):
            return (
                f"Part {tid} has no `expect_green`. Every part must name the tests "
                f"that fail before it is built and pass after -- a part with no "
                f"acceptance tests cannot be verified."
            )

        for spec in t.get("regions") or []:
            f = spec.get("file", "")
            op = str(spec.get("op", regions.REPLACE)).lower()
            if op not in regions.OPS:
                return f"Part {tid}: region {spec.get('id')} has unknown op {op!r}."
            exists = (repo / f).exists() if f else False
            if op == regions.CREATE:
                if exists:
                    return (
                        f"Part {tid}: region {spec.get('id')} creates {f}, which "
                        f"already exists. Use op=insert or op=replace."
                    )
                if f in will_exist:
                    return (
                        f"Part {tid}: {f} is already created by an earlier part. "
                        f"Two parts must not create the same file."
                    )
                will_exist.add(f)
                _note(tid, spec, "(new file)")
                continue
            # replace / insert: the file must be there, or be on its way.
            if not exists and f not in will_exist:
                return (
                    f"Part {tid}: region {spec.get('id')} targets {f}, which does "
                    f"not exist and is not created by an earlier part."
                )
            if not spec.get("anchor"):
                return (
                    f"Part {tid}: region {spec.get('id')} has op={op} but no anchor."
                )
            # An anchor inside a file a previous part will create cannot be
            # checked yet -- there is nothing to look in. Deferred to the loop,
            # which resolves regions per ticket at the moment it runs it.
            if exists and f not in will_exist:
                try:
                    regs = regions.extract(repo, [spec], profile)
                except regions.RegionError as exc:
                    return f"Part {tid}: {exc}"
                for r in regs:
                    _note(tid, spec, f"lines {r.lines_1based} ({op})")
                    # W6: refuse a region above a configured ceiling.
                    line_span = r.end_line - r.start_line + 1
                    max_lines = getattr(profile, "max_region_lines", 150)
                    if line_span > max_lines:
                        return (
                            f"Part {tid}: region {spec.get('id')} in {f} spans "
                            f"{line_span} lines (lines {r.lines_1based}), which "
                            f"exceeds the max of {max_lines}. Split the ticket into "
                            f"smaller regions -- a large region exposes new surface "
                            f"every round and the panel cannot converge."
                        )
            else:
                # C2: greenfield regions (file created by an earlier part)
                # skip the max_region_lines check because there's nothing to
                # look in. But we CAN check the ticket-level size: if the
                # spec is very large or has many regions, the part may not
                # converge. This is advisory, not a hard refusal.
                _note(tid, spec, f"({op}, anchor deferred: file is created by an earlier part)")

    # C2: ticket-level size check. A part with >3 regions or a spec >500
    # chars is advisory — the planner can argue the part is cohesive.
    # This catches the greenfield case that the per-region check misses.
    for t in tickets:
        tid = t.get("id", "?")
        regs = t.get("regions", [])
        spec = t.get("spec", "")
        if len(regs) > 3:
            _note(tid, regs[0] if regs else {}, f"advisory: {len(regs)} regions — consider splitting")
        if len(spec) > 500:
            _note(tid, {}, f"advisory: spec is {len(spec)} chars — consider splitting")

    return ""