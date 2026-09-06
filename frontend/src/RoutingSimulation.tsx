import { useEffect, useRef, useState, type ChangeEvent } from "react";
import type { Rulebook } from "./RulebookPanel";
import "./RoutingSimulation.css";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Fields = { [key: string]: Json };
export type SimulationInput = {
  task: Fields & {
    role: string;
    purpose: string | null;
    readiness: string;
    complexity: string;
    risk: string;
    paths: string[];
    context_tokens: number;
    duration_seconds: number;
    authorization: Fields;
  };
  policy: Fields & { rulebook: Fields; profile_facts: Fields[] };
  capacity: Fields & { as_of: number; pools: Fields[]; estimates: Fields[] };
};
type Candidate = Fields & {
  profile: Fields;
  eligible: boolean;
  reason_codes: string[];
};
type RouteResult = Fields & {
  candidates: Candidate[];
  reason_codes: string[];
  matching_rules: Fields[];
  selected_profile: Fields | null;
};
type Report = {
  schema_version: string;
  scope: string;
  activation_allowed: false;
  model_calls: 0;
  result: RouteResult;
};
type Scope = { csrf: string; projectId: string; active: boolean };
type Operation = { scope: Scope; revision: number; draft: string };

function object(value: unknown): value is Fields {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function strings(value: unknown): value is string[] {
  return (
    Array.isArray(value) && value.every((item) => typeof item === "string")
  );
}
function parseImported(text: string): unknown {
  const value: unknown = JSON.parse(text, (_key: string, item: unknown) => {
    if (
      typeof item === "number" &&
      (!Number.isFinite(item) ||
        (Number.isInteger(item) && !Number.isSafeInteger(item)))
    )
      throw new Error("模拟文件的数字超出可靠范围，请使用有限数值和安全整数。");
    return item;
  });
  // JSON.parse validates syntax but discards duplicate keys. Scan the original
  // string tokens too, comparing decoded keys within each object independently.
  const stack: { keys: Set<string> | null; nextKey: boolean }[] = [];
  for (const token of text.matchAll(/"(?:\\.|[^"\\])*"|[{}\[\]:,]/gs)) {
    const item = token[0];
    if (item === "{" || item === "[")
      stack.push({
        keys: item === "{" ? new Set() : null,
        nextKey: item === "{",
      });
    else if (item === "}" || item === "]") stack.pop();
    else {
      const parent = stack.at(-1);
      if (!parent?.keys) continue;
      if (item === ",") parent.nextKey = true;
      else if (item === ":") parent.nextKey = false;
      else if (parent.nextKey && item.startsWith('"')) {
        const key: string = JSON.parse(item);
        if (parent.keys.has(key))
          throw new Error("模拟文件含有重复字段，请消除歧义后重新导入。");
        parent.keys.add(key);
        parent.nextKey = false;
      }
    }
  }
  return value;
}
function records(value: unknown): value is Fields[] {
  return Array.isArray(value) && value.every(object);
}
function inputDocument(value: unknown): SimulationInput | null {
  if (
    !object(value) ||
    Object.keys(value).sort().join(",") !== "capacity,policy,task"
  )
    return null;
  const { task, policy, capacity } = value;
  if (
    !object(task) ||
    !object(policy) ||
    !object(capacity) ||
    task.schema_version !== "karajan.routing.task.v1" ||
    policy.schema_version !== "karajan.routing.policy.v1" ||
    capacity.schema_version !== "karajan.routing.capacity.v1" ||
    !object(policy.rulebook) ||
    !object(task.authorization) ||
    !records(policy.profile_facts) ||
    !records(capacity.pools) ||
    !records(capacity.estimates)
  )
    return null;
  if (
    !strings(task.paths) ||
    !["commander", "worker", "reviewer"].includes(String(task.role)) ||
    !["ready", "T0"].includes(String(task.readiness)) ||
    !["T1", "T2", "T3"].includes(String(task.complexity)) ||
    typeof task.risk !== "string" ||
    (task.purpose !== null &&
      !["lead", "advice"].includes(String(task.purpose))) ||
    typeof capacity.as_of !== "number" ||
    typeof task.context_tokens !== "number" ||
    typeof task.duration_seconds !== "number"
  )
    return null;
  return value as unknown as SimulationInput;
}
function reportDocument(value: unknown): Report | null {
  if (
    !object(value) ||
    value.schema_version !== "karajan.rulebook-simulation.v1" ||
    value.scope !== "explicit_simulation" ||
    value.activation_allowed !== false ||
    value.model_calls !== 0 ||
    !object(value.result)
  )
    return null;
  const result = value.result;
  if (
    result.schema_version !== "karajan.routing.result.v1" ||
    result.scope !== "simulation_only" ||
    result.activation_allowed !== false ||
    !strings(result.reason_codes) ||
    !records(result.matching_rules) ||
    !records(result.candidates) ||
    !result.candidates.every(
      (row) =>
        object(row.profile) &&
        typeof row.eligible === "boolean" &&
        strings(row.reason_codes),
    ) ||
    (result.selected_profile !== null && !object(result.selected_profile))
  )
    return null;
  return value as unknown as Report;
}
const labels: Record<string, string> = {
  fixture: "固定离线样例",
  official: "导入的官方观察",
  manual: "手工记录",
  local_ledger: "导入的本地记录",
  imported_observation: "导入观察",
  known: "已知",
  calibrated: "经校准估计",
  unknown: "未知",
  bounded_calls: "调用上限可约束",
  estimated_stop: "估计停止",
  preference_band: "偏好等级",
  uncertainty_band: "信息确定程度",
  bottleneck_quota_pressure: "最紧张额度池压力",
  incremental_cash_estimate: "预计新增现金",
  completion_time_estimate: "预计完成秒数",
  profile_id: "稳定配置顺序",
  TASK_NOT_READY: "任务尚未就绪",
  NO_RULE: "没有匹配规则",
  RULE_AMBIGUOUS: "最高优先级规则冲突",
  NO_ELIGIBLE_PROFILE: "没有合格候选",
  NO_STAGE_CANDIDATE: "此阶段没有候选",
  GROUP_PROFILE_NOT_APPROVED: "候选不在冻结批准组中",
  PROFILE_NOT_AUTHORIZED: "配置不在批准范围",
  STAGE_NOT_AUTHORIZED: "阶段未获批准",
  QUALITY_STAGE_NOT_AUTHORIZED: "升级阶段未获批准",
  QUALITY_STAGE_NOT_REACHED: "尚未到达升级阶段",
  QUALITY_REPAIR_LIMIT_REACHED: "质量修复轮数已达上限",
  QUALIFICATION_STALE: "资格证据已过期",
  QUOTA_UNKNOWN: "额度未知",
  QUOTA_INSUFFICIENT: "额度不足",
  CASH_BUDGET_EXCEEDED: "现金预算不足",
  PROFILE_DISABLED: "配置已停用",
};
function display(value: unknown): string {
  if (value === null || value === undefined) return "未知";
  if (typeof value === "string") return label(value);
  if (
    object(value) &&
    typeof value.id === "string" &&
    typeof value.revision === "number"
  )
    return `${value.id} v${value.revision}`;
  if (
    object(value) &&
    typeof value.numerator === "number" &&
    typeof value.denominator === "number"
  )
    return `${value.numerator} / ${value.denominator}`;
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}
function label(value: string): string {
  return Object.hasOwn(labels, value) ? labels[value] : value;
}
function time(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "未知";
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime())
    ? "未知"
    : date.toISOString().replace("T", " ").replace(".000Z", " UTC");
}
function Reasons({ codes }: { codes: string[] }) {
  return codes.length ? (
    <ul className="simulation-reasons">
      {codes.map((code, index) => (
        <li key={`${index}:${code}`}>
          {label(code)}
          {Object.hasOwn(labels, code) && <small>{code}</small>}
        </li>
      ))}
    </ul>
  ) : (
    <p>没有淘汰理由。</p>
  );
}
function FieldsList({ fields }: { fields: Fields }) {
  return (
    <dl className="simulation-fields">
      {Object.entries(fields).map(([key, value]) => (
        <div key={key}>
          <dt>{label(key)}</dt>
          <dd>{display(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
function exportJson(value: unknown, filename: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2) + "\n"], {
      type: "application/json",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

export function RoutingSimulation({
  project,
  csrf,
  draft,
  onSessionExpired,
}: {
  project: { id: string; name: string };
  csrf: string;
  draft: Rulebook | null;
  onSessionExpired: () => void;
}) {
  const [snapshot, setSnapshot] = useState<SimulationInput | null>(null);
  const [source, setSource] = useState("");
  const [adjusted, setAdjusted] = useState(false);
  const [report, setReport] = useState<{
    value: Report;
    draft: string;
    revision: number;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const draftKey = JSON.stringify(draft);
  const rendered = useRef({ projectId: project.id, csrf, draft: draftKey });
  rendered.current = { projectId: project.id, csrf, draft: draftKey };
  const expire = useRef(onSessionExpired);
  expire.current = onSessionExpired;
  const scopeRef = useRef<Scope | null>(null);
  const revision = useRef(0);
  const activeOperation = useRef<Operation | null>(null);
  function currentScope(scope: Scope) {
    return (
      scope.active &&
      scopeRef.current === scope &&
      rendered.current.csrf === scope.csrf &&
      rendered.current.projectId === scope.projectId
    );
  }
  function current(operation: Operation) {
    return (
      currentScope(operation.scope) &&
      operation.revision === revision.current &&
      rendered.current.draft === operation.draft &&
      activeOperation.current === operation
    );
  }
  function invalidate() {
    revision.current += 1;
    activeOperation.current = null;
    setReport(null);
    setBusy(false);
    setError("");
  }
  useEffect(() => {
    const scope = { projectId: project.id, csrf, active: true };
    scopeRef.current = scope;
    invalidate();
    setSnapshot(null);
    setSource("");
    setAdjusted(false);
    return () => {
      scope.active = false;
      if (scopeRef.current === scope) scopeRef.current = null;
    };
  }, [csrf, project.id]);
  useEffect(() => {
    invalidate();
  }, [draftKey]);
  function begin(): Operation | null {
    const scope = scopeRef.current;
    if (!scope || !currentScope(scope) || activeOperation.current) return null;
    const operation = { scope, revision: ++revision.current, draft: draftKey };
    activeOperation.current = operation;
    setReport(null);
    setError("");
    setBusy(true);
    return operation;
  }
  function finish(operation: Operation) {
    if (current(operation)) {
      activeOperation.current = null;
      setBusy(false);
    }
  }
  function fail(operation: Operation, message: string) {
    if (current(operation)) setError(message);
  }
  async function responseJson(
    operation: Operation,
    response: Response,
  ): Promise<unknown> {
    if (!current(operation)) return null;
    if (response.status === 401) {
      expire.current();
      throw new Error("会话已过期，请重新登录。");
    }
    const body: unknown = await response.json();
    if (!current(operation)) return null;
    if (!response.ok) {
      const reason =
        object(body) && typeof body.reason_code === "string"
          ? body.reason_code
          : "请求未被接受";
      const issues =
        object(body) && records(body.issues)
          ? body.issues
              .map((issue) => `${display(issue.path)}: ${display(issue.code)}`)
              .join("；")
          : "";
      throw new Error(
        `无法完成模拟：${label(reason)}${issues ? `（${issues}）` : ""}`,
      );
    }
    return body;
  }
  async function loadExample() {
    const operation = begin();
    if (!operation) return;
    try {
      const response = await fetch(
        `/v1/projects/${encodeURIComponent(operation.scope.projectId)}/rulebook/simulation-example`,
      );
      const body = await responseJson(operation, response);
      if (!current(operation)) return;
      const next = inputDocument(body);
      if (!next) throw new Error("离线示例格式不完整，请重试或导入完整文件。");
      setSnapshot(next);
      setSource("内置固定离线示例");
      setAdjusted(false);
    } catch (cause) {
      fail(
        operation,
        cause instanceof Error && !(cause instanceof TypeError)
          ? cause.message
          : "无法读取离线示例，请重试。",
      );
    } finally {
      finish(operation);
    }
  }
  async function importFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const operation = begin();
    if (!operation) return;
    try {
      if (file.size > 2_000_000) throw new Error("模拟文件不能超过 2 MB。");
      const text = await file.text();
      if (!current(operation)) return;
      const parsed = inputDocument(parseImported(text));
      if (!parsed)
        throw new Error(
          "请导入包含 task、policy、capacity 三份完整快照的文件。",
        );
      setSnapshot(parsed);
      setSource(`导入文件：${file.name}`);
      setAdjusted(false);
    } catch (cause) {
      fail(
        operation,
        cause instanceof SyntaxError
          ? "文件不是有效 JSON。"
          : cause instanceof Error
            ? cause.message
            : "无法读取模拟文件。",
      );
    } finally {
      finish(operation);
    }
  }
  function editTask(
    patch: Partial<
      Pick<
        SimulationInput["task"],
        | "role"
        | "purpose"
        | "readiness"
        | "complexity"
        | "risk"
        | "paths"
        | "context_tokens"
        | "duration_seconds"
      >
    >,
  ) {
    if (!snapshot) return;
    invalidate();
    setSnapshot({ ...snapshot, task: { ...snapshot.task, ...patch } });
    setAdjusted(true);
  }
  function composed(): SimulationInput | null {
    return snapshot && draft
      ? { ...snapshot, policy: { ...snapshot.policy, rulebook: draft } }
      : null;
  }
  async function simulate() {
    const input = composed();
    if (!input) return;
    const payload = JSON.stringify(input);
    if (new TextEncoder().encode(payload).byteLength > 65_536) {
      invalidate();
      setError(
        "当前规则与模拟快照合计超过 64 KiB 请求上限，请精简输入后重试。",
      );
      return;
    }
    const operation = begin();
    if (!operation) return;
    try {
      const response = await fetch(
        `/v1/projects/${encodeURIComponent(operation.scope.projectId)}/rulebook/simulate`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": operation.scope.csrf,
          },
          body: payload,
        },
      );
      const body = await responseJson(operation, response);
      if (!current(operation)) return;
      const next = reportDocument(body);
      if (!next) throw new Error("尚未取得完整模拟报告，请重新模拟。");
      setReport({
        value: next,
        draft: operation.draft,
        revision: operation.revision,
      });
    } catch (cause) {
      fail(
        operation,
        cause instanceof Error && !(cause instanceof TypeError)
          ? cause.message
          : "无法取得模拟结果，请重试。",
      );
    } finally {
      finish(operation);
    }
  }
  const result =
    report && report.draft === draftKey && report.revision === revision.current
      ? report.value.result
      : null;
  const candidates = result?.candidates ?? [];
  const eligible = candidates
    .filter((candidate) => candidate.eligible)
    .sort((left, right) => Number(left.rank) - Number(right.rank));
  const refused = candidates.filter((candidate) => !candidate.eligible);
  const validTask =
    snapshot &&
    Number.isSafeInteger(snapshot.task.context_tokens) &&
    snapshot.task.context_tokens > 0 &&
    Number.isSafeInteger(snapshot.task.duration_seconds) &&
    snapshot.task.duration_seconds > 0 &&
    snapshot.task.risk.trim().length > 0;
  return (
    <details className="routing-simulation">
      <summary>路由模拟 · 使用固定快照演练</summary>
      <section aria-label="路由模拟">
        <h3>看看当前规则会怎样选择模型</h3>
        <p className="simulation-boundary">
          这里只演练导入的任务、授权和资源事实，不读取当前服务额度，不启动模型。离线示例中的资格与额度仅为样例；模拟通过也不代表可启动。
        </p>
        <div className="simulation-actions">
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={loadExample}
          >
            加载固定离线示例
          </button>
          <label>
            导入模拟快照
            <input
              type="file"
              accept=".json,application/json"
              aria-label="导入模拟快照"
              disabled={busy}
              onChange={importFile}
            />
          </label>
          <button
            type="button"
            className="secondary"
            disabled={busy || !snapshot || !draft}
            onClick={() => exportJson(composed(), "routing-input.json")}
          >
            导出当前模拟输入
          </button>
        </div>
        {busy && <p role="status">正在处理模拟…</p>}
        {error && (
          <p role="alert" className="simulation-error">
            {error}
          </p>
        )}
        {!snapshot && (
          <p>先选择明确的离线示例或导入完整快照，再调整任务事实。</p>
        )}
        {snapshot && (
          <>
            <p className="simulation-source">
              事实来源：{source}
              {adjusted && " · 任务事实已在本页调整"}。资源快照时间：
              {time(snapshot.capacity.as_of)}。
            </p>
            <p className="field-help">
              模拟将使用当前编辑的规则{" "}
              {draft ? `${draft.id} v${draft.revision}` : "（尚未载入）"}
              ，替换文件中的规则；冻结批准组与候选上限保持文件原值。模拟输入可导出后编辑高级字段再导入。
            </p>
            <fieldset className="simulation-task" disabled={busy}>
              <legend>任务事实</legend>
              <div className="form-grid">
                <label>
                  模拟角色
                  <select
                    value={snapshot.task.role}
                    onChange={(event) => editTask({ role: event.target.value })}
                  >
                    <option value="commander">Commander</option>
                    <option value="worker">Worker</option>
                    <option value="reviewer">Reviewer</option>
                  </select>
                </label>
                <label>
                  Commander 用途
                  <select
                    value={snapshot.task.purpose ?? ""}
                    onChange={(event) =>
                      editTask({ purpose: event.target.value || null })
                    }
                  >
                    <option value="">未指定</option>
                    <option value="lead">主指挥</option>
                    <option value="advice">顾问</option>
                  </select>
                </label>
                <label>
                  任务就绪情况
                  <select
                    value={snapshot.task.readiness}
                    onChange={(event) =>
                      editTask({ readiness: event.target.value })
                    }
                  >
                    <option value="ready">已就绪</option>
                    <option value="T0">T0 · 待澄清</option>
                  </select>
                </label>
                <label>
                  任务难度
                  <select
                    value={snapshot.task.complexity}
                    onChange={(event) =>
                      editTask({ complexity: event.target.value })
                    }
                  >
                    {["T1", "T2", "T3"].map((item) => (
                      <option key={item}>{item}</option>
                    ))}
                  </select>
                </label>
                <label>
                  风险标签
                  <input
                    value={snapshot.task.risk}
                    onChange={(event) => editTask({ risk: event.target.value })}
                  />
                </label>
                <label>
                  所需上下文 token
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={snapshot.task.context_tokens}
                    onChange={(event) =>
                      editTask({ context_tokens: Number(event.target.value) })
                    }
                  />
                </label>
                <label>
                  预计任务时长（秒）
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={snapshot.task.duration_seconds}
                    onChange={(event) =>
                      editTask({ duration_seconds: Number(event.target.value) })
                    }
                  />
                </label>
              </div>
              <label>
                涉及路径（每行一条）
                <textarea
                  rows={3}
                  value={snapshot.task.paths.join("\n")}
                  onChange={(event) =>
                    editTask({ paths: event.target.value.split("\n") })
                  }
                />
              </label>
            </fieldset>
            <details>
              <summary>核对冻结授权和其他任务条件</summary>
              <p>
                批准配置：{display(snapshot.task.authorization.profile_refs)}
                ；候选上限：
                {display(snapshot.task.authorization.ceiling_profile_refs)}
              </p>
              <FieldsList fields={snapshot.task.authorization} />
              <pre>
                {JSON.stringify(
                  Object.fromEntries(
                    Object.entries(snapshot.task).filter(
                      ([key]) =>
                        ![
                          "authorization",
                          "role",
                          "purpose",
                          "readiness",
                          "complexity",
                          "risk",
                          "paths",
                          "context_tokens",
                          "duration_seconds",
                        ].includes(key),
                    ),
                  ),
                  null,
                  2,
                )}
              </pre>
            </details>
            <details>
              <summary>核对资源事实、时间与未知信息</summary>
              <h4>额度观察</h4>
              {snapshot.capacity.pools.length ? (
                <div className="simulation-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>额度池</th>
                        <th>来源 / 可信程度</th>
                        <th>报告剩余</th>
                        <th>观察时间</th>
                        <th>重置时间</th>
                      </tr>
                    </thead>
                    <tbody>
                      {snapshot.capacity.pools.map((pool, index) => (
                        <tr key={index}>
                          <th>{display(pool.id)}</th>
                          <td>
                            {display(pool.source)} / {display(pool.confidence)}
                          </td>
                          <td>
                            {display(pool.reported_remaining)}{" "}
                            {display(pool.unit)}
                          </td>
                          <td>{time(pool.observed_at)}</td>
                          <td>{time(pool.reset_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p>没有额度观察记录，不能视作额度充足。</p>
              )}
              <h4>资格事实</h4>
              {snapshot.policy.profile_facts.map((fact, index) => (
                <article key={index} className="simulation-fact">
                  <strong>{display(fact.profile)}</strong>
                  <p>
                    {display(fact.provenance)} · {display(fact.evidence_ref)}
                  </p>
                  <p>
                    观察 {time(fact.observed_at)} · 有效至{" "}
                    {time(fact.valid_until)} · 预算限制：
                    {display(fact.budget_enforcement)}
                  </p>
                </article>
              ))}
              {snapshot.policy.profile_facts.length === 0 && (
                <p>没有资格事实。</p>
              )}
              <h4>需求估计</h4>
              {snapshot.capacity.estimates.map((estimate, index) => (
                <p key={index}>
                  {display(estimate.profile)} · {display(estimate.confidence)} ·
                  预计完成 {display(estimate.completion_seconds)} 秒 ·{" "}
                  {display(estimate.evidence_ref)}
                </p>
              ))}
            </details>
            <button
              type="button"
              disabled={busy || !draft || !validTask}
              onClick={simulate}
            >
              模拟当前编辑
            </button>
          </>
        )}
        {result && (
          <section className="simulation-result" aria-label="模拟结果">
            <header>
              <h3>
                {result.selected_profile
                  ? "此快照下可以选出候选"
                  : "此快照下暂时阻塞"}
              </h3>
              <button
                type="button"
                className="secondary"
                onClick={() => exportJson(report!.value, "routing-report.json")}
              >
                导出完整模拟报告
              </button>
            </header>
            <p>
              命中规则：<strong>{display(result.rule_id)}</strong> · 有效难度：
              <strong>{display(result.effective_class)}</strong> · 最终候选：
              <strong>
                {result.selected_profile
                  ? display(result.selected_profile)
                  : "无"}
              </strong>
            </p>
            <p className="field-help">
              仅适用于上面的固定快照和当前编辑。模型调用 0
              次；真实执行资格未验证。
            </p>
            <Reasons codes={result.reason_codes} />
            <details>
              <summary>规则匹配与输入绑定</summary>
              <p>
                匹配规则：
                {result.matching_rules
                  .map(
                    (rule) =>
                      `${display(rule.id)}（优先级 ${display(rule.priority)}）`,
                  )
                  .join("、") || "无"}
              </p>
              <p>规则摘要：{display(result.rulebook_sha256)}</p>
              <FieldsList
                fields={
                  object(result.snapshot_sha256) ? result.snapshot_sha256 : {}
                }
              />
            </details>
            <h4>合格候选（{eligible.length}）</h4>
            {eligible.map((candidate, index) => (
              <article
                className="simulation-candidate"
                aria-label={`候选 ${display(candidate.profile)}`}
                key={index}
              >
                <h4>
                  #{display(candidate.rank)} · {display(candidate.profile)}
                </h4>
                <h5>排序分量</h5>
                {object(candidate.sort_inputs) ? (
                  <FieldsList fields={candidate.sort_inputs} />
                ) : (
                  <p>没有排序分量。</p>
                )}
                <details>
                  <summary>核对资格、现金和各额度池</summary>
                  <FieldsList
                    fields={Object.fromEntries(
                      Object.entries(candidate).filter(
                        ([key]) =>
                          ![
                            "profile",
                            "sort_inputs",
                            "rank",
                            "eligible",
                            "reason_codes",
                          ].includes(key),
                      ),
                    )}
                  />
                </details>
              </article>
            ))}
            <h4>淘汰候选（{refused.length}）</h4>
            {refused.map((candidate, index) => (
              <article
                className="simulation-candidate rejected"
                aria-label={`淘汰 ${display(candidate.profile)}`}
                key={index}
              >
                <h4>{display(candidate.profile)}</h4>
                <Reasons codes={candidate.reason_codes} />
                <details>
                  <summary>核对淘汰依据</summary>
                  <FieldsList
                    fields={Object.fromEntries(
                      Object.entries(candidate).filter(
                        ([key]) =>
                          !["profile", "eligible", "reason_codes"].includes(
                            key,
                          ),
                      ),
                    )}
                  />
                </details>
              </article>
            ))}
            <details>
              <summary>其他编译与排序说明</summary>
              <FieldsList
                fields={{
                  编译问题: result.compiled_issues ?? [],
                  编译提醒: result.compiled_warnings ?? [],
                  现金排序: result.cash_sort ?? null,
                  解析后的能力组: result.resolved_groups ?? [],
                }}
              />
            </details>
          </section>
        )}
      </section>
    </details>
  );
}
