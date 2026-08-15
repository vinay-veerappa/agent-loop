# Agent Loop Critical Review

## Scope

This note is a review of the current implementation and the prompt architecture, not a patch. It focuses on the loop’s discipline around TDD, Agile work breakdown, validation, context management, and prompt hygiene.

Primary code and docs reviewed:
- [README.md](../../README.md)
- [src/agent_loop/developer/driver.py](../../src/agent_loop/developer/driver.py)
- [src/agent_loop/gates.py](../../src/agent_loop/gates.py)
- [src/agent_loop/test_mode.py](../../src/agent_loop/test_mode.py)
- [src/agent_loop/context.py](../../src/agent_loop/context.py)
- [src/agent_loop/memory.py](../../src/agent_loop/memory.py)
- [src/agent_loop/config.py](../../src/agent_loop/config.py)
- [src/agent_loop/loop.py](../../src/agent_loop/loop.py)
- [docs/architecture/BACKLOG.md](./BACKLOG.md)

## Executive summary

This is not just a product review; it is an engineering review of a software system that uses model calls as a control plane. From that perspective, the loop is materially stronger than a typical “LLM in a loop” prototype. It has explicit phases, a gate ladder, review panel, arbiter, protected paths, worktree isolation, context injection, learning feedback, and a serious effort to make the pipeline more disciplined than a raw coding model.

That said, the architecture still contains a few design-level problems that matter more than individual bugs:

1. The TDD gate is enforcing the wrong proxy for independence.
2. The loop is not consistently organized around small, independently validatable task items.
3. Context and token management are improved, but still fragile and easy to overrun in real usage.
4. Prompt and output parsing still depend on brittle assumptions that can silently turn a hard failure into a false green.
5. The system is better at “run a structured review loop” than at “produce a repeatable, auditable software delivery workflow.”

## What works well

### 1. It has a real control structure
The loop is not just “one prompt that asks the model to code.” It has layered stages: gate -> review -> arbiter -> apply, with a worktree and baseline capture pattern in the developer path. That is a genuine design improvement.

### 2. It explicitly tries to protect against reward hacking
Protected paths, compile gates, static checks, and the requirement to fail a test before editing are strong anti-hack controls. This is the right instinct.

### 3. It recognizes the cost of context explosion
The project explicitly tracks context budgets, settled decisions, and learning feedback injection limits in [src/agent_loop/memory.py](../../src/agent_loop/memory.py) and [src/agent_loop/context.py](../../src/agent_loop/context.py). That is a sign of maturity rather than casual prototyping.

### 4. It tries to codify real agent failure modes
The backlog documents a large number of real historical defects in [docs/architecture/BACKLOG.md](./BACKLOG.md), including parser fragility, false-pass traps, and misconfigured budgets. This is evidence that the system is being stress-tested and improved rather than only designed in theory.

## Major issues and design concerns

## 1. The TDD gate is enforcing the wrong proxy

This is the largest conceptual issue.

The stated principle is that a test should be written by someone other than the person who implemented the fix. That is a good property, but the loop is currently enforcing a looser and wrong proxy: that a human wrote the test, not that the test was independently generated or validated.

The problem is not that the property is unimportant. The problem is that the loop currently checks the wrong thing.

### Why the current proxy is wrong

The check is effectively: “a human-owned test file was written before the change, and the code path is not the same model pass.”

But the real property the gate wants is something narrower and more important:

- The test must be independent of the implementation path.
- The test must validate the external behavior, not a model’s implementation guess.
- The test must be capable of failing before the implementation exists.

A second model, given only the spec and denied sight of the implementation, would satisfy the independence property just as well as a human. The current design treats “human wrote it” as the proxy, but the real requirement is “it was not produced by the same implementation pass.”

### Why this matters

This creates a false sense of safety:

- A test created by a second model can still be poor or superficial.
- A human-written test can still be tautological or badly scoped.
- The loop is not really enforcing independence; it is enforcing a social fact about authorship instead of an engineering fact about the test’s validity.

The result is a policy mismatch between what the gate wants to guarantee and what it actually measures.

### The wider implication

This also explains the second hole in the TDD model: for additive work, there may be no meaningful red test before scaffolding exists.

For a new API or new feature, the system may need a contract stub or scaffold first. Without that scaffolding, there is no failing behavior to assert yet. So the loop cannot cleanly support the “red test first” model for pure additive work in principle.

The real rule should not be “must fail on current code” in all cases. It should be “must define a failing contract before implementation, where a contract can exist.”

## 2. The system is not grounded in truly atomic Agile task items

The loop’s mode structure is expressive, but in practice the work is still often handled as broad, multi-stage tasks rather than atomic units of delivery.

This matters because Agile good practice is not only to break work down; it is to validate each deliverable with evidence.

In this repo, the design still allows:

- a broad defect description,
- a region selection,
- a broad patch,
- a multi-round review,
- and a final gate ladder that says yes/no.

That is not the same as decomposing work into small, independently verifiable task units with acceptance criteria and proof artifacts.

### Missing pattern

A stronger Agile pattern would be:

1. A single task item with clear acceptance criteria.
2. A test or reproduction that fails for that task.
3. A minimal patch.
4. A focused validation command.
5. A pass/fail conclusion tied to that task item.

The repo has many of these ideas, but not a strict, enforced task ledger across all operations.

## 3. Context and token bloat are handled, but only partially and with brittle limits

The repo does try to manage this well in several places, notably:
- [src/agent_loop/context.py](../../src/agent_loop/context.py)
- [src/agent_loop/memory.py](../../src/agent_loop/memory.py)
- [src/agent_loop/config.py](../../src/agent_loop/config.py)

This is the right direction. The problem is that the protections are still policy-level caps rather than a principled context architecture.

### Issues in current approach

#### A. Budget caps are local, not systemic
The system sets token limits in several places, but many of the inputs still flow through a long prompt chain:

- ticket + spec + regions
- context slice
- settled decisions
- learning feedback
- graph context
- review findings
- arbiter prompt
- role-specific instructions

These are not independent variables; they compound. A cap in one layer does not guarantee a cap across the whole experience.

#### B. The context is still a mixture of relevance, memory, and instruction
The loop mixes:
- core instructions,
- task data,
- memory history,
- graph context,
- review findings,
- adjudication precedents,
- output format requirements,

This is useful, but it means the prompt is not just “what’s relevant to this task.” It is “everything that has been judged useful, plus some survival scaffolding.” That is inherently unstable.

#### C. The system addresses bloat after the fact
It truncates and caps, which is necessary but not sufficient. The design still behaves as though context can be “pruned” to fit rather than “architected” to stay within bounds.

This means the system is still sensitive to prompt growth and model drift.

## 4. The loop is overly dependent on prompt grammar and output parsing

This is a recurring pattern across the codebase.

### Example: parser fragility
The project itself documents parser issues in [docs/architecture/BACKLOG.md](./BACKLOG.md), including cases where warnings, skipped tests, or malformed output were mistaken for terminal failures. Similarly, the gate and review parsing in [src/agent_loop/loop.py](../../src/agent_loop/loop.py) and [src/agent_loop/gates.py](../../src/agent_loop/gates.py) rely on structured markers such as `<<<VERDICT>>>`, `<<<FINDINGS>>>`, and expected output summaries.

This is an important design tradeoff: it makes the output machine-readable, but it creates a brittle dependency on the model returning a near-perfect format.

### Why this is risky

- When the output deviates slightly, the loop treats it as an unparseable or empty vote.
- The model is not always as disciplined as the parser assumes.
- The guardrails become more about the model conforming to the grammar than about the actual substance of the review.

A better design would separate semantic validation from rigid formatting and fall back to structured outputs or JSON where possible.

## 5. The loop has strong gates, but weaker truth criteria

The fatal issue is not that it has gates. It is that some gates measure proxies rather than actual correctness.

Examples:

- “a human wrote the test” instead of “the test is independent of the implementation path.”
- “the build passes” instead of “the behavior required by the task is proven.”
- “the reviewer responded in a parseable block” instead of “the review was substantively grounded in the code and task.”

This is a recurring theme in AI engineering loops: strong process gates are mistaken for strong correctness gates.

## 6. The system still conflates model behavior and engineering behavior

The loop is built to work around model unpredictability, but it still has some places where the model’s natural behavior is treated as a substitute for software rigor.

Examples:

- The output contract expects a certain generation style and block structure.
- The review process is highly sensitive to how the model emits findings.
- The model may respond with a huge number of findings or degenerate output, which the system then tries to detect and truncate.

This is a real engineering concern because the system is trying to convert a nondeterministic language model into a deterministic engineering workflow. Some of the safeguards help, but they do not fully solve the mismatch.

## 7. The budget and reasoning controls are better, but still unusually manual

[config.py](../../src/agent_loop/config.py) contains explicit comments that explain the reasoning-budget issue and the danger of thinking models consuming the same token budget as output. That is a strong sign the project learned the hard way.

However, this is still a patch-level response to a systemic issue: reasoning and output are competing for the same finite budget, and the model’s own non-deterministic reasoning patterns can make a run fail even when the final response would otherwise be valid.

This suggests a design requirement:

- reasoning budgets should be modeled and validated as part of task planning,
- not just patched into the config after live failures occur.

## 8. The prompt architecture is stronger than many loops, but still too prompt-heavy

The project already tries to inject:
- code context,
- graph context,
- memory,
- review precedents,
- instructions,
- acceptance criteria,
- and output formatting.

This is excellent for solving the “model doesn’t know enough” problem, but it also means the prompts are shipping a lot of historical baggage along with the active task.

This is where context management becomes critical. Without a strict filtering layer, the loop gradually drifts toward a giant prompt with weak signal-to-noise ratio.

## Additional issues worth tracking

### A. Policy vs. evidence gap
The loop often proves process compliance rather than actual behavior. A patch can pass a gate ladder and still fail the real intent in a subtle way if the test or acceptance signal is the wrong thing.

### B. Feature/additive work is under-specified
The “red test before fix” rule is ideal for regression work but weak for feature creation or additive API design. A good agent loop should distinguish between:

- regression repair,
- feature addition,
- additive API creation,
- refactor safety work.

Each requires a different validation contract.

### C. The system values fight/approval more than clarity of evidence
The panel and arbiter are useful, but if the evidence is noisy or the proxy is weak, the system can still produce an approval that is structurally correct but operationally misleading.

### D. The architecture is stronger than the current guarantees
The underlying design is much better than most loops, but the project still shows symptoms of being driven by a sequence of real incidents rather than a fully stable theory of correctness. That is not a failure; it is just a sign that the next step should be a stricter evidence model, not more feature modes.

## Additional engineering findings

The following are distinct from the TDD-independence and task-breakdown concerns above. They are based on the current execution paths and deserve a separate technical review.

### E1. Graph enrichment can dominate ticket latency and has no ticket-level deadline

**Severity: High for performance and reliability.**

`build_context_slice()` performs live MCP calls synchronously. For each discovered symbol it can make an outbound trace, inbound trace, and test search. With up to three names per region, a multi-region ticket can issue many serial RPC calls before the implementer is even invoked.

The client sets a nominal 120-second deadline, but it uses blocking `stdout.readline()` inside the deadline loop. If the server stops responding without closing stdout, the read itself can block past the deadline. There is therefore no reliable end-to-end bound on graph enrichment latency.

Impact:
- a non-essential context enhancement can block the entire ticket;
- latency scales with regions and extracted names, not with demonstrated value;
- failures are silently converted into missing context, making degraded operation hard to distinguish from irrelevant context.

**Recommendation:** make graph context a bounded, concurrent preflight with a small wall-clock budget; return a structured completeness status; cache by `(base_commit, symbol, graph_version)`; and skip live graph calls when a valid cache exists.

### E2. The MCP client can deadlock under server diagnostics

**Severity: High for reliability.**

The MCP process is created with `stderr=subprocess.PIPE`, but no code drains stderr. A sufficiently chatty MCP server can fill its stderr pipe and block, while the client waits on stdout for a reply. The current client therefore has a classic subprocess pipe deadlock risk.

This is especially relevant because the code treats MCP as a local enhancement: a diagnostic-heavy failure should degrade gracefully, not stall a ticket.

**Recommendation:** redirect stderr to a rotating artifact file, consume it asynchronously, or inherit stderr; add process health checks and surface the final stderr tail when context acquisition fails.

### E3. Context budgeting does not guarantee that the actual request fits

**Severity: High for cost predictability.**

The loop limits individual sources using independent character estimates, then appends them to prompts that also include pinned instructions, source regions, review history, feedback, and output-format contracts. `compaction.py` protects the system prompt, initial implement prompt, and newest exchange, but it never refuses a history whose protected portion alone exceeds the configured round budget.

As a result, the stated round input budget is an aspiration rather than an admission-control rule. A large ticket or region set can exceed it before compaction has any removable content.

Impact:
- provider context errors occur late, after preparation work and possibly paid calls;
- cache effectiveness is reduced because prompt shape changes unpredictably;
- cost is not predictable from the ticket size alone.

**Recommendation:** assemble a typed prompt manifest before any provider call; calculate the exact provider-specific token count where available; reserve output capacity; and reject or split a ticket before execution when pinned content exceeds the admissible budget.

### E4. Automatic context-window expansion trades a token error for a memory-capacity risk

**Severity: High for local-model performance.**

`_fit_num_ctx()` expands the Ollama context window from the prompt plus requested output budget, with additional headroom. This fixes an arithmetic impossibility, but it may request a very large KV cache for every call. There is no model-specific admission check against physical memory, model context capability, concurrent panel calls, or the cost of evicting a resident model.

For local inference, a large requested `num_ctx` can increase latency sharply, exhaust GPU or RAM, force model reloads, and make parallel review slower than serial review.

**Recommendation:** keep separate limits for input, visible output, and reasoning; use the model catalogue's measured context capacity as a hard cap; estimate KV-cache memory before dispatch; and choose panel concurrency from available capacity rather than reviewer count alone.

### E5. Memory retrieval is recency-based, globally scoped, and can poison future reviews

**Severity: Medium for correctness; High as history grows.**

Settled decisions and learning feedback are injected primarily by recency. They are not indexed by repository revision, language/profile, subsystem, task type, or semantic similarity to the current ticket. A decision that was correct for an older architecture can therefore bias a reviewer away from a valid new finding. Conversely, unrelated recent findings consume the small injection budget.

The stores are append-only and are reread in full for several operations. This is acceptable at small scale, but cost grows with history even though only a handful of entries are injected.

**Recommendation:** version all memory entries with repository identity, commit range, profile, subsystem and expiry; retrieve by relevance rather than recency; maintain a compact index or bounded rolling store; and mark old precedents as advisory rather than instructions.

### E6. Compaction preserves conversation form, but can lose the reasoning needed to repair a finding

**Severity: Medium for convergence quality.**

The mechanical compactor reduces prior reviewer feedback to counts, first lines, and abbreviated findings. The LLM compactor may omit older messages when its own input budget is exceeded. This is safe from a context-size perspective, but it changes the semantic state of the work: the implementer may receive “a finding existed” without the exact reproduction, constraint, or rejected alternative needed to address it correctly.

This creates a risk of recurrent fixes, repeated reviewer findings, and expensive rounds that appear to be model non-convergence but are actually information loss.

**Recommendation:** store findings and rulings as structured objects outside chat history; build each next-round prompt from the unresolved finding set and immutable evidence, not a lossy transcript summary; measure which compacted facts reappear as repeated findings.

### E7. Worst-verdict panel logic amplifies false positives and cost

**Severity: Medium for throughput and decision quality.**

The panel uses a worst-verdict rule and invalidates the panel when any reviewer is unreachable or unparsable. This protects against a silent false approval, but it means one unstable or systematically pessimistic reviewer can repeatedly force additional rounds and model calls. The solo-REJECT downgrade addresses one visible form of this, but not persistent REVISE verdicts or recurring low-quality findings.

The system needs calibration, not only quorum mechanics. Without measured reviewer precision and recall by task class, adding reviewers can increase cost and reduce convergence.

**Recommendation:** track reviewer-level false-positive, duplicate-finding, timeout, and convergence effects; use a minimum evidence threshold for a blocking finding; and dynamically reduce or replace reviewers whose marginal contribution is negative.

### E8. The cost model is incomplete for operational optimization

**Severity: Medium for cost management.**

The provider layer records token counts and some per-token pricing, but the loop's meaningful cost is broader: cache writes, reasoning tokens, retries, local GPU time, graph-server time, build/test execution, worktree creation, and panel wall-clock latency. For subscription-priced or local models, reporting `$0.00` does not mean the run is free; it masks the scarce resource actually being consumed.

**Recommendation:** report a per-ticket resource ledger with input/output/reasoning tokens, cache hit rate, model seconds, wall-clock seconds, graph-call count and time, test/build time, retries, and estimated local memory pressure. Optimize on cost per accepted task and cost per verified change, not token count alone.

### E9. Full-suite baseline and repeated full-suite checks make small tickets expensive

**Severity: Medium for throughput.**

The worktree captures a full test baseline before the ticket, then later runs the configured test command during gates and developer-tool interactions. This is defensible for correctness, but the default shape makes every tiny ticket pay the cost of the entire suite repeatedly. The result will be poor queue throughput on large repositories and an incentive to weaken test commands.

**Recommendation:** retain one full baseline and final full verification, but support a declared focused test command per task for iteration; verify that the focused selection includes the changed symbols and acceptance tests; and periodically sample or require full-suite checks based on risk classification.

### E10. The architecture needs load and fault-injection testing, not only functional acceptance tests

**Severity: High for production readiness.**

The current acceptance tests are valuable, but the major risks above are operational: blocked MCP reads, stderr backpressure, oversized context windows, slow suites, provider partial responses, concurrent worktrees, and stale-memory effects. Those will not be reliably discovered through ordinary mocked or happy-path tests.

**Recommendation:** create an engineering validation matrix that injects provider timeouts, malformed JSON-RPC, slow/stuck graph responses, full stderr pipes, model context refusals, collection errors, concurrent promotions, and large-history compaction. Record latency and resource budgets as test assertions.

## Recommended enhancements

### 1. Replace the “human-authored test” proxy with a true independence rule
Instead of checking whether a human authored the tests, check that:

- the test was generated from the ticket spec,
- the implementation path was excluded from that generation pass,
- the test is independent from the code being patched,
- the test is red before the fix when a red contract is meaningful.

### 2. Define task types explicitly
Separate task classes:

- regression repair
- bug fix with red test
- feature addition with contract-first validation
- additive API work with scaffolded contract tests
- refactor safety work

Each should have a separate validation rule.

### 3. Add a task-level evidence ledger
Each task item should record:

- id
- acceptance criteria
- failing or red contract
- validation command
- output result
- pass/fail status
- artifact path

This turns the loop into a delivery workflow rather than a prompt churn engine.

### 4. Introduce a structured validation contract
Prefer structured machine-readable result objects over brittle parser sniffing. If the runner output cannot be parsed, the task should fail closed and report “validation inconclusive,” not “green by default.”

### 5. Treat context management as a first-class architecture concern
The current caps are good but not enough. The loop should have explicit context policy layers:

- active task context
- historical memory
- learned precedents
- graph relevance
- validation evidence

with strict pruning rules and a measured signal-to-noise threshold.

### 6. Distinguish verification quality from process compliance
The loop should explicitly measure:

- false green rate,
- false red rate,
- validation inconclusive rate,
- prompt bloat rate,
- context truncation rate,
- approval confidence.

That would make the system much more understandable and significantly less fragile.

## A more comprehensive engineering review would also cover

A deeper engineering review should not stop at prompt quality or TDD semantics. It should also assess the system as a production-grade engineering platform.

### 1. System boundaries and failure modes
The loop is managing multiple state machines at once: ticket state, review state, arbiter state, worktree state, and memory state. Those boundaries are not always cleanly separated, and a failure in one layer can silently contaminate another.

A true engineering review should ask:
- where the state model is explicit and where it is implicit,
- what happens when a provider returns partial data or a malformed response,
- how the system behaves under timeouts, retries, and partial failures,
- whether the loop fails closed or silently falls back.

### 2. Observability and debugging
An engineering loop needs excellent observability. Right now the system appears to have strong logs and artifacts, but the critical test is whether a human can reconstruct the exact causal path from prompt to result.

The review should ask:
- can a debugging engineer reconstruct the full prompt history for a failed run,
- are artifacts tied to exact tickets and rounds,
- are gate outcomes and reviewer findings preserved in a form that makes debugging possible,
- are there explicit metrics for false positives, false negatives, and unparseable responses.

### 3. Architecture of verification
A critical engineering question is whether verification is built around truth or around model convenience. Right now the system uses a powerful combination of local gates and review panels, but it still needs a stronger claim about what is actually proven.

The deeper review should ask:
- what is the system proving at each stage,
- what is merely a proxy,
- which gates are evidence-based and which are process-based,
- how often do passes rely on parser assumptions instead of functional truth.

### 4. Safety, auditability, and rollback
For any autonomous engineering system, rollback and auditability are as important as decision quality. The repo has worktree isolation and logs, which is good, but a serious engineering review should examine:
- whether all generated files are traceable to a ticket and round,
- whether patch promotion is reversible and validated,
- whether the loop can detect when it is operating on stale context,
- whether conflicting work is detected before it is promoted.

### 5. Maintainability and extensibility
The architecture is ambitious and modular in places, but it is also rich with task-specific logic and evolving heuristics. A comprehensive engineering review should assess:
- whether the loop’s configuration surface is actually stable,
- whether the provider and profile abstractions are expressive enough,
- whether new roles or modes can be added without breaking invariants,
- whether prompt logic and policy logic are separated cleanly enough to evolve.

### 6. Operational cost and reliability
There are real gains in structure, but the cost is still high: multi-round prompting, panel review, context enrichment, memory injection, and graph work all stack up. A production engineering review would ask:
- does the loop have predictable runtime cost,
- what is the failure cost of a bad run,
- how much variance is caused by model choice versus pipeline design,
- where the system is brittle under provider drift, output shape drift, or context growth.

### 7. Human oversight model
The loop is explicitly trying to reduce the need for a human to manually review every patch, but it still needs a clear boundary for where human intervention is required. That boundary should be defined in operational terms, not just in model output terms.

The review should ask:
- what should remain human-owned,
- what can be safely automated,
- where the system is allowed to proceed without approval,
- when a run should escalate instead of continuing.

## Conclusion

The loop is already much stronger than a raw agentic coding prototype. It has thought through many failure modes and built in structured controls that materially improve safety and reliability.

But the critical review point is this: the system is still trying to enforce process-quality proxies for correctness. The most important conceptual gap is the TDD independence rule, and the second is the lack of a truly atomic, evidence-based task model.

If those two are tightened, the loop becomes much more credible as an engineering workflow rather than an impressive but still somewhat brittle prompt orchestration system.

---

## Addendum: Independent Engineering Review & Code Audit

A subsequent, independent engineering audit of the codebase (`src/agent_loop`) was conducted to evaluate process IPC, state machines, context/token accounting, provider transport resilience, memory storage indexing, and gate verification mechanics.

The comprehensive findings, architectural flowcharts, vulnerability analysis (including MCP stdio deadlock hazards and token budget overruns), and prioritized action matrix are documented in:
* **[AGENT_LOOP_INDEPENDENT_ENGINEERING_REVIEW.md](./AGENT_LOOP_INDEPENDENT_ENGINEERING_REVIEW.md)**

