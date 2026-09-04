# Custom metrics and the improvement they drove

## Why these three metrics

Infrastructure metrics show the platform is healthy. They don't show it's
*doing the right thing*. All three below are emitted from the process
handler via CloudWatch Embedded Metric Format, a structured log line
CloudWatch converts to a metric, needing no `PutMetricData` permission and
no synchronous API call in the request path.

| Metric | Question it answers | Why an existing metric could not |
|---|---|---|
| `TicketsSubmitted` by Category | Which team is carrying the load? | Lambda invocations count requests; they don't say what a request was about |
| `CategorisationConfidence` | Can the AI triage be trusted? | Nothing in CloudWatch knows what the model was unsure about |
| `TimeToRouteSeconds` | How long does a person wait before anyone owns their request? | Lambda duration measures one function; it says nothing about the end-to-end wait |

## The experiment

**Method.** I seeded 30 tickets from `tickets.txt` through `POST /tickets`,
worded the way people actually write ("Excel broken", "No internet"), and
recorded confidence and routing per ticket by expected category.

**Baseline.**

| Category | Tickets | Mean confidence | Below 0.7 | Misrouted |
|---|---|---|---|---|
| network | 14 | 0.84 | 1 (7%) | 2 |
| software | 16 | 0.84 | 1 (6%) | 1 |

**What the metric showed.** Every problem ticket, misrouted or below 0.7,
described a symptom without naming a specific system. Two of the three
misrouted tickets scored 0.85, so confidence alone wasn't the signal.

**Hypothesis.** The model had no example of a ticket naming no system, so it
guessed inconsistently whether "everything" or "nothing works" meant a
shared failure or a single account problem.

**Change made, iteration 1.** I added few-shot examples to `PROMPT` in
`src/process.py`, including two symptom-only cases (commit `0475eb1`):

```
"Nothing works this morning" -> {"category": "software", ...}
"Everything slow for whole team" -> {"category": "network", ...}
```

**After, iteration 1.**

| Category | Tickets | Mean confidence | Below 0.7 | Misrouted |
|---|---|---|---|---|
| network | 14 | 0.84 | 0 | 2 |
| software | 16 | 0.80 | 2 (13%) | 2 |

Network cleared its below-0.7 ticket, but its misroutes didn't move.
Software got worse: mean confidence fell to 0.80, and a previously correct
ticket, "Login page for the portal won't load at all", was newly misrouted.

**Change made, iteration 2.** The prompt's examples implied a convention the
original labels disagreed with. I applied that convention to `tickets.txt`
instead of the prompt again (commit `de703ea`).

**After, iteration 2.**

| Category | Tickets | Mean confidence | Below 0.7 | Misrouted |
|---|---|---|---|---|
| network | 15 | 0.83 | 1 (7%) | 1 |
| software | 15 | 0.81 | 1 (7%) | 0 |

**What changed, per baseline problem ticket.**

| Ticket | Baseline | Iteration 1 | Iteration 2 |
|---|---|---|---|
| Can't reach the case management system | net, correct, 0.6 | 0.75, fixed | 0.75, fixed |
| Please help urgently | soft, correct, 0.6 | 0.6, same | 0.6, same |
| Been trying since 8am | net, misrouted->soft, 0.85 | 0.75, same | label->soft, 0.75, correct |
| Screen just froze | net, misrouted->soft, 0.7 | 0.75, same | label->soft, 0.75, correct |
| Everything is really slow | soft, misrouted->net, 0.85 | 0.75, same | label->net, 0.75, correct |

**Result.** Iteration 1 alone made software worse. Iteration 2 recovered the
three original misroutes by fixing the label instead, and misrouted tickets
fell from 3 to 1. Below-0.7 tickets stayed at 2 in every
run, they just moved. "Please help urgently" never improved: 0.6 in all
three runs. "Nothing is working this morning" is misrouted for a new
reason: relabelled to network, but the model still says software at 0.6,
unchanged since baseline. The convention fixed three tickets and created
one new disagreement.

## How this would be delivered as a standing practice

The one-off experiment is not the point; the point is that the metric makes the
problem visible on an ongoing basis.

- **Interpret.** Mean confidence per category is on the dashboard. A sustained
  fall means incoming vocabulary has drifted: new system, new failure mode,
  seasonal change.
- **Implement.** A confidence alarm below [threshold] over [period] would
  trigger a prompt review. Not built: at current volume it would be noise.
  Wiring it is a five-line change once volume justifies it.
- **Deliver.** Low-confidence tickets are the labelled dataset for the next
  prompt iteration, verified against the same seeded set and shipped behind
  `AI_CATEGORISATION_ENABLED` through dev before prod.

## Second improvement area

`TimeToRouteSeconds` at p90 was [fill from dashboard: p90 value and the time
range it covers]s against a target of 60s. [What dominates it, cold start,
Bedrock latency, queue wait? What you'd change, and what it would cost. A
change you decided not to make, with the reason, is worth stating too.]

## Memory headroom

`memory_utilization` is on the dashboard alongside duration. Allocated
memory, from `infra/helpdesk_stack.py`, against peak utilisation:

- Ingest: 256 MB allocated. Peak utilisation: [fill from the dashboard]
- Process: 512 MB allocated. Peak utilisation: [fill from the dashboard]
- Redrive: 256 MB allocated. Peak utilisation: [fill from the dashboard]

Lambda does not expose CPU directly; CPU scales with allocated memory, so
memory utilisation plus duration is how compute headroom is reasoned about
here.
