import { useState } from "react";

type Fields = Record<string, unknown>;
type Ref = { id: string; revision: number };
type Task = {
  id: string;
  revision: number;
  role: string;
  purpose: string | null;
  readiness: string;
  complexity: string;
  risk: string;
  paths: string[];
  domains: string[];
  capabilities: string[];
  tools: string[];
  context: number;
  duration: number;
  dependencies: string[];
  acceptance: string[];
  required: boolean;
};
export type RoutingScope = {
  digest: string;
  planDigest: string;
  configurationDigest: string;
  authorizationDigest: string;
  readPaths: string[];
  writePaths: string[];
  checks: string[];
  delivery: string;
  targetBranch: string;
  budget: string;
  budgetCeiling: {
    limits: [string, string | null][];
    attempts: number | null;
    seconds: number | null;
  };
  channels: {
    id: string;
    account: string;
    provider: string;
    billing: string;
    destination: string;
  }[];
  profiles: {
    ref: Ref;
    model: string;
    channel: string;
    account: string;
    runtime: string;
    billing: string;
  }[];
  destinations: string[];
  tools: { id: string; permissions: string[] }[];
  capabilities: string[];
  currencyLimits: [string, string][];
  attemptSeconds: number;
  repairRounds: number;
  stages: {
    id: string;
    normalAllowed: boolean;
    normal: { group: string; profiles: Ref[] }[];
    quality: { index: number; group: string; profiles: Ref[] }[];
  }[];
  tasks: Task[];
  policy: Ref & {
    owner: string;
    projectId: string;
    digest: string;
    toolPolicy: Ref;
    riskPolicy: Ref;
    contextPolicy: Ref;
    risk: [string, string][];
    floors: { path: string; minimum: string }[];
    reservedOutput: number;
    maxContext: number;
  };
  rulebook: Ref & { digest: string };
};

function record(value: unknown): Fields {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error();
  return value as Fields;
}
function list(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new Error();
  return value;
}
function text(value: unknown): string {
  if (typeof value !== "string" || value.length === 0) throw new Error();
  return value;
}
function names(value: unknown): string[] {
  return list(value).map(text);
}
function number(value: unknown, minimum = 0): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < minimum
  )
    throw new Error();
  return value;
}
function flag(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error();
  return value;
}
function hash(value: unknown): string {
  const result = text(value);
  if (!/^[0-9a-f]{64}$/.test(result)) throw new Error();
  return result;
}
function ref(value: unknown): Ref {
  const item = record(value);
  return { id: text(item.id), revision: number(item.revision, 1) };
}
function same(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}
function optionalName(value: unknown, missing: string): string {
  return value === null || value === undefined ? missing : text(value);
}
function billing(value: unknown): string {
  if (value === "subscription_only") return "订阅";
  if (value === "api_cash") return "现金 API";
  if (value === null || value === undefined) return "未声明计费路径";
  throw new Error();
}

/** Read the authenticated server's frozen material, without constructing grants or qualification. */
export function readRoutingScope(
  runValue: unknown,
  planValue: unknown,
  projectId: string,
): RoutingScope | null {
  try {
    const run = record(runValue),
      plan = record(planValue);
    if (
      run.schema_version !== "karajan.run-planning.v2" ||
      run.project_id !== projectId
    )
      throw new Error();
    const document = record(plan.plan),
      auth = record(document.authorization);
    const binding = record(plan.routing_binding),
      policy = record(run.execution_policy_snapshot);
    const fixedPolicy = record(binding.execution_policy),
      rulebook = record(binding.rulebook);
    const snapshot = record(run.configuration_snapshot),
      config = record(snapshot.configuration);
    const resources = record(config.resources);
    if (
      binding.schema_version !== "karajan.approved-routing-binding.v1" ||
      binding.activation_allowed !== false ||
      policy.schema_version !== "karajan.execution-policy.v1" ||
      policy.project_id !== projectId ||
      policy.registered_by !== run.owner ||
      fixedPolicy.project_id !== projectId ||
      fixedPolicy.registered_by !== run.owner ||
      fixedPolicy.id !== policy.id ||
      fixedPolicy.revision !== policy.revision ||
      hash(fixedPolicy.digest) !== hash(policy.digest) ||
      hash(policy.configuration_digest) !== hash(snapshot.digest) ||
      hash(binding.configuration_digest) !== hash(snapshot.digest) ||
      hash(plan.configuration_digest) !== hash(snapshot.digest)
    )
      throw new Error();
    hash(plan.plan_digest);
    hash(plan.authorization_digest);
    hash(binding.authorization_ceiling_digest);
    const digest = hash(plan.routing_digest);
    number(plan.term, 1);
    number(plan.plan_revision, 1);
    const refs = list(auth.profile_refs).map(ref);
    const approvedRefs = (value: unknown) =>
      list(value)
        .map(ref)
        .map((item) => {
          if (
            !refs.some(
              (allowed) =>
                allowed.id === item.id && allowed.revision === item.revision,
            )
          )
            throw new Error();
          return item;
        });
    const catalogProfiles = list(resources.profiles).map(record);
    const profiles = refs.map((reference) => {
      const item = catalogProfiles.find(
        (entry) =>
          entry.id === reference.id && entry.revision === reference.revision,
      );
      if (!item) throw new Error();
      if (item.profile === null || item.profile === undefined)
        return {
          ref: reference,
          model: "未配置模型",
          channel: "未配置通道",
          account: "未配置账户",
          runtime: "未配置执行器",
          billing: "未声明计费路径",
        };
      const profile = record(item.profile),
        source = record(profile.binding);
      if (
        profile.id !== reference.id ||
        profile.revision !== reference.revision
      )
        throw new Error();
      return {
        ref: reference,
        model: text(source.model_id),
        channel: text(source.channel_id),
        account: text(source.account_id),
        runtime: `${text(source.runtime_kind)} ${text(source.runtime_version)}`,
        billing: billing(source.billing_path),
      };
    });
    const accounts = list(resources.accounts).map(record),
      catalogChannels = list(resources.channels).map(record);
    const destinations = names(auth.data_destinations),
      mappings = record(policy.channel_destinations);
    const channels = names(auth.channel_ids).map((id) => {
      const source = catalogChannels.find((item) => item.id === id);
      if (!source) throw new Error();
      const account = accounts.find((item) => item.id === source.account_id);
      const destination = text(mappings[id]);
      if (!destinations.includes(destination)) throw new Error();
      return {
        id,
        account: optionalName(source.account_id, "未声明账户"),
        provider: optionalName(account?.provider_id, "未声明服务商"),
        billing: billing(source.billing_path),
        destination,
      };
    });
    const permissions = record(record(policy.tool_policy).tool_permissions);
    const tools = names(auth.tools).map((id) => ({
      id,
      permissions: names(permissions[id]),
    }));
    if (auth.min_isolation !== "tool_sandboxed") throw new Error();
    const currencyLimits = Object.entries(record(auth.currency_limits)).map(
      ([currency, value]): [string, string] => {
        const amount = text(value);
        if (!/^[A-Z]{3}$/.test(currency)) throw new Error();
        // The authenticated domain response owns decimal validation. Preserve its
        // exact string, including exponent notation, without a Number conversion.
        return [currency, amount];
      },
    );
    const stagePermissions = record(auth.stage_permissions),
      grants = record(binding.stage_grants);
    if (!same(Object.keys(stagePermissions).sort(), Object.keys(grants).sort()))
      throw new Error();
    const stages = Object.entries(stagePermissions).map(([id, value]) => {
      const permission = record(value),
        grant = record(grants[id]);
      const normalAllowed = flag(permission.normal);
      const normal = Object.entries(record(grant.normal)).map(
        ([group, members]) => ({ group, profiles: approvedRefs(members) }),
      );
      const quality = list(grant.quality).map((value) => {
        const item = record(value);
        return {
          index: number(item.index),
          group: text(item.group),
          profiles: approvedRefs(item.profiles),
        };
      });
      const indices = list(permission.quality_indices).map((value) =>
        number(value),
      );
      if (
        (!normalAllowed && normal.length !== 0) ||
        !same([...indices].sort(), quality.map((item) => item.index).sort())
      )
        throw new Error();
      return { id, normalAllowed, normal, quality };
    });
    const requirements = record(binding.task_requirements),
      tasks = list(document.tasks).map(record);
    if (
      !same(
        Object.keys(requirements).sort(),
        tasks.map((task) => text(task.id)).sort(),
      )
    )
      throw new Error();
    const detailedTasks = tasks.map((task) => {
      const requirement = record(requirements[text(task.id)]);
      for (const key of [
        "revision",
        "role",
        "purpose",
        "readiness",
        "complexity",
        "risk",
        "paths",
        "domains",
        "required_capabilities",
        "tools",
        "context_tokens",
        "duration_seconds",
      ]) {
        if (!same(task[key], requirement[key])) throw new Error();
      }
      if (
        !["commander", "worker", "reviewer"].includes(text(task.role)) ||
        !["ready", "T0"].includes(text(task.readiness)) ||
        !["T1", "T2", "T3"].includes(text(task.complexity)) ||
        !["standard", "critical"].includes(text(task.risk)) ||
        (task.purpose !== null &&
          task.purpose !== "lead" &&
          task.purpose !== "advice")
      )
        throw new Error();
      return {
        id: text(task.id),
        revision: number(task.revision, 1),
        role: text(task.role),
        purpose: task.purpose,
        readiness: text(task.readiness),
        complexity: text(task.complexity),
        risk: text(task.risk),
        paths: names(task.paths),
        domains: names(task.domains),
        capabilities: names(task.required_capabilities),
        tools: names(task.tools),
        context: number(task.context_tokens, 1),
        duration: number(task.duration_seconds, 1),
        dependencies: names(task.depends_on),
        acceptance: names(task.acceptance),
        required: flag(task.required),
      };
    });
    const context = record(policy.context_policy),
      risk = record(policy.risk_policy);
    if (
      context.input_accounting !== "explicit_approved_upper_bound" ||
      !["none", "pull_request"].includes(text(auth.delivery))
    )
      throw new Error();
    const budget = list(resources.budgets)
      .map(record)
      .find((item) => item.id === auth.budget_ref);
    if (!budget) throw new Error();
    const budgetCeiling = {
      limits: Object.entries(record(budget.currency_limits)).map(
        ([currency, value]): [string, string | null] => [
          currency,
          value === null ? null : text(value),
        ],
      ),
      attempts:
        budget.max_total_attempts == null
          ? null
          : number(budget.max_total_attempts, 1),
      seconds:
        budget.max_duration_seconds == null
          ? null
          : number(budget.max_duration_seconds, 1),
    };
    return {
      digest,
      planDigest: hash(plan.plan_digest),
      authorizationDigest: hash(plan.authorization_digest),
      configurationDigest: hash(plan.configuration_digest),
      readPaths: names(auth.read_paths),
      writePaths: names(auth.write_paths),
      checks: names(auth.checks),
      delivery: text(auth.delivery),
      targetBranch: text(auth.target_branch),
      budget: text(auth.budget_ref),
      budgetCeiling,
      profiles,
      channels,
      destinations,
      tools,
      capabilities: names(auth.required_capabilities),
      currencyLimits,
      attemptSeconds: number(auth.max_attempt_duration_seconds, 1),
      repairRounds: number(auth.max_quality_repair_rounds),
      stages,
      tasks: detailedTasks,
      policy: {
        ...ref(policy),
        owner: text(policy.registered_by),
        projectId,
        digest: hash(policy.digest),
        toolPolicy: ref(policy.tool_policy),
        riskPolicy: ref(risk),
        contextPolicy: ref(context),
        risk: Object.entries(record(risk.mapping)).map(([key, value]) => [
          key,
          text(value),
        ]),
        floors: list(risk.path_floors).map((value) => {
          const floor = record(value);
          return {
            path: text(floor.prefix),
            minimum: text(floor.minimum_class),
          };
        }),
        reservedOutput: number(context.reserved_output_tokens),
        maxContext: number(policy.max_context_tokens, 1),
      },
      rulebook: { ...ref(rulebook), digest: hash(rulebook.digest) },
    };
  } catch {
    return null;
  }
}

const joined = (values: string[]) => values.join("、") || "无";
const profileNames = (values: Ref[]) =>
  values.map((item) => `${item.id}（版本 ${item.revision}）`).join("、") ||
  "无配置，需等待";
const roleNames: Record<string, string> = {
  commander: "规划",
  worker: "实现",
  reviewer: "审查",
};

export function RoutingAuthorization({
  scope,
  canApprove,
  busy,
  onApprove,
}: {
  scope: RoutingScope;
  canApprove: boolean;
  busy: boolean;
  onApprove: () => void;
}) {
  const [reviewed, setReviewed] = useState(false);
  return (
    <section
      className="preview-result routing-authorization"
      aria-label="完整执行授权（v2）"
    >
      <h4>完整执行授权（v2）</h4>
      <p className="field-help">
        这次确认保存固定版本的授权。实际派发尚未接入，来源与能力仍需验证。
      </p>
      <h4>修改与交付范围</h4>
      <p>允许读取：{joined(scope.readPaths)}</p>
      <p>允许修改：{joined(scope.writePaths)}</p>
      <p>
        必需检查：
        {joined(
          scope.checks.map((item) =>
            item === "independent_review" ? "独立审查" : item,
          ),
        )}
      </p>
      <p>
        交付：
        {scope.delivery === "pull_request"
          ? `向 ${scope.targetBranch} 创建 PR；合并由你决定`
          : "暂不交付 PR"}
      </p>
      <h4>允许的服务与模型</h4>
      <ul>
        {scope.profiles.map((item) => (
          <li key={`${item.ref.id}:${item.ref.revision}`}>
            <div>
              <strong>{profileNames([item.ref])}</strong>
              <p>
                模型：{item.model}；执行器：{item.runtime}
              </p>
              <p>
                通道：{item.channel}；账户：{item.account}；计费：{item.billing}
              </p>
            </div>
          </li>
        ))}
      </ul>
      <ul>
        {scope.channels.map((item) => (
          <li key={item.id}>
            <div>
              <strong>
                {item.id} · {item.provider}
              </strong>
              <p>
                {item.billing}；账户：{item.account}；数据去向：
                {item.destination}
              </p>
            </div>
          </li>
        ))}
      </ul>
      <p>允许的数据去向：{joined(scope.destinations)}</p>
      <h4>工具与执行要求</h4>
      <ul>
        {scope.tools.map((item) => (
          <li key={item.id}>
            {item.id}：{joined(item.permissions)}
          </li>
        ))}
      </ul>
      <p>必需能力：{joined(scope.capabilities)}</p>
      <p>最低隔离要求：工具沙箱（tool_sandboxed）；尚未验证执行器资格。</p>
      <h4>本次批准的资源上限</h4>
      <p>
        原币限额：
        {scope.currencyLimits
          .map(([currency, amount]) => `${currency} ${amount}`)
          .join("，") || "无现金币种授权"}
      </p>
      <p>
        单次尝试最多 {scope.attemptSeconds} 秒；质量修复最多{" "}
        {scope.repairRounds} 轮。
      </p>
      <p className="field-help">
        预算引用：{scope.budget}
        。这些是本次允许使用的上限，服务配额与余额仍需另行核验。
      </p>
      <p>
        所引用的运行预算上限：
        {scope.budgetCeiling.limits
          .map(([currency, value]) => `${currency} ${value ?? "未确定"}`)
          .join("，") || "无币种限额"}
        ；{scope.budgetCeiling.attempts ?? "未确定"} 次尝试、
        {scope.budgetCeiling.seconds ?? "未确定"}{" "}
        秒。上方原币限额是本次批准的范围。
      </p>
      <h4>允许的分发阶段</h4>
      {scope.stages.length === 0 ? (
        <p>未授权分发阶段。</p>
      ) : (
        <ul>
          {scope.stages.map((stage) => (
            <li key={stage.id}>
              <div>
                <strong>{stage.id}</strong>
                <p>普通阶段：{stage.normalAllowed ? "允许" : "不允许"}</p>
                {stage.normal.map((group) => (
                  <p key={group.group}>
                    配置组 {group.group}：{profileNames(group.profiles)}
                  </p>
                ))}
                {stage.quality.length ? (
                  stage.quality.map((group) => (
                    <p key={group.index}>
                      质量升级第 {group.index + 1} 组（序号 {group.index}）·{" "}
                      {group.group}：{profileNames(group.profiles)}
                    </p>
                  ))
                ) : (
                  <p>质量升级：未授权</p>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      <h4>各任务的固定要求</h4>
      <div className="project-grid">
        {scope.tasks.map((task) => (
          <article className="project-card" key={task.id}>
            <h4>
              {task.id} · 任务版本 {task.revision}
            </h4>
            <p>
              {roleNames[task.role]} ·{" "}
              {task.purpose === "lead"
                ? "主 Commander"
                : task.purpose === "advice"
                  ? "顾问"
                  : "无 Commander 职责"}{" "}
              · {task.complexity} ·{" "}
              {task.risk === "critical" ? "高风险" : "一般风险"}
            </p>
            <p>
              {task.required ? "必需任务" : "可选任务"} ·{" "}
              {task.readiness === "ready" ? "已就绪" : "仍待澄清，不能执行"}
            </p>
            <p>任务路径：{joined(task.paths)}</p>
            <p>领域：{joined(task.domains)}</p>
            <p>额外能力：{joined(task.capabilities)}</p>
            <p>任务工具：{joined(task.tools)}</p>
            <p>
              输入上界 {task.context} token；任务时长 {task.duration} 秒。
            </p>
            <p>前置任务：{joined(task.dependencies)}</p>
            <ul>
              {task.acceptance.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </article>
        ))}
      </div>
      <p>
        上下文按已批准的输入上界记账，另预留输出 {scope.policy.reservedOutput}{" "}
        token；总上下文上限 {scope.policy.maxContext}{" "}
        token。实际输入尚需执行前测量。
      </p>
      <p>
        风险映射：
        {scope.policy.risk
          .map(([name, tier]) => `${name} → ${tier}`)
          .join("，")}
      </p>
      <p>
        路径风险下限：
        {scope.policy.floors
          .map((floor) => `${floor.path} → ${floor.minimum}`)
          .join("，") || "无"}
      </p>
      <details>
        <summary>核对固定版本与授权摘要</summary>
        <p>
          执行政策：{scope.policy.id}（版本 {scope.policy.revision}）；登记人：
          {scope.policy.owner}；项目：{scope.policy.projectId}
        </p>
        <p>工具政策：{profileNames([scope.policy.toolPolicy])}</p>
        <p>风险政策：{profileNames([scope.policy.riskPolicy])}</p>
        <p>上下文政策：{profileNames([scope.policy.contextPolicy])}</p>
        <p>
          政策正文摘要：<code>{scope.policy.digest}</code>
        </p>
        <p>
          Rulebook：{scope.rulebook.id}（版本 {scope.rulebook.revision}）
        </p>
        <p>
          Rulebook 正文摘要：<code>{scope.rulebook.digest}</code>
        </p>
        <p>
          计划摘要：<code>{scope.planDigest}</code>
        </p>
        <p>
          配置摘要：<code>{scope.configurationDigest}</code>
        </p>
        <p>
          授权摘要：<code>{scope.authorizationDigest}</code>
        </p>
        <p>
          路由授权摘要：<code>{scope.digest}</code>
        </p>
      </details>
      {canApprove && (
        <div className="approval-confirmation">
          <label>
            <input
              type="checkbox"
              checked={reviewed}
              disabled={busy}
              onChange={(event) => setReviewed(event.target.checked)}
            />
            我已审阅以上完整授权范围（v2）
          </label>
          <button disabled={busy || !reviewed} onClick={onApprove}>
            确认 v2 授权
          </button>
        </div>
      )}
    </section>
  );
}
