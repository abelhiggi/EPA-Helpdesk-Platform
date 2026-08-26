# Custom metrics and the improvement they drove

> **This document is a template with worked structure — you must run the
> experiment and replace the bracketed figures with your own.** Fabricated
> numbers will not survive questioning, and this is the single strongest piece
> of distinction evidence in the project. Budget two hours.

## Why these three metrics

Infrastructure metrics tell you the platform is healthy. They cannot tell you
the platform is *doing the right thing*. All three below are emitted from the
process handler via CloudWatch Embedded Metric Format — a structured log line
that CloudWatch converts to a metric, so there is no `PutMetricData` permission
on the runtime role and no synchronous API call in the request path.

| Metric | Question it answers | Why an existing metric could not |
|---|---|---|
| `TicketsSubmitted` by Category | Which team is carrying the load? | Lambda invocations count requests, not what they were about |
| `CategorisationConfidence` | Can the AI triage be trusted? | Nothing in CloudWatch knows what the model was unsure about |
| `TimeToRouteSeconds` | How long does a person wait before anyone owns their request? | Lambda duration measures one function, not the end-to-end wait |

## The experiment

**Method.** [N] representative tickets seeded through `POST /tickets`, drawn
from the language people actually use in the shared mailbox rather than clean
synthetic phrasing. Confidence broken down by category over the run.

**Baseline.**

| Category | Tickets | Mean confidence | Below 0.7 |
|---|---|---|---|
| network | [ ] | [ ] | [ ]% |
| software | [ ] | [ ] | [ ]% |

**What the metric showed.** [Which category was weak, and what the low-confidence
tickets had in common. The pattern is the finding — for example, tickets that
describe a symptom without naming a system, or vocabulary that spans both
categories such as a printer that is both a device and a network endpoint.]

**Hypothesis.** [State it as something that could be wrong. For example: the
model has no examples of ambiguous phrasing, so borderline tickets fall to a
coin toss rather than a considered call.]

**Change made.** [What you altered in `PROMPT` in `src/process/handler.py`.
Include the diff. Keep it to one change so the effect is attributable.]

**After.**

| Category | Tickets | Mean confidence | Below 0.7 |
|---|---|---|---|
| network | [ ] | [ ] | [ ]% |
| software | [ ] | [ ] | [ ]% |

**Result.** [Effect size. If it did not work, say so and say what you would try
next — a negative result honestly reported reads better than a suspiciously
tidy improvement.]

## How this would be delivered as a standing practice

The one-off experiment is not the point; the point is that the metric makes the
problem visible on an ongoing basis.

- **Interpret.** Mean confidence per category is on the dashboard. A sustained
  fall means the incoming vocabulary has drifted away from the prompt — new
  system rolled out, new failure mode, seasonal change.
- **Implement.** A confidence alarm below [threshold] over [period] would
  trigger a prompt review. Not built: at current volume it would be noise, and
  an alarm nobody trusts is worse than no alarm. Wiring it is a five-line change
  once volume justifies it.
- **Deliver.** Low-confidence tickets are the labelled dataset for the next
  prompt iteration. Each cycle is a prompt change behind the existing
  `AI_CATEGORISATION_ENABLED` toggle, verified against the same seeded set, and
  shipped through dev before prod.

## Second improvement area

`TimeToRouteSeconds` at p90 was [ ]s against a target of 60s. [What dominates
the figure — cold start, Bedrock latency, queue wait? What you would change,
and what it would cost. Naming a change you decided *not* to make, with the
reason, is stronger than pretending everything was optimised.]

## Memory headroom

`memory_utilization` is on the dashboard alongside duration. [Peak utilisation
across functions, and whether any memory size should change. Lambda does not
expose CPU directly — CPU scales with allocated memory, so memory utilisation
plus duration is how you reason about compute headroom here. Say that out loud
in the practical; it is a question assessors ask because Lambda has no CPU
metric to point at.]
