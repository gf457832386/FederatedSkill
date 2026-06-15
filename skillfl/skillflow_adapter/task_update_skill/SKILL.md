---
name: task-update
description: Cloud-side step 1 — maintain task_memory.md as the family's running list of tasks NOT YET adequately covered by the workers' libraries. Drop tasks once covered; add tasks the patches reveal aren't covered.
---

# task-update

You are step 1 of a two-step cloud merger pipeline.

**Step 1 (this step):** Maintain `task_memory.md` — a running list of **what tasks in this family are NOT YET adequately covered** by the workers' current libraries. Each round you reassess: drop tasks the patches+rewards show are now covered; add tasks the patches reveal aren't covered.

**Step 2 (NOT your job):** Per-worker library merge. Each worker's merger reads `task_memory.md` you produce and uses the gap list to decide what library work is still needed.

**You do not touch any library.** Your only output is an updated `task_memory.md`.

## Inputs (already in cwd)

```
task_memory.md         your running gap list (read at start, write at end).
                       On round 0 this is a stub.
library_skills.md      digest of every worker's pre-task library this round —
                       skill name + description per worker. NOT full SKILL.md.
                       Use this to decide which tasks are already covered.
patches/<wid>/         every worker's proposals from this round (read-only)
  meta.json              {worker_id, reward, summary, delete_paths}
  files/                 the proposed skill files (SKILL.md + scripts)
meta.json              {family, round_idx, all_workers, worker_models}
```

You do **not** see: full library/, peer trajectories, original task statements. Coverage judgment is made from skill descriptions + reward signals — not full SKILL.md content.

## What task_memory.md is

It's a **per-worker coverage matrix**, not a single covered/uncovered flag and not a history log:

- Each entry describes a task observed in this family, plus **which workers (models) have it covered and which still have a gap**.
- Coverage is per-worker because libraries are per-worker. A task that worked for `u1 (glm-5)` is NOT automatically covered for `u0 (qwen3.6-plus)`. Their libraries diverge; their models have different strengths.
- An entry stays in task_memory as long as **any** worker still has a gap on that task. It is fully removed only when **every** worker is covered.
- New entries get added when this round reveals a task type that no worker yet handles (or that the relevant workers handle poorly).

When the matrix is dense with `covered for all workers`, the family is in good shape. When some cells are gaps, those are exactly the work step-2 mergers should focus on — and crucially, *which worker's library to fix* is read off the matrix.

## Reward semantics — read carefully

Every patch in `patches/<wid>/meta.json` has a `reward` field. Two things must be clear about what it means:

**1. `reward = 1.0` is the only "task passed" value.** Anything below 1.0 means **the task did NOT fully pass** — the verifier's outer test failed. The fractional value (0.5, 0.66, 0.8 etc.) is the inner sub-test pass rate (how many pytest sub-tests inside the failed verifier passed). It tells you *how partial* the failure was, not that the task partially succeeded.

   - `reward = 1.0` → covered (task passed)
   - `reward < 1.0` (any value, including 0.5, 0.66, 0.9) → **not covered** (task failed). Use the fractional value only as a partial-progress hint when writing per-worker findings.
   - Don't treat 0.5 or higher as "half-success". A 0.66 partial is still a fail — the worker did not produce verifier-accepted output.

**2. Reward is per-(worker, model, task).** That reward is the result of **that specific worker's specific model attempting that specific task this round**. It does NOT mean the family's task is solved for everyone. Always interpret reward in the context of `(worker_id, model, this round's task)`.

A common misread to avoid: "u1 got reward=1.0, so this task is covered, drop it." Wrong — only u1 (and only u1's model glm-5) is confirmed covered. u0 and u2 are unconfirmed, and if their patches have reward=0 the task is likely a gap for their models specifically.

## How to classify each (task, worker) cell

For this round's task, determine each worker's status by combining:
- The patch's `reward` (per-worker signal that this worker tried and succeeded/failed)
- Whether the worker's library in `library_skills.md` has a skill whose description matches this round's task

Common cases (note: "reward < 1.0" means task did NOT pass — see Reward semantics above):

| this round's patch reward (worker W) | W's library has a matching skill? | meaning for (task, W) | matrix action |
|---|---|---|---|
| **1.0** | yes | W passed, library skill works | mark `covered` for W; if all workers `covered`, drop entry |
| **1.0** | no, but the patch this round adds a matching skill | absorbing — will be covered after step-2 merges this patch | mark `absorbing` for W |
| **< 1.0** (e.g. 0.0, 0.5, 0.66, 0.9) | no | task NOT passed; library lacks the workflow | mark `gap (reward=X.X)` for W; flag what's missing. Cite the actual reward value to indicate how partial the failure was. |
| **< 1.0** | yes | library has the skill but task NOT passed — skill is broken or wrong-grain | mark `gap (broken skill, reward=X.X)` for W; step-2 should fix the skill, not ignore |
| not in this round | (not directly tested this round) | carry over from last round's entry | keep prior status |

These are common cases — read the actual evidence and use judgment. Edge cases:

- A patch reward=1.0 that *replaces* a library skill at the same name → that worker's status stays `covered`; the underlying task is still covered.
- A library skill whose description matches the round's task closely but reward is 0 → check whether the patch shows the worker actually invoked that skill or wrote one elsewhere; sometimes workers ignore their library and the skill isn't really tested.
- A new sub-domain emerging that no worker's library handles → add a row with all workers marked `gap`.
- A persistent per-model failure (e.g., `u0 (qwen)` has had `gap` on a task type for 3+ rounds while `u1, u2` are covered) — this is exactly the signal step-2 needs. Note it explicitly so step-2 mergers know to refactor `u0`'s library specifically rather than mirroring `u1` or `u2` content.

## Format — your structure, but here's a useful shape

The substance is a **(task × worker) coverage matrix**. Inline tables are usually clearest. Each section below corresponds to a section of the final `task_memory.md`.

### Task buckets section

For each bucket: what the task family actually requires, broken down by INPUT (data shape, file types), TRANSFORMATION (the workflow steps that must happen), OUTPUT (schema, format, invariants the verifier checks). Recently seen rounds in parens.

```markdown
## Task buckets (concrete, observed)

### B1: Image OCR → Excel with reference-list validation (R0, R1, R2 — labels, orders, claims)
- INPUT: directory of label/order/claim images; optionally a reference CSV
  (known orders / employee roster) for ID validation/enrichment
- TRANSFORMATION: pre-process (grayscale + contrast) → multi-PSM OCR (3, 4, 6,
  11) → regex parse for date+amount+ID → cross-ref against CSV (drop or
  enrich rows by ID hit) → dedup by composite key → sort by filename
- OUTPUT: Excel with strict schema {filename, id, date, total}; date as
  ISO YYYY-MM-DD; amount as float with exactly 2 decimal places; rows sorted
- Common failures: row count mismatch when dedup is wrong; OCR mis-reads
  '0' as 'O' on uppercase IDs; date format normalized inconsistently for
  partial-year rows like 'II/2024'

### B2: Image OCR → Excel template fill (R4 — utility bills)
- INPUT: utility bill images + a template Excel workbook with placeholder
  rows in a target sheet plus read-only reference sheets (e.g. "cover")
- TRANSFORMATION: load template with openpyxl preserving styles → OCR each
  bill → parse date+amount with line-break tolerance → clear placeholders
  → write rows into target sheet (NOT new workbook)
- OUTPUT: same workbook structure as template, target sheet populated,
  cover sheet untouched
- Common failures: workers create new workbooks instead of editing template;
  styles get lost when openpyxl reads with data_only=True
```

### Coverage matrix section

```markdown
## Coverage matrix

| Task bucket | u0 (qwen) | u1 (glm-5) | u2 (minimax) |
|-------------|-----------|------------|--------------|
| B1 (image OCR + ref list) | gap (broken: u0's `extract-label-data` reward=0 R1, R2; OCR script can't handle multi-PSM, CSV cross-ref logic skips rows with hyphenated IDs) | covered | covered |
| B2 (image OCR + template) | covered | gap (no skill: u1 has only B1 skill, R4 reward=0; template-write path absent) | covered |
```

## Per-worker findings — only for workers with REAL gaps; only based on FACTS

**Critical scoping rule.** Write per-worker findings **only for workers that have a real coverage gap** (one or more cells `gap` / `gap (broken skill)` in the matrix). For workers whose cells are all `covered`, the findings section is a single line: `- u_X (model): stable, do not over-engineer`.

Why: when step-2 sees detailed "weaknesses" for a covered worker, it tries to fix what isn't broken — and breaks the working library. (We've observed this regression: detailed findings for an otherwise-stable worker triggered library changes that dropped the worker's reward in subsequent rounds.)

**Evidence rule — record facts, don't speculate.** Every claim in this section must be tied to a specific round and the literal reward observed. Forbidden:

- "u0 is volatile" without citing rounds.
- "u1 fails on new input formats" from a single round.
- "u0 silently uses default args" without an actual round where the patch / patcher summary shows that behavior.
- Inventing a "workaround" whose effectiveness has not been demonstrated. Workarounds are speculative — flag them as such ("hypothesis: …") rather than asserting them as solutions.

A well-formed finding for a gap worker has:

- **Capability deficit** — describe the actual behavior observed in this round's patch (or in the patcher summary). Quote or paraphrase what the patcher reported, with the round number. Example: "R7 patcher summary cites 'agent wrote inline Python for backlog calculation despite SKILL.md anti-pattern' — patch reward=0.5 (task did NOT pass)."
- **Pattern claim only with ≥2 rounds.** A single round = "observed in R7", not "u0 has a pattern of …".
- **Workaround proposed cautiously** — phrase as "hypothesis for step-2: try X" not "fix: X". Speculation is OK but must be flagged. Step-2 will try it and the next round's evidence will confirm or refute.

Findings are NOT:
- Strengths. The matrix already shows what works.
- Single-round events generalized into patterns.
- Confident prescriptions for `covered` workers (use the `stable, do not over-engineer` line for them).

Example findings (this family has u0 with persistent gaps and u1/u2 covered):

```markdown
## Per-worker findings

- **u0 (qwen3.6-plus) — gap worker:**
  - **R7 patcher summary**: "agent wrote inline Python for backlog calculation
    despite SKILL.md anti-pattern; trace shows wrong constants used (108 vs 180)"
    — patch reward=0.5 (task did NOT pass).
  - **R8 patcher summary**: "agent wrote inline Python for JSON parsing AND
    backlog chains; subtle precision/format mismatches" — patch reward=0.5
    (task did NOT pass).
  - **Pattern (≥2 rounds)**: u0 writes inline Python for computation steps
    despite SKILL.md prohibitions (R7 + R8). Quote in task_memory comes from
    patcher summaries, not invented.
  - **Hypothesis for step-2**: try a more prominent anti-pattern format
    (e.g. WRONG/RIGHT side-by-side, or a STOP-checkpoint at the top of
    SKILL.md). Effectiveness untested — next round will tell.
- **u1 (glm-5) — covered worker**: stable, do not over-engineer.
- **u2 (MiniMax-M2.5) — covered worker**: stable, do not over-engineer.
```

## What's missing section (for step-2 mergers)

```markdown
## What's missing (for step-2 mergers)

- u0's B1 skill needs a concrete fix: explicit `format(amount, ".2f")` rule,
  multi-PSM combination via `set().union(...)`, fall-through path when CSV ID
  has hyphens. Don't copy u1's B1 skill — u1's verbose workflow style hurts qwen.
- u1 needs a B2 (template-fill) skill. Apply u2's B2 patch (reward=1.0); u2's
  template-preservation steps are a good fit.
```

If a different structure suits this family (e.g., grouped by sub-domain when there are many tasks), use it. The constraint is on substance:

- **Concrete task descriptions** — not "OCR family covered" but "input X → transformation Y → output Z, common failure modes, what verifier checks". The next round's mergers should be able to design a library skill from your task description without reading patches themselves.
- **Per-worker coverage cells** show *what specifically broke*, not just `gap`/`covered`. "gap (broken: extract-label-data fails at multi-PSM combine, R1 R2)" beats "gap".
- **Per-worker findings are model-specific patterns**, accumulated across rounds. "u0 silently rounds floats" ← cumulative observation. NOT round-by-round logs.
- **No per-round journal**. Don't list R0:..., R1:..., R2:... — rows are tasks, columns are workers, evidence is cited inline.
- **Drop only when all workers covered**. Partial coverage stays.

## Workflow

1. Read `task_memory.md`, `library_skills.md`, all `patches/<wid>/meta.json` AND each `patches/<wid>/files/<skill>/SKILL.md` body. **Read enough to write a real task description** (input/transformation/output), not just summarize the patcher's one-line summary.
2. **Update or add task buckets:**
   - Refine an existing bucket's INPUT/TRANSFORMATION/OUTPUT description if this round's evidence clarifies it (new input format, new edge case, verifier rule revealed).
   - Add a new bucket only if this round's task is genuinely a new input modality + workflow combo.
   - Drop a bucket only when every worker has it `covered` AND it hasn't been touched in 2+ rounds (still useful as a record otherwise).
3. **Update worker cells per bucket** (this round's per-worker rewards + library_skills):
   - `covered` → only when this worker had reward=1.0 this round on this bucket AND has a matching library skill.
   - `gap (broken: ...specifics...)` → reward=0 with matching skill. **Cite what failed**, e.g. "OCR script can't handle multi-PSM combine; CSV cross-ref skips hyphenated IDs".
   - `gap (no skill)` → reward=0 with no matching library skill.
   - `absorbing` → patch this round adds the skill at reward=1; will be covered after step-2 merges it.
4. **Update per-worker findings (model-specific patterns):**
   - Each worker block should describe **patterns across rounds**, not events. Aim for: "qwen silently rounds floats" / "minimax ignores prescriptive scripts when prose alternatives are present" / "glm-5 fails when SKILL.md references missing helper scripts".
   - Add a new finding only when you have ≥2 rounds of evidence supporting it. Don't write "qwen failed R7" — write the abstracted pattern, with rounds in parens.
   - When this round's evidence contradicts a prior finding, edit or remove it.
5. **Update "what's missing" with concrete next-step instructions** for step-2 mergers — what to apply, what to NOT copy from peers, what specific fix to attempt for a broken-skill cell.
6. **Hard cap: 100 lines.** Concrete descriptions earn space. Per-round event logs do not. Compress event-style content into pattern-style content.
7. Write `DONE.txt`.

## What to avoid

- **Vague task descriptions** ("capacity planning + summary validation"). Every bucket needs INPUT/TRANSFORMATION/OUTPUT specifics that step-2 can use to design or fix a library skill.
- **Vague gap cells** (just "gap"). Always cite what specifically failed (script bug, missing helper, format mismatch, model-specific pattern).
- **Vague per-worker notes** ("u0 has model-specific issues"). Concrete: what does u0 do that produces wrong outputs? What style of SKILL.md unlocks it?
- **Per-round event journal.** No "R3: u0 applied X, R4: u1 rejected Y". Events compress into patterns.
- **Dropping rows when only some workers are covered.** Partial coverage stays — step-2 needs the gap signal.
- **Articulating rigid rules.** Don't write "all skills MUST be named X-Y-Z". Stick to coverage facts and per-worker observations.

## Budget

≤10 turns, ≤5 minutes. There are no library writes, no validation scripts, no decisions log — just read inputs and rewrite one file. If you hit turn 8 without writing task_memory, write what you have and exit.
