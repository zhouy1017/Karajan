import { useEffect, useRef, useState } from "react";
import { NewRunForm } from "./NewRunForm";
import { readRoutingScope, RoutingAuthorization } from "./RoutingAuthorization";

export type RunProject = {
  id: string;
  name: string;
  revision: number;
  target_branch: string;
  configuration: { status: string; digest?: string | null };
};
type Plan = {
  routing_digest?: string;
  routing_binding?: unknown;
  term: number;
  plan_revision: number;
  plan_digest: string;
  authorization_digest: string;
  configuration_digest: string;
  plan: {
    summary: string;
    authorization: {
      profile_refs: { id: string; revision: number }[];
      read_paths: string[];
      write_paths: string[];
      checks: string[];
      budget_ref: string;
      delivery: string;
      target_branch: string;
    };
    tasks: {
      id: string;
      role: string;
      readiness: string;
      complexity: string;
      risk: string;
      paths: string[];
      depends_on: string[];
      required: boolean;
      acceptance: string[];
    }[];
  };
};
type Run = {
  schema_version?: string;
  id: string;
  requirement: { goal: string; acceptance: string[] };
  commander: { term: number; principal: string };
  active_plan_revision: number | null;
  state: string;
  dispatch_enabled: boolean;
  plans: Plan[];
  handoffs: Handoff[];
  configuration_snapshot?: {
    configuration: {
      resources: {
        budgets: {
          id: string;
          currency_limits: Record<string, string | null>;
          max_total_attempts: number;
          max_duration_seconds: number;
        }[];
      };
    };
  };
};
type Handoff = {
  id: string;
  digest: string;
  binding: { term: number };
  candidate: {
    principal: string;
    profile?: { id: string; revision: number };
  };
  checkpoint: { summary: string; artifacts: { ref: string; sha256: string }[] };
  resource_impact: { summary: string; budget_ref: string };
  expires_at: number;
  state: string;
};

export function ProjectRuns({
  project,
  csrf,
}: {
  project: RunProject;
  csrf: string;
}) {
  return (
    <RunWorkbench key={`${csrf}:${project.id}`} project={project} csrf={csrf} />
  );
}

function RunWorkbench({
  project,
  csrf,
}: {
  project: RunProject;
  csrf: string;
}) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [loadingRuns, setLoadingRuns] = useState(true);
  const [selected, setSelected] = useState<Run | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [reviewEpoch, setReviewEpoch] = useState(0);
  const command = useRef<{ identity: string; key: string } | null>(null);
  const reading = useRef(0);
  const sending = useRef(false);
  const lifetime = useRef({ active: true });

  useEffect(() => {
    let active = true;
    const session = { active: true };
    lifetime.current = session;
    setSelected(null);
    setLoadingRuns(true);
    setRuns([]);
    setError("");
    setNotice("");
    fetch(`/v1/runs?project_id=${encodeURIComponent(project.id)}`)
      .then(async (response) => {
        if (!response.ok) throw new Error("无法读取需求，请重试。");
        const result = await response.json();
        if (active) setRuns(result.items);
      })
      .catch(() => {
        if (active) setError("无法读取需求，请重试。");
      })
      .finally(() => {
        if (active) setLoadingRuns(false);
      });
    return () => {
      active = false;
      session.active = false;
      reading.current += 1;
    };
  }, [project.id]);

  async function openRun(id: string) {
    const generation = ++reading.current;
    const session = lifetime.current;
    const current = () => session.active && reading.current === generation;
    // A read invalidates the previous decision even if the replacement read fails.
    setSelected(null);
    setReviewEpoch(generation);
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const response = await fetch(`/v1/runs/${encodeURIComponent(id)}`);
      if (!response.ok) throw new Error("无法读取当前计划，请重试。");
      const result = await response.json();
      if (!current()) return false;
      if (
        result.id !== id ||
        (result.project_id !== undefined && result.project_id !== project.id)
      )
        throw new Error("计划与当前项目不一致，请重新打开需求。");
      setSelected(result);
      return true;
    } catch (cause) {
      if (current())
        setError(cause instanceof Error ? cause.message : "无法读取计划。");
      return false;
    } finally {
      if (current()) setBusy(false);
    }
  }

  async function approvePlan(plan: Plan) {
    if (!selected) return;
    const versionTwo = selected.schema_version === "karajan.run-planning.v2";
    const scope = versionTwo
      ? readRoutingScope(selected, plan, project.id)
      : null;
    if (
      versionTwo
        ? !scope
        : selected.schema_version !== "karajan.run-planning.v1" ||
          plan.routing_binding !== undefined ||
          plan.routing_digest !== undefined
    )
      return;
    const payload = {
      ...(scope
        ? {
            schema_version: "karajan.approve-plan.v2",
            routing_digest: scope.digest,
          }
        : {}),
      term: plan.term,
      plan_revision: plan.plan_revision,
      plan_digest: plan.plan_digest,
      authorization_digest: plan.authorization_digest,
      configuration_digest: plan.configuration_digest,
    };
    await decide(
      "plan-approval",
      payload,
      versionTwo
        ? "v2 授权已确认；尚未启用实际派发。"
        : "计划已确认；执行仍需满足运行资格。",
    );
  }

  async function decideHandoff(
    handoff: Handoff,
    decision: "approve" | "reject",
  ) {
    await decide(
      "handoff-decision",
      {
        term: handoff.binding.term,
        handoff_id: handoff.id,
        handoff_digest: handoff.digest,
        decision,
      },
      decision === "approve"
        ? "已确认交接；新 Commander 的调用仍需满足运行资格。"
        : "已拒绝本次交接，继续保留当前 Commander。",
    );
  }

  async function decide(action: string, payload: object, success: string) {
    if (!selected || sending.current) return;
    sending.current = true;
    const session = lifetime.current;
    const generation = reading.current;
    setBusy(true);
    setError("");
    setNotice("");
    const endpoint = `/v1/runs/${encodeURIComponent(selected.id)}/${action}`;
    const identity = endpoint + JSON.stringify(payload);
    if (command.current?.identity !== identity)
      command.current = { identity, key: crypto.randomUUID() };
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        body: JSON.stringify(payload),
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": command.current.key,
        },
      });
      if (!session.active || reading.current !== generation) return;
      if (response.status === 409) {
        const reason = await response.json().catch(() => ({}));
        const refreshed = await openRun(selected.id);
        command.current = null;
        if (!refreshed)
          throw new Error(
            "方案已有变化，当前版本尚未读取成功；暂不能确认，请重新打开需求。",
          );
        if (reason.reason_code === "RUN_PROTOCOL_VERSION_MISMATCH")
          throw new Error(
            "授权协议与计划不匹配，已重新读取。请核对当前版本后再决定。",
          );
        throw new Error("方案已有变化，已重新读取。请审阅当前版本后再决定。");
      }
      if (response.status === 422) {
        await openRun(selected.id);
        command.current = null;
        throw new Error(
          "授权请求未被接受，请核对重新读取的版本与范围后再决定。",
        );
      }
      if (!response.ok) throw new Error("尚未确认批准结果，可重试同一操作。");
      const refreshed = await openRun(selected.id);
      command.current = null;
      if (session.active)
        setNotice(
          refreshed
            ? success
            : "决定已保存，但当前计划尚未读取成功。请重新打开需求。",
        );
    } catch (cause) {
      if (session.active)
        setError(
          cause instanceof Error
            ? cause.message
            : "尚未确认批准结果，可重试同一操作。",
        );
    } finally {
      sending.current = false;
      if (session.active) setBusy(false);
    }
  }

  const plan = selected?.plans.at(-1);
  const versionTwo = selected?.schema_version === "karajan.run-planning.v2";
  const versionOne =
    selected?.schema_version === "karajan.run-planning.v1" &&
    plan?.routing_binding === undefined &&
    plan?.routing_digest === undefined;
  const routingScope =
    selected && plan && versionTwo
      ? readRoutingScope(selected, plan, project.id)
      : null;
  const budget =
    selected?.configuration_snapshot?.configuration.resources.budgets.find(
      (item) => item.id === plan?.plan.authorization.budget_ref,
    );
  const handoff = selected?.handoffs.find(
    (item) =>
      item.state === "pending" && item.binding.term === selected.commander.term,
  );
  const planningBudget =
    selected?.configuration_snapshot?.configuration.resources.budgets.find(
      (item) => item.id === handoff?.resource_impact.budget_ref,
    );
  return (
    <section className="project-form">
      <h2>{project.name} · 需求与计划</h2>
      <button
        className="secondary"
        disabled={busy}
        onClick={() => setShowCreate(!showCreate)}
      >
        {showCreate ? "收起新需求" : "新建需求"}
      </button>
      {showCreate && (
        <NewRunForm
          project={project}
          csrf={csrf}
          onSaved={(id) => {
            setShowCreate(false);
            void openRun(id);
            void fetch(`/v1/runs?project_id=${encodeURIComponent(project.id)}`)
              .then((response) =>
                response.ok ? response.json() : Promise.reject(),
              )
              .then((value) => setRuns(value.items))
              .catch(() =>
                setError("需求已保存，但列表尚未刷新。请重新打开项目。"),
              );
          }}
        />
      )}
      <p className="field-help">
        查看已保存需求和当前方案。真实模型执行尚未启用。
      </p>
      {loadingRuns ? (
        <p role="status" className="muted">
          正在读取已保存需求…
        </p>
      ) : runs.length === 0 ? (
        !error && <p className="muted">这个项目还没有需求。</p>
      ) : (
        <div className="form-actions">
          {runs.map((run) => (
            <button
              className="secondary"
              key={run.id}
              disabled={busy}
              onClick={() => void openRun(run.id)}
            >
              {run.requirement.goal}
            </button>
          ))}
        </div>
      )}
      {selected && (
        <article className="run-detail">
          <h3>{selected.requirement.goal}</h3>
          {handoff && (
            <section className="preview-result">
              <h3>Commander 交接提案</h3>
              <p>候选 · {handoff.candidate.principal}</p>
              <p>
                模型配置：
                {handoff.candidate.profile
                  ? `${handoff.candidate.profile.id}（版本 ${handoff.candidate.profile.revision}）`
                  : "尚未取得固定版本，暂不能确认"}
              </p>
              <h4>检查点材料</h4>
              <p>{handoff.checkpoint.summary}</p>
              {handoff.checkpoint.artifacts.length ? (
                <ul>
                  {handoff.checkpoint.artifacts.map((artifact, index) => (
                    <li key={index}>
                      <span>{artifact.ref}</span>
                      <br />
                      <code>{artifact.sha256}</code>
                    </li>
                  ))}
                </ul>
              ) : (
                <p>提案未附检查点文件。</p>
              )}
              <h4>资源材料</h4>
              <p>提案说明：{handoff.resource_impact.summary}</p>
              {planningBudget ? (
                <p>
                  规划预算上限：
                  {Object.entries(planningBudget.currency_limits)
                    .map(
                      ([currency, value]) => `${currency} ${value ?? "未确定"}`,
                    )
                    .join("，")}
                  ；{planningBudget.max_total_attempts} 次尝试、
                  {planningBudget.max_duration_seconds} 秒。
                </p>
              ) : (
                <p>尚未取得固定规划预算，暂不能确认。</p>
              )}
              <p className="field-help">
                当前预算余量和检查点文件内容尚未核对。此处确认只保存你的交接决定，实际模型调用仍需运行资格与资源核验。
              </p>
              <p className="field-help">
                有效至{" "}
                {new Date(handoff.expires_at * 1000).toLocaleString("zh-CN")}
                。确认前保持当前 Commander。
              </p>
              {handoff.expires_at * 1000 > Date.now() ? (
                <div className="form-actions">
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() => void decideHandoff(handoff, "reject")}
                  >
                    保留当前 Commander
                  </button>
                  <button
                    disabled={
                      busy || !handoff.candidate.profile || !planningBudget
                    }
                    onClick={() => void decideHandoff(handoff, "approve")}
                  >
                    确认交接
                  </button>
                </div>
              ) : (
                <p>这份交接提案已过期。</p>
              )}
            </section>
          )}
          <ul>
            {selected.requirement.acceptance.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
          <p className="field-help">
            主 Commander · {selected.commander.principal} · 第{" "}
            {selected.commander.term} 任
          </p>
          {plan ? (
            <>
              <h3>计划第 {plan.plan_revision} 版</h3>
              {selected.active_plan_revision === plan.plan_revision && (
                <p className="notice success">
                  这份计划已确认，等待满足执行条件。
                </p>
              )}
              <p>{plan.plan.summary}</p>
              {versionTwo ? (
                routingScope ? (
                  <RoutingAuthorization
                    key={`${selected.id}:${plan.plan_digest}:${plan.routing_digest}:${reviewEpoch}`}
                    scope={routingScope}
                    busy={busy}
                    canApprove={
                      selected.active_plan_revision !== plan.plan_revision &&
                      plan.term === selected.commander.term &&
                      !!budget
                    }
                    onApprove={() => void approvePlan(plan)}
                  />
                ) : (
                  <p role="alert" className="notice error">
                    v2
                    授权材料不完整或固定版本不一致，暂不能确认。请重新读取计划。
                  </p>
                )
              ) : !versionOne ? (
                <p role="alert" className="notice error">
                  无法识别这份计划的授权协议，暂不能确认。
                </p>
              ) : (
                <p className="field-help">授权协议 v1 · 原有计划范围</p>
              )}
              {!versionTwo && (
                <>
                  <section className="preview-result">
                    <h4>本次授权范围</h4>
                    <p>
                      允许读取：{plan.plan.authorization.read_paths.join("、")}
                    </p>
                    <p>
                      允许修改：
                      {plan.plan.authorization.write_paths.join("、") || "无"}
                    </p>
                    <p>
                      允许的模型配置：
                      {plan.plan.authorization.profile_refs
                        .map((ref) => `${ref.id}（版本 ${ref.revision}）`)
                        .join("、")}
                    </p>
                    <p>
                      必需检查：
                      {plan.plan.authorization.checks
                        .map((check) =>
                          check === "independent_review" ? "独立审查" : check,
                        )
                        .join("、")}
                    </p>
                    <p>
                      交付：
                      {plan.plan.authorization.delivery === "pull_request"
                        ? `向 ${plan.plan.authorization.target_branch} 创建 PR；合并由你决定`
                        : "暂不交付 PR"}
                    </p>
                    {budget ? (
                      <p>
                        预算上限：
                        {Object.entries(budget.currency_limits)
                          .map(
                            ([currency, value]) =>
                              `${currency} ${value ?? "未确定"}`,
                          )
                          .join("，")}
                        ；{budget.max_total_attempts} 次尝试、
                        {budget.max_duration_seconds} 秒。
                      </p>
                    ) : (
                      <p>尚未取得这份计划的固定预算，暂不能确认。</p>
                    )}
                  </section>
                  <div className="project-grid">
                    {plan.plan.tasks.map((task) => (
                      <article key={task.id} className="project-card">
                        <h4>{task.id}</h4>
                        <span>
                          {task.role === "worker"
                            ? "实现"
                            : task.role === "reviewer"
                              ? "审查"
                              : "规划"}{" "}
                          · {task.complexity}
                          {task.risk === "critical" ? " · 高风险" : ""}
                        </span>
                        <p>
                          {task.required ? "必需任务" : "可选任务"}
                          {task.readiness === "T0"
                            ? " · 仍待澄清，不能执行"
                            : ""}
                        </p>
                        <p>前置任务：{task.depends_on.join("、") || "无"}</p>
                        <p>{task.paths.join("、")}</p>
                        <ul>
                          {task.acceptance.map((item, index) => (
                            <li key={index}>{item}</li>
                          ))}
                        </ul>
                      </article>
                    ))}
                  </div>
                </>
              )}
              {versionOne &&
                selected.active_plan_revision !== plan.plan_revision &&
                plan.term === selected.commander.term &&
                budget && (
                  <button
                    disabled={busy}
                    onClick={() => void approvePlan(plan)}
                  >
                    确认这份计划
                  </button>
                )}
            </>
          ) : (
            <p className="muted">需求已保存，尚未收到 Commander 的计划。</p>
          )}
        </article>
      )}
      {busy && !selected && (
        <p role="status" className="muted">
          正在读取当前计划…
        </p>
      )}
      {notice && (
        <p role="status" className="notice success">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="notice error">
          {error}
        </p>
      )}
    </section>
  );
}
