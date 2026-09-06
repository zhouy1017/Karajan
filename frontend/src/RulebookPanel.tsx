import { useEffect, useRef, useState, type ChangeEvent } from "react";
import "./RulebookPanel.css";
import { RoutingSimulation } from "./RoutingSimulation";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type ObjectValue = { [key: string]: Json };
type ProfileRef = { id: string; revision: number };
type Rule = ObjectValue & {
  id: string;
  priority: number;
  when: ObjectValue;
  eligible_groups: string[];
  capabilities_all: string[];
  quality_escalation_groups?: string[];
};
export type Rulebook = ObjectValue & {
  id: string;
  revision: number;
  description: string;
  profile_groups: Record<string, ProfileRef[]>;
  rules: Rule[];
  resource_policy: ObjectValue & { candidate_order: string[] };
  global_constraints: ObjectValue;
  collaboration: ObjectValue;
};
type Configuration = {
  project_revision: number;
  configuration_revision: number;
  configuration: {
    rulebook: unknown;
    resources?: {
      profiles: (ProfileRef & { enabled: boolean; model_family?: string })[];
    } | null;
  } | null;
};
type Issue = { code: string; path?: string };
type Preview = {
  preview_id: string;
  project_revision: number;
  can_save_draft: boolean;
  can_publish: boolean;
  issues: Issue[];
  compile_issues: Issue[];
  warnings: Issue[];
  waiting_reasons: string[];
  rulebook_sha256: string | null;
};
type Version = ProfileRef & { rulebook_sha256: string };
type Publication = {
  publication_id: string;
  rulebook: Version;
  at: number;
  state: string;
};
type View = {
  configuration: Configuration;
  versions: Version[];
  publications: Publication[];
};
type Scope = {
  csrf: string;
  projectId: string;
  active: boolean;
  storageKey: string | null;
};
type Command = { body: string; revision: number; key: string };
type PendingPublication = {
  version: 1;
  projectId: string;
  command: Command;
  draft: Rulebook;
};

function readPending(
  serialized: string,
  projectId: string,
): PendingPublication {
  const record: unknown = JSON.parse(serialized);
  if (
    !object(record) ||
    record.version !== 1 ||
    record.projectId !== projectId ||
    !object(record.command) ||
    !readRulebook(record.draft)
  )
    throw new Error("Invalid pending publication");
  const command = record.command;
  if (
    typeof command.body !== "string" ||
    typeof command.key !== "string" ||
    !command.key.trim() ||
    !Number.isSafeInteger(command.revision) ||
    Number(command.revision) < 1
  )
    throw new Error("Invalid pending command");
  const body: unknown = JSON.parse(command.body);
  if (
    !object(body) ||
    Object.keys(body).length !== 1 ||
    typeof body.preview_id !== "string" ||
    !body.preview_id.trim()
  )
    throw new Error("Invalid pending body");
  return record as unknown as PendingPublication;
}

const names: Record<string, string> = {
  commander: "Commander",
  worker: "Worker",
  reviewer: "Reviewer",
  lead: "主指挥",
  advice: "顾问",
  ready: "已就绪",
  preference_band: "偏好等级",
  uncertainty_band: "信息确定程度",
  bottleneck_quota_pressure: "最紧张额度池的压力",
  incremental_cash_estimate: "预计新增现金支出",
  completion_time_estimate: "预计完成时间",
  profile_id: "配置标识（稳定顺序）",
  LIVE_QUALIFICATION_NOT_RUN: "尚未验证真实执行资格",
  EMPTY_PROFILE_GROUP: "能力组尚无成员",
  GROUP_EMPTY: "能力组尚无成员",
  PROFILE_GROUP_EMPTY: "能力组尚无成员",
};
const limits: Record<string, string> = {
  max_parallel_writers_per_project: "每项目并行 Writer 上限",
  max_quality_repair_rounds: "每 Run 质量修复轮数上限",
  max_infrastructure_retries_per_root_task: "每根任务基础设施重试上限",
};
function fixedCollaboration(value: ObjectValue) {
  return Object.fromEntries(
    Object.entries(value).filter(([key]) => !(key in limits)),
  );
}

function object(value: unknown): value is ObjectValue {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function strings(value: unknown): value is string[] {
  return (
    Array.isArray(value) && value.every((item) => typeof item === "string")
  );
}
function readRulebook(value: unknown): Rulebook | null {
  if (
    !object(value) ||
    value.schema_version !== "karajan.rulebook.v1" ||
    typeof value.id !== "string" ||
    !Number.isSafeInteger(value.revision) ||
    typeof value.description !== "string" ||
    !object(value.profile_groups) ||
    !object(value.global_constraints) ||
    !object(value.collaboration) ||
    !object(value.resource_policy) ||
    !strings(value.resource_policy.candidate_order) ||
    !Array.isArray(value.rules)
  )
    return null;
  if (
    !Object.values(value.profile_groups).every(
      (refs) =>
        Array.isArray(refs) &&
        refs.every(
          (ref) =>
            object(ref) &&
            typeof ref.id === "string" &&
            Number.isSafeInteger(ref.revision),
        ),
    )
  )
    return null;
  if (
    !value.rules.every(
      (rule) =>
        object(rule) &&
        typeof rule.id === "string" &&
        Number.isSafeInteger(rule.priority) &&
        object(rule.when) &&
        strings(rule.eligible_groups) &&
        strings(rule.capabilities_all) &&
        (rule.quality_escalation_groups === undefined ||
          strings(rule.quality_escalation_groups)),
    )
  )
    return null;
  return value as Rulebook;
}
function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (object(value))
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stable(value[key])}`)
      .join(",")}}`;
  return JSON.stringify(value) ?? "（无）";
}
function differences(
  before: unknown,
  after: unknown,
  path = "规则",
): { path: string; before: string; after: string }[] {
  if (stable(before) === stable(after)) return [];
  if (path === "规则.rules" && Array.isArray(before) && Array.isArray(after)) {
    const keyed = (rows: unknown[]) =>
      rows.every((row) => object(row) && typeof row.id === "string") &&
      new Set(rows.map((row) => (row as { id: string }).id)).size ===
        rows.length;
    if (keyed(before) && keyed(after)) {
      const previous = new Map(
        before.map((row) => [String((row as ObjectValue).id), row]),
      );
      const next = new Map(
        after.map((row) => [String((row as ObjectValue).id), row]),
      );
      const changes = [
        ...new Set([...previous.keys(), ...next.keys()]),
      ].flatMap((id) =>
        differences(previous.get(id), next.get(id), `${path}.${id}`),
      );
      if (stable([...previous.keys()]) !== stable([...next.keys()]))
        changes.push({
          path: `${path}.规则顺序（含新增或移除）`,
          before: stable([...previous.keys()]),
          after: stable([...next.keys()]),
        });
      return changes;
    }
  }
  if (object(before) && object(after))
    return [
      ...new Set([...Object.keys(before), ...Object.keys(after)]),
    ].flatMap((key) =>
      differences(
        before[key],
        after[key],
        `${path}.${key === "priority" ? "优先级" : key}`,
      ),
    );
  return [{ path, before: stable(before), after: stable(after) }];
}
function toggle(values: string[], value: string): string[] {
  return values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value];
}
function Issues({ title, items }: { title: string; items: Issue[] }) {
  return (
    items.length > 0 && (
      <div>
        <h4>{title}</h4>
        <ul>
          {items.map((issue, index) => (
            <li key={index}>
              {names[issue.code] ?? issue.code}
              {issue.path ? ` · ${issue.path}` : ""}
            </li>
          ))}
        </ul>
      </div>
    )
  );
}

export function RulebookPanel({
  project,
  csrf,
  onSessionExpired,
}: {
  project: { id: string; name: string };
  csrf: string;
  onSessionExpired: () => void;
}) {
  const [view, setView] = useState<View | null>(null);
  const [draft, setDraft] = useState<Rulebook | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(true);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [storageProblem, setStorageProblem] = useState("");
  const session = useRef<Scope | null>(null);
  const rendered = useRef({ csrf, projectId: project.id });
  rendered.current = { csrf, projectId: project.id };
  const expire = useRef(onSessionExpired);
  expire.current = onSessionExpired;
  const editing = useRef(0);
  const pending = useRef<Command | null>(null);
  const operation = useRef(false);
  const previewCommand = useRef<Command | null>(null);

  function current(scope: Scope) {
    return (
      scope.active &&
      session.current === scope &&
      rendered.current.csrf === scope.csrf &&
      rendered.current.projectId === scope.projectId
    );
  }
  function endpoint(scope: Scope, suffix: string) {
    return `/v1/projects/${encodeURIComponent(scope.projectId)}/${suffix}`;
  }
  async function read(scope: Scope, suffix: string): Promise<unknown> {
    const response = await fetch(endpoint(scope, suffix));
    if (!current(scope)) return null;
    if (response.status === 401) {
      expire.current();
      throw new Error("会话已过期，请重新登录。");
    }
    if (!response.ok) throw new Error("暂时无法读取规则，请重试。");
    const body: unknown = await response.json();
    return current(scope) ? body : null;
  }
  async function load(scope: Scope, replaceDraft: boolean) {
    const [configuration, versions, publications] = await Promise.all([
      read(scope, "configuration"),
      read(scope, "rulebook/versions"),
      read(scope, "rulebook/publications"),
    ]);
    if (!current(scope)) return;
    const next = {
      configuration: configuration as Configuration,
      versions: (versions as { items: Version[] }).items,
      publications: (publications as { items: Publication[] }).items,
    };
    setView(next);
    if (replaceDraft)
      setDraft(readRulebook(next.configuration.configuration?.rulebook));
  }
  useEffect(() => {
    const scope: Scope = {
      csrf,
      projectId: project.id,
      active: true,
      storageKey: null,
    };
    session.current = scope;
    operation.current = true;
    pending.current = null;
    previewCommand.current = null;
    editing.current += 1;
    setView(null);
    setDraft(null);
    setPreview(null);
    setError("");
    setNotice("");
    setUncertain(false);
    setStorageProblem("");
    setBusy(true);
    async function restore() {
      try {
        const digest = await crypto.subtle.digest(
          "SHA-256",
          new TextEncoder().encode(scope.csrf),
        );
        if (!current(scope)) return;
        const identity = Array.from(new Uint8Array(digest), (byte) =>
          byte.toString(16).padStart(2, "0"),
        ).join("");
        const key = `karajan.pending-rulebook.v1:${identity}:${encodeURIComponent(scope.projectId)}`;
        const stored = sessionStorage.getItem(key);
        const record =
          stored === null ? null : readPending(stored, scope.projectId);
        scope.storageKey = key;
        if (record) {
          pending.current = record.command;
          setDraft(record.draft);
          setUncertain(true);
          setNotice("已恢复待核对的发布请求与编辑内容，请显式重试原请求。");
        }
      } catch {
        if (current(scope))
          setStorageProblem(
            "无法读取本标签页的待核对记录，暂不能发布。请保留页面并核对已有发布。",
          );
      }
      if (current(scope)) await load(scope, pending.current === null);
    }
    restore()
      .catch(() => {
        if (current(scope)) setError("暂时无法读取规则，请重试。");
      })
      .finally(() => {
        if (current(scope)) {
          operation.current = false;
          setBusy(false);
        }
      });
    return () => {
      scope.active = false;
      if (session.current === scope) session.current = null;
    };
  }, [csrf, project.id]);

  function remember(scope: Scope, command: Command, document: Rulebook) {
    if (!scope.storageKey) throw new Error("Pending storage unavailable");
    const record: PendingPublication = {
      version: 1,
      projectId: scope.projectId,
      command,
      draft: document,
    };
    const serialized = JSON.stringify(record);
    const existing = sessionStorage.getItem(scope.storageKey);
    if (
      existing !== null &&
      stable(readPending(existing, scope.projectId)) !== stable(record)
    )
      throw new Error("Different pending publication");
    readPending(serialized, scope.projectId);
    sessionStorage.setItem(scope.storageKey, serialized);
    if (sessionStorage.getItem(scope.storageKey) !== serialized)
      throw new Error("Pending storage not confirmed");
  }
  function forget(scope: Scope, command: Command) {
    if (!scope.storageKey) throw new Error("Pending storage unavailable");
    const existing = sessionStorage.getItem(scope.storageKey);
    if (existing === null) return;
    const record = readPending(existing, scope.projectId);
    if (stable(record.command) !== stable(command))
      throw new Error("Different pending publication");
    sessionStorage.removeItem(scope.storageKey);
    if (sessionStorage.getItem(scope.storageKey) !== null)
      throw new Error("Pending record not removed");
  }

  function edit(next: Rulebook) {
    if (pending.current) return;
    editing.current += 1;
    previewCommand.current = null;
    setDraft(next);
    setPreview(null);
    setNotice("");
    setError("");
  }
  async function refresh() {
    const scope = session.current;
    if (!scope || !current(scope) || operation.current || pending.current)
      return;
    operation.current = true;
    setBusy(true);
    setError("");
    setPreview(null);
    previewCommand.current = null;
    try {
      await load(scope, false);
    } catch {
      if (current(scope)) {
        setView(null);
        setError("未能刷新当前版本，请重试后再预览。");
      }
    } finally {
      if (current(scope)) {
        operation.current = false;
        setBusy(false);
      }
    }
  }
  async function importFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    const scope = session.current;
    if (
      !file ||
      !scope ||
      !current(scope) ||
      operation.current ||
      pending.current
    )
      return;
    operation.current = true;
    setBusy(true);
    setError("");
    try {
      if (file.size > 2_000_000) throw new Error("规则文件不能超过 2 MB。");
      const contents = await file.text();
      if (!current(scope)) return;
      const parsed = readRulebook(JSON.parse(contents));
      if (!parsed)
        throw new Error("文件需要包含完整的 Rulebook 文档，请检查格式。");
      const original = readRulebook(
        view?.configuration.configuration?.rulebook,
      );
      if (
        original &&
        (stable(parsed.global_constraints) !==
          stable(original.global_constraints) ||
          stable(fixedCollaboration(parsed.collaboration)) !==
            stable(fixedCollaboration(original.collaboration)))
      )
        throw new Error("导入不能改变已确认的全局约束与固定协作规则。");
      edit(parsed);
      setNotice("文件已载入表单，请预览差异后发布。");
    } catch (cause) {
      if (current(scope))
        setError(
          cause instanceof SyntaxError
            ? "文件不是有效的 JSON。"
            : cause instanceof Error
              ? cause.message
              : "无法读取规则文件。",
        );
    } finally {
      if (current(scope)) {
        operation.current = false;
        setBusy(false);
      }
    }
  }
  function exportFile() {
    if (!draft) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(draft, null, 2) + "\n"], {
        type: "application/json",
      }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "rulebook.json";
    anchor.click();
    URL.revokeObjectURL(url);
  }
  async function submit(kind: "preview" | "publish") {
    const scope = session.current;
    if (
      !scope ||
      !current(scope) ||
      operation.current ||
      (!view && !pending.current) ||
      !draft ||
      storageProblem
    )
      return;
    if (kind === "preview" && pending.current) return;
    if (kind === "publish" && !pending.current && !preview?.can_publish) return;
    const generation = editing.current;
    let command: Command;
    if (kind === "publish") {
      command = pending.current ?? {
        body: JSON.stringify({ preview_id: preview!.preview_id }),
        revision: preview!.project_revision,
        key: crypto.randomUUID(),
      };
      try {
        remember(scope, command, draft);
      } catch {
        setStorageProblem(
          "无法保存本标签页的发布身份，尚未发送新请求。请保留页面并检查浏览器存储。",
        );
        return;
      }
      pending.current = command;
    } else {
      const body = JSON.stringify(draft);
      const revision = view!.configuration.project_revision;
      if (
        previewCommand.current?.body !== body ||
        previewCommand.current.revision !== revision
      )
        previewCommand.current = { body, revision, key: crypto.randomUUID() };
      command = previewCommand.current;
      setPreview(null);
    }
    operation.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const response = await fetch(endpoint(scope, `rulebook/${kind}`), {
        method: "POST",
        body: command.body,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": scope.csrf,
          "If-Match": `"${command.revision}"`,
          "Idempotency-Key": command.key,
        },
      });
      if (!current(scope)) return;
      if (response.status === 401) {
        expire.current();
        throw new Error("会话已过期，请重新登录。");
      }
      if (response.status === 409) {
        if (kind === "publish") forget(scope, command);
        pending.current = null;
        previewCommand.current = null;
        setUncertain(false);
        setPreview(null);
        setView(null);
        await load(scope, false);
        if (current(scope))
          setError(
            "项目或预览版本已变化。已刷新当前版本，请检查差异并重新预览；已使用的规则版本号需要递增。",
          );
        return;
      }
      if (!response.ok) {
        if (kind === "publish" && response.status >= 500)
          throw new TypeError("Unknown publication");
        if (kind === "publish") forget(scope, command);
        pending.current = null;
        setUncertain(false);
        setPreview(null);
        throw new Error("操作未被接受，请检查规则后重新预览。");
      }
      const body = await response.json().catch(() => {
        throw new TypeError("Unknown response");
      });
      if (!current(scope) || generation !== editing.current) return;
      if (kind === "preview") {
        if (
          !object(body) ||
          typeof body.preview_id !== "string" ||
          !Number.isSafeInteger(body.project_revision) ||
          typeof body.can_publish !== "boolean" ||
          ![
            body.issues,
            body.compile_issues,
            body.warnings,
            body.waiting_reasons,
          ].every(Array.isArray)
        )
          throw new TypeError("Incomplete preview");
        setPreview(body as unknown as Preview);
      } else {
        if (
          !object(body) ||
          typeof body.publication_id !== "string" ||
          !object(body.rulebook) ||
          body.state !== "waiting_qualification" ||
          body.activation_allowed !== false ||
          !Number.isSafeInteger(body.project_revision)
        )
          throw new TypeError("Incomplete publication");
        forget(scope, command);
        pending.current = null;
        previewCommand.current = null;
        setUncertain(false);
        setPreview(null);
        setNotice(
          "规则版本已发布，等待执行资格验证。旧 Run 继续使用原先固定的规则版本。",
        );
        setView(null);
        await load(scope, true);
      }
    } catch (cause) {
      if (!current(scope)) return;
      if (kind === "publish" && pending.current) {
        setUncertain(true);
        setError(
          "尚未确认发布结果。请重试原发布请求，核对完成前保留这份编辑内容。",
        );
      } else
        setError(
          cause instanceof Error && !(cause instanceof TypeError)
            ? cause.message
            : "尚未取得结果，请重试。",
        );
    } finally {
      if (current(scope)) {
        operation.current = false;
        setBusy(false);
      }
    }
  }

  const original = readRulebook(view?.configuration.configuration?.rulebook);
  const profiles = view?.configuration.configuration?.resources?.profiles ?? [];
  const groups = Object.keys(draft?.profile_groups ?? {});
  const changes = differences(original, draft);
  const locked = busy || uncertain || !!storageProblem;
  return (
    <section className="rulebook-panel" aria-labelledby="rulebook-heading">
      <header className="rulebook-header">
        <div>
          <p className="eyebrow">调度规则</p>
          <h2 id="rulebook-heading">{project.name} · 调度规则</h2>
          <p>决定哪些任务交给哪些模型，发布前核对每一项变化。</p>
        </div>
        <button
          type="button"
          className="secondary"
          disabled={locked}
          onClick={refresh}
        >
          刷新当前版本
        </button>
      </header>
      <p className="rulebook-boundary">
        发布会保存供后续 Run 使用的版本；旧 Run
        保留原批准版本。本页不启动模型，真实执行资格仍需单独验证。
      </p>
      {error && (
        <p role="alert" className="rulebook-error">
          {error}
        </p>
      )}
      {storageProblem && (
        <p role="alert" className="rulebook-error">
          {storageProblem}
        </p>
      )}
      {notice && (
        <p role="status" className="rulebook-notice">
          {notice}
        </p>
      )}
      {busy && <p role="status">正在处理规则…</p>}
      {view && (
        <p>
          当前保存版本：
          {original ? `${original.id} · v${original.revision}` : "尚无规则"} ·
          项目修订 {view.configuration.project_revision}
        </p>
      )}
      <fieldset disabled={locked || !view} className="rulebook-editor">
        <div className="rulebook-actions">
          <label className="rulebook-import">
            导入完整规则文件
            <input
              aria-label="导入完整规则文件"
              type="file"
              accept=".json,application/json"
              onChange={importFile}
            />
          </label>
          <button
            type="button"
            className="secondary"
            disabled={!draft}
            onClick={exportFile}
          >
            导出当前编辑
          </button>
        </div>
        <p className="field-help">
          高级调整可导出文件后编辑并重新导入。全局约束与固定协作规则保持已确认值；仅填写配置引用。
        </p>
        {!draft && <p>还没有可编辑的规则，请导入完整规则文件。</p>}
        {draft && (
          <>
            <div className="form-grid">
              <label>
                规则标识
                <input value={draft.id} readOnly />
              </label>
              <label>
                编辑版本号
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={draft.revision}
                  onChange={(event) =>
                    edit({ ...draft, revision: Number(event.target.value) })
                  }
                />
              </label>
            </div>
            <label>
              版本说明
              <textarea
                rows={2}
                maxLength={8000}
                value={draft.description}
                onChange={(event) =>
                  edit({ ...draft, description: event.target.value })
                }
              />
            </label>
            <h3>任务分派矩阵</h3>
            <p className="field-help">
              优先级数值较高的匹配规则优先；候选组仍须通过资格与批准范围检查。
            </p>
            <div className="rulebook-table-scroll">
              <table className="rulebook-matrix">
                <thead>
                  <tr>
                    <th>任务条件与能力</th>
                    <th>优先级</th>
                    <th>候选组</th>
                    <th>质量升级组</th>
                  </tr>
                </thead>
                <tbody>
                  {draft.rules.map((rule, index) => {
                    function changeRule(
                      patch: Partial<
                        Pick<
                          Rule,
                          | "priority"
                          | "eligible_groups"
                          | "quality_escalation_groups"
                        >
                      >,
                    ) {
                      edit({
                        ...draft!,
                        rules: draft!.rules.map((item, position) =>
                          position === index ? { ...item, ...patch } : item,
                        ),
                      });
                    }
                    return (
                      <tr key={`${index}:${rule.id}`}>
                        <th scope="row">
                          <strong>{rule.id}</strong>
                          <p>
                            {Object.entries(rule.when)
                              .filter(([, value]) => value !== null)
                              .map(
                                ([key, value]) =>
                                  `${key}: ${typeof value === "string" ? (names[value] ?? value) : JSON.stringify(value)}`,
                              )
                              .join(" · ")}
                          </p>
                          <p>
                            能力：
                            {rule.capabilities_all.join("、") || "无额外要求"}
                          </p>
                          <details>
                            <summary>其他约束</summary>
                            <pre>
                              {JSON.stringify(
                                Object.fromEntries(
                                  Object.entries(rule).filter(
                                    ([key]) =>
                                      ![
                                        "id",
                                        "priority",
                                        "when",
                                        "capabilities_all",
                                        "eligible_groups",
                                        "quality_escalation_groups",
                                      ].includes(key),
                                  ),
                                ),
                                null,
                                2,
                              )}
                            </pre>
                          </details>
                        </th>
                        <td>
                          <input
                            aria-label={`${rule.id} 优先级`}
                            type="number"
                            min="-1000000"
                            max="1000000"
                            step="1"
                            value={rule.priority}
                            onChange={(event) =>
                              changeRule({
                                priority: Number(event.target.value),
                              })
                            }
                          />
                        </td>
                        <td>
                          {[
                            ...new Set([...groups, ...rule.eligible_groups]),
                          ].map((group) => (
                            <label className="rulebook-check" key={group}>
                              <input
                                type="checkbox"
                                aria-label={`${rule.id} 候选组 ${group}`}
                                checked={rule.eligible_groups.includes(group)}
                                onChange={() =>
                                  changeRule({
                                    eligible_groups: toggle(
                                      rule.eligible_groups,
                                      group,
                                    ),
                                  })
                                }
                              />
                              {group}
                            </label>
                          ))}
                        </td>
                        <td>
                          {[
                            ...new Set([
                              ...groups,
                              ...(rule.quality_escalation_groups ?? []),
                            ]),
                          ].map((group) => (
                            <label className="rulebook-check" key={group}>
                              <input
                                type="checkbox"
                                aria-label={`${rule.id} 升级组 ${group}`}
                                checked={
                                  rule.quality_escalation_groups?.includes(
                                    group,
                                  ) ?? false
                                }
                                onChange={() =>
                                  changeRule({
                                    quality_escalation_groups: toggle(
                                      rule.quality_escalation_groups ?? [],
                                      group,
                                    ),
                                  })
                                }
                              />
                              {group}
                              {rule.quality_escalation_groups?.includes(group)
                                ? ` · 第 ${rule.quality_escalation_groups.indexOf(group) + 1} 阶段`
                                : ""}
                            </label>
                          ))}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <h3>能力组成员</h3>
            <p className="field-help">
              成员从项目已登记配置中选择。加入能力组不代表已经获得执行资格；空组会等待配置。
            </p>
            <div className="rulebook-groups">
              {groups.map((group) => {
                const members = draft.profile_groups[group];
                const registered = new Set(
                  profiles.map(
                    (profile) => `${profile.id}:${profile.revision}`,
                  ),
                );
                return (
                  <section key={group} aria-label={`能力组 ${group}`}>
                    <h4>{group}</h4>
                    {members.length === 0 && (
                      <p className="rulebook-waiting">尚无成员 · 等待配置</p>
                    )}
                    {profiles.map((profile) => {
                      const checked = members.some(
                        (ref) =>
                          ref.id === profile.id &&
                          ref.revision === profile.revision,
                      );
                      return (
                        <label
                          className="rulebook-check"
                          key={`${profile.id}:${profile.revision}`}
                        >
                          <input
                            type="checkbox"
                            aria-label={`${group} 成员 ${profile.id} v${profile.revision}`}
                            checked={checked}
                            onChange={() =>
                              edit({
                                ...draft,
                                profile_groups: {
                                  ...draft.profile_groups,
                                  [group]: checked
                                    ? members.filter(
                                        (ref) =>
                                          ref.id !== profile.id ||
                                          ref.revision !== profile.revision,
                                      )
                                    : [
                                        ...members,
                                        {
                                          id: profile.id,
                                          revision: profile.revision,
                                        },
                                      ],
                                },
                              })
                            }
                          />
                          {profile.id} v{profile.revision}
                          {!profile.enabled && " · 已停用"}
                        </label>
                      );
                    })}
                    {members
                      .filter(
                        (ref) => !registered.has(`${ref.id}:${ref.revision}`),
                      )
                      .map((ref) => (
                        <p key={`${ref.id}:${ref.revision}`}>
                          {ref.id} v{ref.revision} · 未登记{" "}
                          <button
                            type="button"
                            className="secondary"
                            onClick={() =>
                              edit({
                                ...draft,
                                profile_groups: {
                                  ...draft.profile_groups,
                                  [group]: members.filter(
                                    (item) => item !== ref,
                                  ),
                                },
                              })
                            }
                          >
                            移除 {group} 中的 {ref.id} v{ref.revision}
                          </button>
                        </p>
                      ))}
                    {profiles.length === 0 && <p>项目尚无已登记配置。</p>}
                  </section>
                );
              })}
            </div>
            <h3>候选排序</h3>
            <p className="field-help">
              通过硬资格检查后，按以下顺序比较候选。配置标识固定在最后，用于稳定排序。
            </p>
            <ol className="rulebook-order">
              {draft.resource_policy.candidate_order.map(
                (item, index, order) => (
                  <li key={`${index}:${item}`}>
                    <span>{names[item] ?? item}</span>
                    {[-1, 1].map((direction) => (
                      <button
                        type="button"
                        className="secondary"
                        key={direction}
                        aria-label={`${direction < 0 ? "上移" : "下移"}${names[item] ?? item}`}
                        disabled={
                          index + direction < 0 ||
                          index + direction >= order.length ||
                          item === "profile_id" ||
                          order[index + direction] === "profile_id"
                        }
                        onClick={() => {
                          const next = [...order];
                          [next[index], next[index + direction]] = [
                            next[index + direction],
                            next[index],
                          ];
                          edit({
                            ...draft,
                            resource_policy: {
                              ...draft.resource_policy,
                              candidate_order: next,
                            },
                          });
                        }}
                      >
                        {direction < 0 ? "↑" : "↓"}
                      </button>
                    ))}
                  </li>
                ),
              )}
            </ol>
            <h3>协作上限</h3>
            <div className="form-grid">
              {Object.entries(limits).map(([key, label]) => (
                <label key={key}>
                  {label}
                  <input
                    type="number"
                    min={key === "max_parallel_writers_per_project" ? 1 : 0}
                    max={1000000000}
                    step="1"
                    value={Number(draft.collaboration[key] ?? 0)}
                    onChange={(event) =>
                      edit({
                        ...draft,
                        collaboration: {
                          ...draft.collaboration,
                          [key]: Number(event.target.value),
                        },
                      })
                    }
                  />
                </label>
              ))}
            </div>
            <details>
              <summary>核对固定限制、队列与其他资源策略</summary>
              <pre>
                {JSON.stringify(
                  {
                    collaboration: draft.collaboration,
                    global_constraints: draft.global_constraints,
                    resource_policy: draft.resource_policy,
                  },
                  null,
                  2,
                )}
              </pre>
            </details>
            <button
              type="button"
              disabled={
                !Number.isSafeInteger(draft.revision) ||
                draft.revision < 1 ||
                draft.rules.some((rule) => !Number.isSafeInteger(rule.priority))
              }
              onClick={() => void submit("preview")}
            >
              预览规则变更
            </button>
          </>
        )}
      </fieldset>
      <RoutingSimulation
        project={project}
        csrf={csrf}
        draft={draft}
        onSessionExpired={onSessionExpired}
      />
      {preview && (
        <section className="rulebook-preview" aria-label="发布预览">
          <h3>发布前核对</h3>
          <p>
            {preview.can_publish
              ? "结构检查通过，可以发布此版本。"
              : "当前规则尚不能发布，请处理以下问题。"}
          </p>
          <p>
            规则摘要：<code>{preview.rulebook_sha256 ?? "尚未生成"}</code>
          </p>
          <Issues title="配置待完善" items={preview.issues} />
          <Issues title="规则结构问题" items={preview.compile_issues} />
          <Issues title="提醒" items={preview.warnings} />
          <ul>
            {preview.waiting_reasons.map((reason) => (
              <li key={reason}>{names[reason] ?? reason}</li>
            ))}
          </ul>
          <h4>相对当前保存版本的差异</h4>
          {changes.length ? (
            <ul className="rulebook-differences">
              {changes.map((change) => (
                <li key={change.path}>
                  <strong>{change.path}</strong>
                  <div>
                    原值：<code>{change.before}</code>
                  </div>
                  <div>
                    新值：<code>{change.after}</code>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p>内容没有变化。</p>
          )}
          <button
            type="button"
            disabled={busy || uncertain || !preview.can_publish}
            onClick={() => void submit("publish")}
          >
            确认发布此版本
          </button>
        </section>
      )}
      {uncertain && (
        <p className="rulebook-boundary">
          本请求保留在同一标签页、同一登录会话中，页面刷新或切换项目后可恢复。请先核对结果再退出登录或关闭标签页；新登录可查看发布历史，但不会恢复旧会话的重试身份。
        </p>
      )}
      {uncertain && (
        <button
          type="button"
          disabled={busy || !!storageProblem}
          onClick={() => void submit("publish")}
        >
          重试原发布请求
        </button>
      )}
      {view && (
        <details className="rulebook-history">
          <summary>版本与发布历史（{view.versions.length} 个版本）</summary>
          <h3>已保存版本</h3>
          {view.versions.length ? (
            <ul>
              {view.versions.map((version) => (
                <li key={`${version.id}:${version.revision}`}>
                  {version.id} · v{version.revision}
                  <code>{version.rulebook_sha256}</code>
                </li>
              ))}
            </ul>
          ) : (
            <p>尚无版本记录。</p>
          )}
          <h3>发布记录</h3>
          {view.publications.length ? (
            <ul>
              {[...view.publications].reverse().map((publication) => (
                <li key={publication.publication_id}>
                  {publication.rulebook.id} · v{publication.rulebook.revision} ·{" "}
                  {new Date(publication.at * 1000).toLocaleString()} ·
                  等待执行资格验证
                </li>
              ))}
            </ul>
          ) : (
            <p>尚无发布记录。</p>
          )}
        </details>
      )}
    </section>
  );
}
