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

Datadog should receive only derived metrics and sanitized tags. Langfuse may hold richer traces where policy permits, but access should be role-based and retention-bound.

For reconstructing past interactions, the platform should store enough provenance to explain what happened without logging everything forever. A replay/audit record should include gateway request ID, Langfuse trace ID, model alias and resolved model, provider, prompt template version, system prompt version, policy version, tool manifest version, retrieval document IDs and hashes, input/output hashes, policy decisions, and approval IDs.

## 3. Agentic Safety

Agentic safety should sit at the platform boundary between agents and tools. Application teams should inherit safe defaults instead of building separate safety layers.

The platform should introduce an Agent Tool Gateway or policy middleware on the MCP/tool-call path. Every tool call should be classified, authorized, budgeted, and logged before execution.

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
- DevAgent: use stronger managed models for planning/tool use, cheaper models for summarization or read-only helper steps.
- Research: provide a broad but quota-controlled catalog.
- Marketing: use low-cost approved models by default.

The spend sample suggests DevAgent is the largest cost driver at about $20k over 30 days, followed by AdvisorChat at about $9.2k. That is enough to justify routing, prompt optimization, caching, and managed open-weight experiments. It is not automatically enough to justify self-hosting. Self-hosting only wins when GPU utilization is high, workloads are predictable, compliance accepts the model/runtime path, and the platform can own inference reliability.

What would change the recommendation:

- Sustained monthly spend grows enough that managed provider margin dominates total cost.
- A small set of models handles most traffic with predictable utilization.
- Open-weight quality meets route-specific eval thresholds.
- Security and Legal approve the data path.
- The platform can staff GPU operations and model serving on-call.

## 7. Six-Month Direction

First, ship the foundations: route registry, mandatory metadata, model aliases, ownership enforcement, and direct-provider blocking. Without this, cost, data governance, and observability remain inconsistent.

Second, ship cost and operational visibility: LiteLLM budget policies, spend controller, Datadog dashboards, SLIs, SLOs, budget burn alerts, and daily AWS/provider reconciliation.

Third, ship data governance: logging modes, retention rules, audit bundles, KMS/S3 Object Lock where required, DPO-approved replay policy, and trace access controls.

Fourth, ship agentic safety: MCP/tool registry, action classification, policy engine, destructive-action budgets, approval flow, and forensic logs.

Fifth, ship self-service modules and model sourcing expansion: Terraform modules, CI policy validation, model onboarding templates, and a managed open-weight pilot for DigestBot or Marketing.

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
