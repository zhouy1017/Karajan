import { useEffect, useRef, useState, type FormEvent } from "react";
import type { RunProject } from "./ProjectRuns";

type Configuration = {
  approved_profile_refs: { id: string; revision: number }[];
  rulebook: {
    profile_groups: { commander_qualified: { id: string; revision: number }[] };
    resource_policy: { run_budget_ref: string };
  };
  resources: {
    budgets: {
      id: string;
      currency_limits: Record<string, string | null>;
      max_total_attempts: number;
      max_duration_seconds: number;
    }[];
  };
};
type FormProps = {
  project: RunProject;
  csrf: string;
  onSaved: (id: string) => void;
};
type PendingCommand = { body: string; key: string };

export function NewRunForm(props: FormProps) {
  return <RunDraft key={`${props.csrf}:${props.project.id}`} {...props} />;
}

function RunDraft({ project, csrf, onSaved }: FormProps) {
  const [configuration, setConfiguration] = useState<Configuration | null>(
    null,
  );
  const [goal, setGoal] = useState("");
  const [acceptance, setAcceptance] = useState("");
  const [commander, setCommander] = useState("");
  const [readPaths, setReadPaths] = useState("");
  const [writePaths, setWritePaths] = useState("");
  const [checks, setChecks] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [storageKey, setStorageKey] = useState<string | null>(null);
  const [storageError, setStorageError] = useState("");
  const [pendingScope, setPendingScope] = useState("");
  const command = useRef<PendingCommand | null>(null);
  const sending = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    let active = true;
    // SessionStore keeps CSRF stable for one authenticated owner session.
    // Persist only its digest; a new login gets a separate pending namespace.
    Promise.resolve()
      .then(() =>
        crypto.subtle.digest("SHA-256", new TextEncoder().encode(csrf)),
      )
      .then((digest) => {
        const scope = Array.from(new Uint8Array(digest), (byte) =>
          byte.toString(16).padStart(2, "0"),
        ).join("");
        const key = `karajan.pending-run.v1:${scope}:${encodeURIComponent(project.id)}`;
        const stored = sessionStorage.getItem(key);
        const restored =
          stored === null ? null : readPending(stored, project.id);
        if (!active) return;
        if (restored) {
          command.current = restored.command;
          setGoal(restored.goal);
          setAcceptance(restored.acceptance.join("\n"));
          setReadPaths(restored.readPaths.join("\n"));
          setWritePaths(restored.writePaths.join("\n"));
          setChecks(restored.checks.join("\n"));
          setCommander("pending");
          setPendingScope(restored.scope);
          setPending(true);
        }
        setStorageKey(key);
      })
      .catch(() => {
        if (active)
          setStorageError(
            "无法读取本标签页的待核对记录，暂不能发送需求。请保留本页面并核对已有需求。",
          );
      });
    return () => {
      active = false;
    };
  }, [csrf, project.id]);
  useEffect(() => {
    let active = true;
    if (project.configuration.status !== "offline_valid") return;
    fetch(`/v1/projects/${encodeURIComponent(project.id)}/configuration`)
      .then(async (response) => {
        if (!response.ok) throw new Error();
        const saved = await response.json();
        if (saved.project_revision !== project.revision) throw new Error();
        if (active) setConfiguration(saved.configuration);
      })
      .catch(() => {
        if (active) setError("无法读取当前配置，请重新打开项目。");
      });
    return () => {
      active = false;
    };
  }, [project.id, project.revision, project.configuration.status]);
  const budget = configuration?.resources.budgets.find(
    (item) => item.id === configuration.rulebook.resource_policy.run_budget_ref,
  );
  async function save(event: FormEvent) {
    event.preventDefault();
    if (sending.current || !storageKey || storageError) return;
    let current = command.current;
    if (!current) {
      if (!configuration || !budget || commander === "") return;
      const profile =
        configuration.rulebook.profile_groups.commander_qualified[
          Number(commander)
        ];
      if (!profile) return;
      const body = JSON.stringify({
        project_id: project.id,
        project_revision: project.revision,
        configuration_digest: project.configuration.digest,
        requirement: { goal, acceptance: lines(acceptance) },
        participants: [{ principal: "lead", profile, purpose: "lead" }],
        authorization: {
          profile_refs: configuration.approved_profile_refs,
          read_paths: lines(readPaths),
          write_paths: lines(writePaths),
          budget_ref: budget.id,
          checks: [...new Set(["independent_review", ...lines(checks)])],
          delivery: "pull_request",
          target_branch: project.target_branch,
        },
      });
      current = { body, key: crypto.randomUUID() };
    }
    try {
      const serialized = JSON.stringify({ version: 1, ...current });
      const existing = sessionStorage.getItem(storageKey);
      if (existing !== null) {
        const recorded = readPending(existing, project.id).command;
        if (recorded.key !== current.key || recorded.body !== current.body)
          throw new Error();
      }
      const recorded = readPending(serialized, project.id);
      sessionStorage.setItem(storageKey, serialized);
      if (sessionStorage.getItem(storageKey) !== serialized) throw new Error();
      setPendingScope(recorded.scope);
    } catch {
      setStorageError(
        "无法保存本标签页的请求身份，尚未发送新请求。请保留页面并检查浏览器存储。",
      );
      return;
    }
    command.current = current;
    setPending(true);
    sending.current = true;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/v1/runs", {
        method: "POST",
        body: current.body,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": current.key,
        },
      });
      if (response.status === 409)
        throw new Error(
          "原请求与当前项目状态不一致，已保留原请求。请核对已有需求和项目配置。",
        );
      if (!response.ok)
        throw new Error(
          "服务尚未确认保存成功，已保留原请求。请核对已有需求和授权范围。",
        );
      const run = await response.json();
      if (typeof run?.id !== "string" || !run.id.trim())
        throw new Error("尚未确认保存结果，可核对同一操作。");
      const stored = sessionStorage.getItem(storageKey);
      if (stored !== null) {
        const recorded = readPending(stored, project.id).command;
        if (recorded.key === current.key && recorded.body === current.body)
          sessionStorage.removeItem(storageKey);
      }
      command.current = null;
      setPending(false);
      if (mounted.current) onSaved(run.id);
    } catch (cause) {
      setError(
        cause instanceof TypeError
          ? "尚未确认保存结果，可重试同一操作。"
          : cause instanceof Error
            ? cause.message
            : "保存失败，请重试。",
      );
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }
  if (
    project.configuration.status !== "offline_valid" &&
    !pending &&
    storageKey
  )
    return (
      <p className="muted">先补齐项目配置，再保存带有执行范围的新需求。</p>
    );
  return (
    <form onSubmit={save} className="run-create">
      <h3>新需求</h3>
      {pending && (
        <p role="status" className="notice">
          这次保存尚待核对。内容已锁定，只会重试原请求。{pendingScope}{" "}
          本记录仅保留在同一标签页、同一登录会话中；请先核对结果再关闭标签页。
        </p>
      )}
      <fieldset
        disabled={
          busy || pending || !configuration || !storageKey || !!storageError
        }
      >
        <label htmlFor="run-goal">希望完成什么</label>
        <textarea
          id="run-goal"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          required
          maxLength={8000}
        />
        <label htmlFor="run-acceptance">验收标准（每行一条）</label>
        <textarea
          id="run-acceptance"
          value={acceptance}
          onChange={(e) => setAcceptance(e.target.value)}
          required
        />
        <label htmlFor="run-commander">主 Commander</label>
        <select
          id="run-commander"
          value={commander}
          onChange={(e) => setCommander(e.target.value)}
          required
        >
          <option value="">请选择配置</option>
          {pending && commander === "pending" && (
            <option value="pending">沿用原请求的 Commander</option>
          )}
          {configuration?.rulebook.profile_groups.commander_qualified.map(
            (profile, index) => (
              <option key={index} value={index}>
                {profile.id} · 版本 {profile.revision}
              </option>
            ),
          )}
        </select>
        <div className="form-grid">
          <div>
            <label htmlFor="run-read">允许读取的路径（每行一条）</label>
            <textarea
              id="run-read"
              value={readPaths}
              onChange={(e) => setReadPaths(e.target.value)}
              required
            />
          </div>
          <div>
            <label htmlFor="run-write">允许修改的路径（每行一条）</label>
            <textarea
              id="run-write"
              value={writePaths}
              onChange={(e) => setWritePaths(e.target.value)}
            />
          </div>
        </div>
        <label htmlFor="run-checks">其他必需检查名称（每行一条）</label>
        <textarea
          id="run-checks"
          value={checks}
          onChange={(e) => setChecks(e.target.value)}
        />
        {!pending && (
          <p className="field-help">
            独立审查始终必需。目标分支：{project.target_branch}
            ；最终合并由你决定。
          </p>
        )}
        {!pending && (
          <p className="field-help">
            本次沿用项目批准的模型配置：
            {configuration?.approved_profile_refs
              .map((ref) => `${ref.id}（版本 ${ref.revision}）`)
              .join("、")}
            。实际任务分配以待确认计划为准。
          </p>
        )}
        {!pending && budget && (
          <p className="field-help">
            执行预算：
            {Object.entries(budget.currency_limits)
              .map(([currency, limit]) => `${currency} ${limit ?? "未确定"}`)
              .join("，")}
            ；最多 {budget.max_total_attempts} 次尝试、
            {budget.max_duration_seconds} 秒。
          </p>
        )}
        <p className="field-help">
          这里只保存需求与范围。计划需另行确认；真实调用还需通过资格和资源检查。
        </p>
      </fieldset>
      <button
        disabled={
          busy || !storageKey || !!storageError || (!pending && !budget)
        }
      >
        {pending ? "核对保存结果" : "保存需求"}
      </button>
      {(storageError || error) && (
        <p role="alert" className="notice error">
          {storageError || error}
        </p>
      )}
    </form>
  );
}
function lines(value: string) {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error();
  return value as Record<string, unknown>;
}
function text(value: unknown): string {
  if (typeof value !== "string") throw new Error();
  return value;
}
function textList(value: unknown): string[] {
  if (!Array.isArray(value)) throw new Error();
  return value.map(text);
}
function readPending(serialized: string, projectId: string) {
  if (serialized.length > 4 * 1024 * 1024) throw new Error();
  const record = object(JSON.parse(serialized));
  const key = text(record.key);
  if (
    record.version !== 1 ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      key,
    )
  )
    throw new Error();
  const body = text(record.body);
  const payload = object(JSON.parse(body));
  if (
    payload.project_id !== projectId ||
    !Number.isSafeInteger(payload.project_revision) ||
    Number(payload.project_revision) < 1
  )
    throw new Error();
  const requirement = object(payload.requirement);
  const authorization = object(payload.authorization);
  if (!Array.isArray(payload.participants) || payload.participants.length !== 1)
    throw new Error();
  const participant = object(payload.participants[0]);
  const profile = object(participant.profile);
  if (!Number.isSafeInteger(profile.revision) || Number(profile.revision) < 1)
    throw new Error();
  const profileId = text(profile.id);
  const budget = text(authorization.budget_ref);
  const branch = text(authorization.target_branch);
  return {
    command: { key, body },
    goal: text(requirement.goal),
    acceptance: textList(requirement.acceptance),
    readPaths: textList(authorization.read_paths),
    writePaths: textList(authorization.write_paths),
    checks: textList(authorization.checks),
    scope: `原项目版本 ${payload.project_revision}；Commander ${profileId}（版本 ${profile.revision}）；固定预算 ${budget}；目标分支 ${branch}。`,
  };
}
