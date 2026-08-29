# Task 1: AI Platform Design

## Summary

Keep LiteLLM as the mandatory gateway and add a thin **policy control plane** around it.
The shift is from convenient model access to **governed access**: every request carries an
owner, data class, allowed route, budget policy, logging mode, and operational signal, and
the platform — not the model alias a team happens to call — decides what applies.

The current stack is used deliberately: LiteLLM on ECS Fargate stays the enforcement point
(auth, routing, rate/budget caps, callbacks); Postgres stays the source of truth for keys,
spend, policy, and audit references; Langfuse stays trace-level debugging; Datadog becomes
the platform operational view (SLIs/SLOs/alerts, no raw content); Terraform + CI + pull
requests become the self-service path.

## Assumptions

- All sanctioned LLM and MCP traffic passes through the platform gateways; direct
  provider/tool egress from workloads is blocked at the network.
- Bedrock (Frankfurt) for regulated and customer-data workloads; the external provider only
  where Zero Data Retention is contractually approved.
- Datadog never receives raw prompt or response content.
- KYC-class workflows need stronger auditability than ordinary trace debugging.
- The sample spend data is directionally useful but has governance defects (missing team
  ownership, a blank cost value) the platform should treat as first-class violations.
- Per-team monthly budgets are an open question; figures used here are illustrative.

## 1. Cost governance

**Primitive:** a registered `route` (use case) mapping team → data class → default alias →
allowed fallbacks → budget policy → downgrade behaviour → observability tags. Teams call
aliases and routes, never raw provider model IDs. Every virtual key must carry `team`,
`owner`, `environment`, `cost_center`, `use_case`, `data_class`; unattributed requests are
rejected or sent to a tiny quarantine budget. The sample's ~$2k of un-owned personal-key
spend should be structurally impossible.

**Enforcement in layers:**

- Per request (hot path, in LiteLLM): max input/output tokens, timeout, streaming timeout,
  estimated maximum cost.
- Per key: RPM/TPM, concurrency, monthly budget, allowed aliases.
- Per team / per route: monthly budget, burst allowance, downgrade policy, escalation
  contact; stricter caps for experimental routes.

**Standard thresholds:** 75% warn the team · 90% warn team + platform, optionally shed
non-critical concurrency · 100% take the route's configured action (downgrade / throttle /
block / require approval).

**Downgrade is per workload, not global:**

| Workload | At 100% | Why |
| --- | --- | --- |
| AdvisorChat | explicit app signal, no silent swap | customer-facing financial answers |
| KYC | block + escalate | reproducibility and audit outweigh cost |
| DevAgent | throttle | write-capable tools; slowing is safer than swapping mid-session |
| DigestBot / Marketing | silent downgrade | batch / low-stakes, a cheaper model is fine |
| Research | throttle or downgrade | exploratory, shared key |

The hot path never depends on AWS billing data (too delayed); operational spend comes from
LiteLLM/Postgres, with AWS CUR / Cost Explorer used only for daily invoice reconciliation.

A first implementation of this tiered enforcement — a controller that reads accumulated
spend, evaluates the thresholds per workload, and emits LiteLLM-shaped policy intent — is
the **Task 2 component** in this repo. It also flags the sample's governance gaps
(un-attributed spend, blank cost, off-catalogue model use).

## 2. Data governance

Data movement, logging, retention, and replayability come from **route policy**, not
developer judgement at call time. A version-controlled **use-case registry** (YAML/HCL →
Postgres/LiteLLM) declares per route: team/owner, data class (`customer_data` /
`regulated_document` / `internal_confidential` / `public`), allowed providers + regions,
allowed aliases, logging mode, retention, reproducibility requirement, and tool access
level. Direct Bedrock invocation is denied except from the gateway role (IAM/SCP); the
external provider is reached only through a controlled egress proxy with a destination
allow-list.

**Logging modes:**

- `metadata_only` (default, incl. customer data): IDs, team, route, model, tokens, cost,
  latency, status, policy decision.
- `redacted_trace`: content after approved redaction; lower-risk internal work.
- `full_audit` (KYC and equivalent): template + model versions, retrieval context IDs, tool
  calls, approvals, output, input/output hashes, policy decisions.

**Retention — a property of the data class, subject to Legal/DPO sign-off:**

| Data | Retention | Store |
| --- | --- | --- |
| `metadata_only` records | 400 days, then aggregate-only | Postgres → S3 cold archive |
| `redacted_trace` content | 30–90 days | Langfuse |
| `full_audit` bundles | 7 years (KYC regulatory retention) | S3 Object Lock + KMS |
| Datadog derived metrics | 15 months | Datadog |
| Raw provider prompt/response | not retained | — (Bedrock no-logging / external ZDR) |

**Position:** retain the minimum that answers "what happened" for every request (metadata,
hashes, template/policy versions, retrieval doc IDs, decisions); keep raw content only where
a named regulatory or dispute need exists.

**Reproducibility:** a replay record ties together gateway request ID, Langfuse trace ID,
alias + resolved model + version, provider, prompt/system-prompt versions, policy version,
tool-manifest version, retrieval doc IDs + hashes, input/output hashes, policy decisions,
and approval IDs — enough to explain and re-run an interaction without storing everything
forever.

## 3. Agentic safety

LiteLLM proxies model calls, not tool calls, so agentic safety needs its own enforcement
point: a **dedicated MCP gateway**, sibling to LiteLLM, sharing its Postgres and policy
engine (OPA/Rego or Cedar). All agent tool/MCP traffic is routed through it; agents get
tool endpoints pointing at the gateway, and direct egress to tool servers is blocked. Every
call is classified, authorised, budgeted, and logged before execution.

**Classification** (registry default + argument-aware escalation):

- `read` — search wiki, inspect ticket, summarise.
- `write` — draft page, add comment, open PR, create task.
- `destructive` — delete/overwrite, close ticket, merge PR, deploy, change permissions, send
  external comms, modify customer/financial records.

For example `jira.update_issue(add_comment=…)` is write; `jira.update_issue(status=closed)`
escalates to destructive.

**Default posture:** new agents get read-only tools; write tools require route registration
and ownership; destructive actions require explicit approval, a small time-window budget,
and a manual reset after exhaustion; credentials are least-privilege and short-lived; tool
results re-entering the model are treated as untrusted input.

**Forensic log** per decision: agent/user/team/session, tool, normalised args (or hash),
target resource, classification, policy version, decision, approval ID, result, timestamp —
a trail that does not depend on teams remembering to log.

**Scaling:** the gateway is a stateless check with state in Postgres, so it scales
horizontally; new tools onboard via the same PR registry as models and start read-only; the
classification table and destructive budgets become the main tuning surface, and the
forensic log feeds the observability SLIs below.

## 4. Observability

Keep the separation: **Langfuse** for trace-level debugging and quality analysis; **Datadog**
for platform health, SLOs, alerts, and cost trends (derived metrics and sanitized tags
only). An OpenTelemetry collector normalises gateway/agent metrics into Datadog; CloudWatch
covers Bedrock/provider-level signals.

**Core SLIs:** gateway availability; provider success rate; end-to-end latency p50/p95/p99
by route; time to first token (streaming); error rate by category (auth / budget / policy /
provider 4xx / 5xx / timeout / rate limit); cost per team/route/model/provider; budget burn
rate; fallback + downgrade rate; policy block rate; agent approval + destructive-block rate;
quality proxies (empty-response, refusal, completion-length anomaly, groundedness, feedback
score).

**Investigate vs page:**

- **Investigate:** cost above baseline, fallback rate rising, a batch job getting more
  expensive, quality proxies drifting.
- **Page:** gateway unavailable; AdvisorChat breaches its latency/error SLO; KYC audit
  logging fails; policy engine fails open; direct provider access detected; raw content
  appears in Datadog.

**SLOs are route-class based:** AdvisorChat needs stricter latency/availability than
DigestBot; KYC needs audit-completeness and reproducibility SLOs, not just latency; Research
needs quota/spend control more than tight availability.

## 5. Self-service

Onboarding is a **pull request, not a ticket**. The platform ships Terraform modules and
YAML/HCL registry files; teams declare intent; CI enforces policy (`terraform validate`,
tflint, checkov, and Conftest/OPA over the plan and the YAML); plan/apply runs in a
protected environment and syncs LiteLLM via its admin API or generated config.

- **New team** declares: owner, cost centre, on-call contact, environments, budget, default
  model tier, allowed data classes, SLO tier, agentic tools yes/no.
- **New model** declares: provider model ID, alias, region, allowed data classes + teams,
  fallback, max tokens, timeout, budget cap, logging mode, eval result, monitor, owner.
- **New use case** declares: route name, team, data class, exposure, quality requirement,
  latency target, downgrade behaviour, retention mode, audit requirement, tool access.

**Approvals are risk-based:** platform for ordinary onboarding; + security for new egress
paths or write-capable tools; + Legal/DPO for new customer-data flows, retention exceptions,
or full-content logging; + finance for large budget increases.

**Inherited defaults:** metadata-only logging, no raw content in Datadog, conservative
budgets, approved aliases only, dashboards + SLO tags + standard alerts, least-privilege
access.

## 6. Model sourcing

**Recommendation: hybrid.** Keep managed frontier models for customer-facing and regulated
workloads; move batch/internal workloads to Bedrock managed open-weight models where evals
pass; **defer self-hosting** until a single model sustains enough traffic to amortise GPU
and on-call cost. LiteLLM aliases hide the provider choice from teams throughout.

**Assumptions for the math:**

- Managed frontier (Sonnet class, Bedrock Frankfurt): ~$3 / 1M input, ~$15 / 1M output.
- Bedrock managed open-weight (70B class): ~$0.75 / 1M blended.
- Self-host node: one reserved GPU instance (~$3.5/hr, 1-yr) ≈ $2,550/month, ~6.5B
  tokens/month at full utilisation; assume 2 nodes for HA + ~0.3–0.5 FTE of GPU on-call.
- Observed (30 days): DevAgent ~$20.1k / 2.38B tokens (100% external premium), AdvisorChat
  ~$9.2k, KYC ~$2.8k, DigestBot ~$2.0k, Research ~$0.3k, Marketing ~$8, un-attributed
  ~$2.0k. Platform ≈ $36.5k / 30 days ≈ $440k/year.

**Worked example — DevAgent** (largest driver; ~1.24B input + 1.14B output tokens/month):

| Option | ~Monthly cost | Note |
| --- | --- | --- |
| Stay on external premium | ~$20k | blended ~$8.5 / 1M |
| Route to managed Sonnet | ~$20.8k | **no saving** — output-heavy, Sonnet output is $15 / 1M |
| Managed open-weight (~$0.75 / 1M) | ~$1.8k | needs eval parity; quality risk on multi-step tool use |
| Self-host (2 reserved nodes) | ~$5.1k + ~0.4 FTE | workload uses ~37% of one node — poor utilisation |

Switching between *frontier* providers barely moves an output-heavy bill; only a cheaper
*model class* or self-hosting does — and self-hosting is worst here because the workload
cannot keep a GPU busy. Self-hosting only overtakes managed open-weight when one model
sustains ~13B tokens/month (~430M/day, ~5× current DevAgent) around the clock, and only once
Security/Legal accept the runtime and the platform can staff GPU on-call.

**First move:** pilot a managed open-weight model on **DigestBot** — batch, cost-sensitive,
downgrade-tolerant. Direct saving is small (~$0.5–1k/month); the point is standing up the
eval/qualification pipeline every later sourcing decision depends on.

**What would change the recommendation:** sustained volume on one/two models past the
break-even; open-weight quality clearing route eval thresholds (esp. agentic tool use);
Security/Legal approving an open-weight runtime for a data class; a commitment to staff GPU
on-call; managed-provider price rises or Frankfurt capacity/latency problems.

## 7. Six-month direction

1. **Foundations** — route registry, mandatory metadata, aliases, ownership enforcement,
   direct-provider blocking.
2. **Cost + operational visibility** — budget policies, the spend controller, Datadog
   SLIs/SLOs, burn alerts, daily invoice reconciliation.
3. **Data governance** — logging modes, retention rules, audit bundles (KMS / S3 Object
   Lock), DPO-approved replay policy, trace access controls.
4. **Agentic safety** — MCP gateway + tool registry, classification, policy engine,
   destructive-action budgets, approval flow, forensic log.
5. **Self-service + sourcing** — Terraform modules, CI policy validation, onboarding
   templates, the DigestBot open-weight pilot.

**Dependencies:** (2) and (3) need the route registry + metadata from (1); (4) reuses the
policy-engine and forensic-logging patterns from (3); (5) templates whatever (1)–(4)
standardised, so it ships last. The eval pipeline has no upstream dependency and runs in
parallel with (2).

**Deferred:** self-hosted serving; an org-wide prompt-quality platform; full-content logging
everywhere; replacing LiteLLM; multi-provider cost arbitrage before governance is solid.

## 8. Rejected alternatives

- **Replace LiteLLM now** — it already provides the right extension points for routing,
  spend, auth, budgets, callbacks.
- **AWS Budgets for hot-path control** — billing data is hours late; useful only for
  reconciliation.
- **Log all prompts/responses globally** — unnecessary privacy/regulatory risk;
  metadata-only default with route-specific audit exceptions is better.
- **Let teams pick raw model IDs** — breaks attribution and routing governance.
- **Agentic safety inside each app** — doesn't scale; enforce shared defaults at the tool
  gateway.
- **Self-host in the first six months** — premature before pilots, evals, and an
  operating-model decision.

## 9. Open questions

- Which workloads may downgrade automatically, and which must fail closed?
- Exact retention by product, data class, and jurisdiction?
- Does reconstructing an interaction require raw content, or are hashes + templates +
  retrieval IDs + policy records enough?
- Who approves destructive agent actions, and which systems may agents write to in
  production?
- What monthly spend envelope should each team get?
- Are Bedrock managed open-weight models acceptable for customer data in an approved EU
  region?
- Who owns incidents when the gateway is healthy but a model/provider degrades an
  application?
- What evidence does Legal/DPO need for audits, disputes, and regulatory challenges?

## Conclusion

Make the safe path the easy path: LiteLLM stays the gateway, and the platform adds a
declarative policy layer for routes, teams, data classes, budgets, models, tools, logging,
and observability — practical for six teams today, and a foundation for more teams, agents,
and model sources over the next six months.
