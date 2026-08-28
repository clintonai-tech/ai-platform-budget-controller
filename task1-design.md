# Task 1: AI Platform Design

## Executive Summary

Scalable Fictional should keep LiteLLM as the mandatory LLM gateway and add a small platform control plane around it. The goal is not only convenient model access, but governed access: every request should have an owner, data classification, allowed model route, budget policy, logging mode, and operational signal.

The target design uses the current stack deliberately:

- LiteLLM on ECS Fargate remains the enforcement point for authentication, model routing, rate limits, budgets, and callbacks.
- Postgres remains the source of truth for keys, teams, spend, policies, and audit references.
- Langfuse remains the trace-level debugging system for prompts, spans, model calls, and evaluations.
- Datadog becomes the platform operational view for SLIs, SLOs, alerting, and cost/reliability trends, without raw prompt or response content.
- Terraform, CI policy checks, and pull requests become the self-service path for onboarding teams, models, and use cases.
- AWS-native controls such as IAM, KMS, Secrets Manager, CloudTrail, CloudWatch, S3 Object Lock, Macie, and Bedrock Guardrails provide the regulated infrastructure baseline.

My recommended direction is a policy-driven AI platform: teams call stable model aliases and registered routes, while the platform decides which providers, logging modes, budgets, and safety controls apply.

## Assumptions

- All sanctioned LLM calls must pass through LiteLLM. Direct provider access from application workloads should be blocked.
- Bedrock Anthropic models in Frankfurt are preferred for regulated and customer-data workloads.
- The external provider may be used where Zero Data Retention is contractually approved.
- Datadog must never receive raw prompt or response content.
- Some workflows, especially KYC, need stronger auditability than ordinary trace debugging.
- The spend data is directionally useful but not perfectly clean. It includes missing team ownership and a blank cost value, which the platform should treat as governance issues.

## 1. Cost Governance

The platform should prevent waste before a request is sent, surface spend close to real time, and make model choice safer by default.

The core primitive should be a `use_case` or `route` registered in the platform control plane. A route maps to a team, data class, default model alias, allowed fallback models, budget policy, downgrade behavior, and observability tags. Teams should not call raw provider model IDs directly.

Recommended technologies:

- LiteLLM virtual keys for attribution and access control.
- LiteLLM teams for budget boundaries.
- LiteLLM model aliases for stable application-facing model names.
- LiteLLM budgets, `budget_duration`, RPM/TPM limits, and max parallel requests for hot-path protection.
- LiteLLM callbacks for spend and usage events.
- Postgres for team/key/route policy and near-real-time spend state.
- Datadog custom metrics for dashboards and alerts.
- AWS Cost Explorer, Cost and Usage Reports, and cost allocation tags for finance reconciliation.

Each virtual key should be required to include `team`, `owner`, `environment`, `cost_center`, `use_case`, and `data_class`. Requests without valid ownership should be rejected in production or routed through a tiny quarantine budget. The sample spend file shows why this matters: several rows have missing team ownership, including high-cost personal-key usage. That should become structurally impossible.

Spend enforcement should happen in layers:

- Per-request guardrails: max input tokens, max output tokens, timeout, streaming timeout, and estimated maximum cost.
- Per-key limits: RPM, TPM, concurrent requests, monthly budget, and allowed model aliases.
- Per-team limits: monthly budget, burst allowance, downgrade policy, and escalation contacts.
- Per-route limits: stricter caps for experimental workloads and larger caps for approved production workloads.

Budget thresholds should be standardized:

- At 75%, emit warning metrics and notify the owning team.
- At 90%, notify team and platform, and optionally reduce concurrency for non-critical workloads.
- At 100%, enforce the route-specific action: downgrade, throttle, block, or require approval.

Downgrade behavior must depend on workload:

- AdvisorChat: no silent downgrade. Customer-facing financial Q&A should either use an approved equivalent model or return an explicit application signal.
- KYC: no silent downgrade. Auditability and reproducibility matter more than cost.
- DigestBot: automatic downgrade is acceptable because it is batch and cost-sensitive.
- DevAgent: downgrade read-only assistance, but require stronger controls for write-capable tool sessions.
- Research: throttle or downgrade after threshold.
- Marketing: cheapest approved model by default.

The hot path should not depend on AWS billing data because billing signals are delayed. LiteLLM/Postgres should provide operational spend. AWS CUR and Cost Explorer should reconcile provider and infrastructure bills daily.

A first implementation of the tiered enforcement described above (a controller that reads accumulated spend, evaluates the 75/90/100% thresholds per workload, applies workload-specific downgrade rules, and emits LiteLLM-shaped policy intent) is included in this repository as the Task 2 component. It also surfaces the governance gaps visible in the sample data: un-attributed personal-key spend, a blank cost value, and teams calling models outside their registered allow-list.

## 2. Data Governance

Data governance should be structural: data movement, logging, retention, and replayability should come from route policy, not developer judgment at call time.

The main design primitive is a Use Case Registry stored as version-controlled YAML/HCL and materialized into Postgres/LiteLLM. Each use case declares:

- Team and accountable owner.
- Product or internal system.
- Data class, such as `customer_data`, `regulated_document`, `internal_confidential`, or `public`.
- Allowed providers and regions.
- Allowed model aliases.
- Logging mode.
- Retention period.
- Reproducibility requirement.
- Tool/MCP access level.

Recommended technologies:

- LiteLLM aliases and access controls to prevent teams from choosing disallowed raw models.
- AWS IAM and possibly AWS Organizations SCPs to deny direct Bedrock invocation except from the gateway role.
- VPC egress controls, NAT allow lists, or a controlled egress proxy for external provider APIs.
- AWS Secrets Manager for provider credentials.
- AWS KMS for encrypting Postgres, S3 audit storage, ClickHouse, backups, and logs.
- S3 Object Lock for immutable audit bundles where required.
- Amazon Macie for detecting unexpected sensitive data in S3 log or audit buckets.
- CloudTrail for AWS API audit trails.
- Bedrock model invocation logging only for tightly controlled audit routes, not globally.
- Presidio or a similar PII detection/redaction library where redacted traces are required.

Logging should have explicit modes:

- `metadata_only`: request ID, team, route, model alias, provider, token counts, cost, latency, status, and policy decisions. This should be the default for customer data.
- `redacted_trace`: prompt/response content after approved redaction. Useful for lower-risk internal workflows.
- `full_audit`: prompt template version, model version, retrieval context IDs, tool calls, approval records, final output, hashes of sensitive inputs/outputs, and policy decisions. This should be reserved for KYC-like workflows.

Retention should be a property of the route's data class, not a per-team choice. Proposed defaults, subject to Legal/DPO confirmation:

| Data | Retention | Store |
| --- | --- | --- |
| `metadata_only` records (request ID, team, route, model, token counts, cost, latency, status, policy decision) | 400 days, then aggregate-only | Postgres, then cold archive in S3 |
| `redacted_trace` content | 30 to 90 days | Langfuse |
| `full_audit` bundles (KYC and equivalent) | 7 years, matching regulatory retention for KYC records | S3 with Object Lock and KMS |
| Datadog derived metrics | 15 months (Datadog default) | Datadog |
| Un-redacted provider prompt/response | not retained by the platform; relies on Bedrock no-logging and the external provider ZDR term | not stored |

The position is to retain the minimum that answers "what happened" (metadata, input/output hashes, template and policy versions, retrieval document IDs, policy decisions) for every request, and to keep raw content only where a named regulatory or dispute-resolution need exists.

Datadog should receive only derived metrics and sanitized tags. Langfuse may hold richer traces where policy permits, but access should be role-based and retention-bound.

For reconstructing past interactions, the platform should store enough provenance to explain what happened without logging everything forever. A replay/audit record should include gateway request ID, Langfuse trace ID, model alias and resolved model, provider, prompt template version, system prompt version, policy version, tool manifest version, retrieval document IDs and hashes, input/output hashes, policy decisions, and approval IDs.

## 3. Agentic Safety

Agentic safety should sit at the platform boundary between agents and tools. Application teams should inherit safe defaults instead of building separate safety layers.

LiteLLM proxies model calls, not MCP or tool calls, so agentic safety needs its own enforcement point. The platform should run a dedicated MCP gateway as a sibling service to LiteLLM, sharing the same Postgres and policy engine. All agent tool and MCP traffic is configured to route through it; agents are handed tool endpoints that point at the gateway, not at the tool servers, and direct egress from agent workloads to tool servers is blocked the same way direct provider access is. Every tool call is classified, authorized, budgeted, and logged at this gateway before execution.

Recommended technologies:

- LiteLLM gateway integration for agent traffic and metadata propagation.
- MCP server registry for tool definitions, owners, scopes, and risk levels.
- Open Policy Agent with Rego, or AWS Verified Permissions with Cedar, for deterministic policy decisions.
- Postgres for tool registry, session state, destructive-action counters, approval records, and forensic logs.
- AWS Step Functions, Slack, Jira, or an internal approval service for human approval workflows.
- Short-lived scoped credentials, issued per agent session and tool scope.
- CloudTrail for AWS-side activity and immutable S3 storage for high-risk decision logs.

Tool actions should be classified into:

- `read`: search wiki, inspect ticket, fetch repository metadata, summarize a document.
- `write`: create draft wiki page, add ticket comment, open pull request, create internal task.
- `destructive`: delete, overwrite, close ticket, merge pull request, trigger deployment, change permissions, send external communication, or modify customer/financial records.

Classification should be both static and argument-aware. For example, `jira.update_issue(add_comment=...)` is a write action, while `jira.update_issue(status=closed)` is potentially destructive. The registry should provide the default impact level; the policy engine should inspect arguments for escalation.

Default posture:

- New agents get read-only tools by default.
- Write tools require route registration and team ownership.
- Destructive actions require explicit approval, a small time-window budget, and a reset workflow after exhaustion.
- Tool credentials are least privilege and short lived.
- Tool results that re-enter the model are treated as untrusted input.

Each decision log should include agent ID, user ID, team, session ID, tool name, normalized arguments or argument hash, target resource, classification, policy version, decision, approval ID, execution result, and timestamp. This gives the platform a forensic trail without relying on application teams to remember to log safely.

Scaling: the gateway is a stateless policy check with all state in Postgres, so it scales horizontally as agent traffic grows. New MCP servers and tools onboard through the same pull-request registry as models (owner, scopes, default impact level), and every new tool starts read-only until a route registration promotes it. As agents become the dominant traffic shape, the classification table and destructive-action budgets become the main tuning surface, and the same forensic log feeds the observability SLIs below (approval rate, destructive-action block rate).

Bedrock Guardrails can help with content controls, sensitive information filters, and grounding checks for model input/output. They should complement tool authorization, not replace it.

## 4. Observability

LiteLLM and Langfuse already provide useful raw telemetry. What is missing is a platform-owned operational view: consistent SLIs, SLOs, and alerts across teams, routes, models, and providers.

The key separation is:

- Langfuse: trace-level debugging, prompt iteration, spans, model calls, evaluations, and quality analysis.
- Datadog: platform health, SLOs, alerts, cost trends, and operational dashboards.
- Postgres/ClickHouse: source stores for gateway metadata, spend, and trace data.

Recommended technologies:

- LiteLLM callbacks or logs for request usage, routing, cost, and errors.
- Langfuse for traces and quality signals.
- OpenTelemetry Collector for metric normalization.
- Datadog metrics, monitors, SLOs, dashboards, and incident routing.
- CloudWatch metrics for Bedrock/provider-level AWS signals.
- CloudWatch Logs subscription filters or Firehose for selected operational log pipelines.

Core SLIs:

- Gateway availability: successful valid requests divided by total valid requests.
- Provider success rate: successful provider attempts divided by provider attempts.
- End-to-end latency: p50, p95, p99 by route.
- Time to first token for streaming use cases.
- Error rate by category: auth, budget, policy, provider 4xx, provider 5xx, timeout, rate limit.
- Cost per team, route, model alias, and provider.
- Budget burn rate.
- Fallback and downgrade rate.
- Policy block rate.
- Agent approval and destructive-action block rate.
- Quality proxies: empty response rate, refusal rate, completion length anomaly, groundedness score where available, and user feedback score where available.

Alerting should separate investigation from paging:

- Investigate when cost rises above baseline, fallback rate increases, a batch workload becomes more expensive, or quality proxies drift.
- Page when the gateway is unavailable, AdvisorChat breaches latency/error SLOs, KYC audit logging fails, the policy engine fails open, direct provider access is detected, or raw prompt/response content appears in Datadog.

SLOs should be route-class based. AdvisorChat needs stricter latency and availability SLOs than DigestBot. KYC needs audit completeness and reproducibility SLOs, not only latency. Research needs quota and spend controls more than tight availability.

## 5. Self Service

Onboarding should be a pull request, not a ticket. The platform team should provide templates and modules; teams should declare intent; CI should enforce policy.

Recommended technologies:

- Terraform modules for Bedrock model access, IAM, Secrets Manager, CloudWatch alarms, Datadog monitors, ECS config, and budget alarms.
- YAML/HCL registry files for teams, model aliases, use cases, budgets, data classes, and MCP tools.
- GitHub Actions, GitLab CI, or similar CI for validation.
- `terraform fmt`, `terraform validate`, TFLint, Checkov or tfsec for infrastructure checks.
- Conftest with OPA/Rego for governance checks over YAML and Terraform plans.
- Atlantis, Spacelift, Terraform Cloud, or protected CI environments for plan/apply.
- LiteLLM admin API or generated config for syncing teams, keys, model aliases, and budgets.

A new team PR should define owner, cost center, Slack/on-call contact, environments, budget, default model tier, allowed data classes, SLO tier, and whether agentic tools are allowed.

A new model PR should define provider model ID, model alias, region, allowed data classes, allowed teams, fallback, max tokens, timeout, budget cap, logging mode, eval result, Datadog monitor, and owner.

A new use-case PR should define route name, team, data class, customer/internal exposure, model quality requirement, latency target, downgrade behavior, retention mode, audit requirement, and tool access.

Approval should be risk-based:

- Platform approval for ordinary team/model/use-case onboarding.
- Security approval for new egress paths, write-capable tools, or destructive agent actions.
- Legal/DPO approval for new customer-data flows, retention exceptions, or full-content logging.
- Finance/leadership approval for large budget increases.

The paved road should inherit defaults: metadata-only logging, no raw content in Datadog, conservative budgets, allowed model aliases only, dashboards, SLO tags, standard alerts, and least-privilege access.

## 6. Model Sourcing

The recommended sourcing strategy is hybrid: keep managed providers for critical workloads, add Bedrock managed open-weight models for selected internal and batch use cases, and defer self-hosting until utilization and economics justify the operational burden.

Recommended technologies:

- Amazon Bedrock for managed Anthropic and managed open-weight models.
- Bedrock inference profiles for usage tracking and cost allocation.
- LiteLLM aliases to hide provider choice from application teams.
- Bedrock Guardrails for supported input/output controls.
- Langfuse datasets and scores for prompt/model regression testing.
- Bedrock model evaluation or custom eval pipelines for model qualification.
- vLLM, Text Generation Inference, NVIDIA Triton, EKS, Karpenter, and GPU nodes only for a later self-hosting pilot.

Workload-specific sourcing:

- AdvisorChat: use managed premium models in approved regions. Reliability, latency, and quality matter more than lowest unit cost.
- KYC: use managed models with strong auditability, regional controls, and reproducibility. Avoid experimental sourcing.
- DigestBot: best candidate for Bedrock managed open-weight models or cheaper managed models because it is batch and cost-sensitive.
- DevAgent: largest driver and output-heavy, so a frontier-to-frontier switch saves little (see the worked example below). Keep a managed model for planning and write-capable tool steps; qualify a managed open-weight model for summarisation and read-only helper steps, where most of the tokens are.
- Research: provide a broad but quota-controlled catalog.
- Marketing: use low-cost approved models by default.

### Assumptions for the math

- Managed frontier (Anthropic Sonnet class, Bedrock Frankfurt): about $3 per 1M input tokens, $15 per 1M output.
- Bedrock managed open-weight (Llama or Mistral 70B class): about $0.75 per 1M blended.
- Self-hosted serving node: one reserved GPU instance able to serve a quantised 70B at roughly 2,500 output tokens/sec sustained, about $3.5/hr on a one-year commit, so about $2,550 per month per node and about 6.5B tokens/month at full utilisation. Assume two nodes for redundancy and peak headroom, plus roughly 0.3 to 0.5 FTE of platform on-call to own inference reliability.
- Observed spend (`spend_30d.csv`, 30 days): DevAgent about $20.1k on 2.38B tokens (100% on the external premium model), AdvisorChat about $9.2k on 1.86B, KYC about $2.8k, DigestBot about $2.0k on 1.41B, Research about $0.3k, Marketing about $8, un-attributed personal keys about $2.0k. Platform total about $36.5k per 30 days, roughly $440k per year.

### Worked example: DevAgent, the largest driver

DevAgent runs entirely on the external premium model: about 1.24B input and 1.14B output tokens per month, roughly 79M tokens/day, an average of about 915 tokens/sec.

| Option | Estimated monthly cost | Note |
| --- | --- | --- |
| Stay on external premium | ~$20k | observed; blended about $8.5 per 1M |
| Route to managed Anthropic Sonnet | ~$20.8k | no saving: the workload is output-heavy and Sonnet output is $15 per 1M |
| Bedrock managed open-weight (~$0.75 per 1M) | ~$1.8k | needs eval parity; quality risk on multi-step tool use |
| Self-host, two reserved nodes | ~$5.1k plus ~0.4 FTE | 79M tokens/day is about 37% of one node, so utilisation is poor |

The instructive result is that switching frontier providers barely moves DevAgent's bill, because it generates almost as many output tokens as input. Only a cheaper model class (managed open-weight) or self-hosting changes the economics, and self-hosting is the worst of those because the workload cannot keep a GPU node busy.

Self-hosting only overtakes managed open-weight when a single model sustains at least two nodes' worth of throughput around the clock, on these assumptions roughly 13B tokens/month for one model (about 430M tokens/day), and only once Security and Legal accept the runtime and the platform can staff GPU on-call. Nothing in today's mix is within 5x of that, and the entire platform's annual spend (~$440k) is less than the fully loaded cost of the extra headcount a self-hosting programme needs.

### Batch workloads

DigestBot (about $2.0k/month on Anthropic Haiku, blended about $1.4 per 1M) is the right first open-weight pilot: batch, cost-sensitive, not customer-facing, and already flagged as tolerant of a silent downgrade. The direct saving is modest, perhaps $0.5k to $1k per month; the real value is standing up the eval and qualification pipeline (Langfuse datasets and scores, regression thresholds) that every later sourcing decision depends on.

### What would change the recommendation

- Sustained volume on one or two models rising past the self-host break-even above.
- Open-weight quality clearing route-specific eval thresholds, especially for agentic tool use.
- Security and Legal approving an open-weight model and its runtime for a given data class.
- A commitment to staff GPU serving and inference on-call.
- Managed-provider price increases, or capacity or latency problems in the Frankfurt region.

## 7. Six-Month Direction

First, ship the foundations: route registry, mandatory metadata, model aliases, ownership enforcement, and direct-provider blocking. Without this, cost, data governance, and observability remain inconsistent.

Second, ship cost and operational visibility: LiteLLM budget policies, spend controller, Datadog dashboards, SLIs, SLOs, budget burn alerts, and daily AWS/provider reconciliation.

Third, ship data governance: logging modes, retention rules, audit bundles, KMS/S3 Object Lock where required, DPO-approved replay policy, and trace access controls.

Fourth, ship agentic safety: MCP/tool registry, action classification, policy engine, destructive-action budgets, approval flow, and forensic logs.

Fifth, ship self-service modules and model sourcing expansion: Terraform modules, CI policy validation, model onboarding templates, and a managed open-weight pilot for DigestBot or Marketing.

Dependencies: cost and operational visibility (2) and data governance (3) both depend on the route registry and mandatory metadata from (1), because attribution and logging mode are route properties. Agentic safety (4) reuses the policy-engine and forensic-logging patterns established in (3). Self-service modules (5) template whatever (1) through (4) have already standardised, so they ship last. The open-weight eval pipeline is the one piece with no upstream dependency and can run in parallel with (2).

Explicit deferrals:

- Full self-hosted model serving.
- Organization-wide prompt quality platform.
- Full-content logging for all workflows.
- Replacing LiteLLM.
- Complex multi-provider arbitrage before basic governance is solid.

## 8. Rejected Alternatives

Replacing LiteLLM immediately is not justified. The current gateway already provides the right extension point for routing, spend, auth, budgets, and callbacks.

Using AWS Budgets for hot-path control is too slow. AWS billing data is useful for reconciliation, not for stopping a runaway request.

Logging all prompts and responses globally creates unnecessary privacy and regulatory risk. The better default is metadata-only logging with route-specific audit exceptions.

Letting teams choose raw model IDs breaks governance. Teams should call approved aliases and routes.

Putting agentic safety inside every application team does not scale. The platform should enforce shared defaults at the tool gateway.

Self-hosting models in the first six months is premature. It may become attractive later, but only after managed open-weight pilots, evals, cost analysis, and operating-model decisions.

## 9. Open Questions

- Which workloads may downgrade automatically, and which must fail closed?
- What exact retention periods apply by product, data class, and jurisdiction?
- Does reconstructing a past interaction require raw content, or are hashes, templates, retrieved document IDs, and policy records sufficient?
- Who approves destructive agent actions?
- Which systems may agents write to in production?
- What monthly AI spend envelope should each team receive?
- Are Bedrock managed open-weight models acceptable for customer data if hosted in an approved EU region?
- Which team owns incidents when the gateway is healthy but a model/provider causes application degradation?
- What evidence does Legal/DPO need for audits, customer disputes, and regulatory challenges?

## Conclusion

The target platform should make the safe path the easy path. LiteLLM remains the gateway, but the platform adds a declarative policy layer for routes, teams, data classes, budgets, models, tools, logging, and observability. This approach keeps the system practical for six teams today while creating a scalable foundation for more teams, more agents, and more model sources over the next six months.
