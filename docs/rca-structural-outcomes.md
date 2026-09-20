# RCA structural outcomes — the cross-phase contract (#177)

The causal-inference epic (#177) replaces "guess the guilty service" with a **deterministic
structural resolution**: partition the causal hypotheses into equivalence classes over the *usable*
observations (Phase D, #182), then read the outcome off that partition (Phase E, #183). This file is
the **authoritative outcome algebra** shared by Phase D (which produces the partition), Phase E (which
labels it), and Phase H (which evaluates it). Code comments do not define outcomes; this doc does.

## The partition (Phase D)

`partition(H, O, policy)` returns, over a **non-empty** hypothesis set `H`:

- `classes` — the surviving `~_O` equivalence classes (hypotheses sharing a full structural signature
  over `F_usable`), each with its `members` and its `D_missing` (the uncollected distinguishers);
- `eliminated` — the hypotheses a *usable* observation hard-contradicted.

A valid multi-member class always has a non-empty `D_missing` (fully indistinguishable distinct
hypotheses are rejected at the boundary as degenerate).

## The four outcomes

The outcome is a total function of the partition's **class cardinality** — never of ranker scores:

| # surviving classes | class shape | Outcome | Meaning |
|---|---|---|---|
| exactly 1 | singleton (1 member) | **IDENTIFIED** | one hypothesis is the unique structural explanation |
| exactly 1 | multi-member (≥2) | **NON_IDENTIFIABLE** | the survivors are structurally indistinguishable given what was collected; `D_missing` names what would separate them |
| ≥ 2 | any | **UNCERTAIN** | more than one distinct structural explanation survives |
| 0 | — (all in `eliminated`) | **NO_COMPATIBLE_HYPOTHESIS** | every hypothesis was hard-contradicted by a usable observation |

`NO_COMPATIBLE_HYPOTHESIS` is the fourth outcome (the pre-D spike named the prototype `NONE`). It is
**not** one of the three positive resolutions: it is a coverage/model failure — the hypothesis set did
not contain a cause consistent with the evidence, *or* the observations are contradictory. Because
`partition` requires a non-empty `H` and a zero-class partition must carry a non-empty `eliminated`
(enforced by `Partition`), this outcome can only mean "all eliminated" — never "nothing supplied".

## Evidence semantics (Phase E packet)

- **IDENTIFIED** — the single hypothesis, its supporting usable observations, and its soft support.
- **NON_IDENTIFIABLE** — the class members and the (non-empty) `D_missing` distinguisher set,
  source-tagged, i.e. *what to collect* to resolve them.
- **UNCERTAIN** — the surviving classes, each summarized as above.
- **NO_COMPATIBLE_HYPOTHESIS** — the `eliminated` hypotheses with, for each, the usable observation
  that contradicted it; the honest "nothing here explains this" packet, plus any integration gaps.

## Evaluation treatment (Phase H)

- **IDENTIFIED** scores as a positive localization (correct iff the identified hypothesis is the
  ground-truth cause).
- **NON_IDENTIFIABLE** and **UNCERTAIN** are *honest partial* results: a case is a success when the
  ground-truth cause is retained in the surviving classes (they are not counted as wrong answers), and
  a separate resolution metric tracks how often the telemetry pinned a unique cause. This is the
  `struct_ok` vs `unique` split validated in the pre-D spike.
- **NO_COMPATIBLE_HYPOTHESIS** is neither a hit nor a confident miss: it is scored as an abstention /
  coverage failure (never as a wrong localization), and flags the case for hypothesis-ontology or
  collection gaps.

Phase E (#183) implements the labels and packets; Phase H (#186) implements the evaluation. Both defer
to this table.
