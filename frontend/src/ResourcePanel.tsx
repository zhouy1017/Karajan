import { useEffect, useRef, useState, type FormEvent } from "react";
import "./ResourcePanel.css";

export type ResourcePolicy = {
  account_id: string;
  max_active_attempts: number;
  max_attempt_duration_seconds: number;
  observation_max_age_seconds: number;
  require_official_observation: boolean;
  safety_margin: Record<string, string>;
  lead_reserve: Record<string, string>;
  lead_reserved_slots: number;
  conservative_mode: {
    enabled: boolean;
    max_local_active_attempts?: number | null;
    max_attempt_duration_seconds?: number | null;
    observation_max_age_seconds?: number | null;
    cooldown_seconds?: number | null;
  } | null;
};

export type ResourcePool = {
  id: string;
  kind: "service" | "platform_allowance";
  unit: "requests" | "percent" | "tokens";
  window_kind: string;
  window_id: string | null;
  reported_remaining: string | null;
  reported_limit: string | null;
  local_uncovered: string;
  future_reserved: string;
  safety_margin: string;
  lead_reserve: string;
  available_for_worker: string | null;
  available_for_lead: string | null;
  source: string | null;
  observed_at: number | null;
  received_at: number | null;
  reset_at: number | null;
  status: "unknown" | "stale" | "observed" | "unconfigured";
  coverage_status: "uncertain" | "explicit_coverage" | "no_local_usage";
  covered_usage_count: number;
};

export type ResourceAccount = {
  id: string;
  policy_revision: number | null;
  policy: ResourcePolicy | null;
  active_attempts: number;
  waiting_reconciliation: number;
  blockers?: { reason_code: string; until: number | null }[];
  pools: ResourcePool[];
};

export type ResourceView = {
  schema_version: "karajan.resources.view.v1";
  observed_at: number;
  accounts: ResourceAccount[];
  live_qualification: "not_run";
  activation_allowed: false;
};

type Draft = {
  account: ResourceAccount;
  revision: number;
  reserve: Record<string, string>;
  slots: string;
};

type SessionScope = { csrf: string; active: boolean };

const statusNames = {
  observed: "已取得报告",
  unknown: "额度未知",
  stale: "报告已过期",
  unconfigured: "尚未配置",
};

function timeLabel(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "未知";
  return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
}

function amount(value: string | null, unit: ResourcePool["unit"]) {
  if (value === null) return "未知";
  const compact = value.includes(".") ? value.replace(/\.?0+$/, "") : value;
  return `${compact} ${unit === "requests" ? "次" : unit === "percent" ? "%" : "token"}`;
}

function quotaUnits(value: string): bigint {
  const [whole, fraction = ""] = value.split(".");
  return BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(6, "0"));
}

function PoolCard({ pool }: { pool: ResourcePool }) {
  const current = pool.status === "observed";
  const source =
    {
      official: "官方报告",
      fixture: "本机样例",
      manual: "人工校准",
      local_ledger: "本地记录",
    }[pool.source ?? ""] ?? "来源未知";
  return (
    <article className="resource-pool" aria-label={`${pool.id} 额度详情`}>
      <div className="resource-pool-heading">
        <div>
          <span className="resource-eyebrow">
            {pool.kind === "service" ? "服务额度" : "平台分配额度"}
          </span>
          <h4>{pool.id}</h4>
        </div>
        <span className={`resource-badge resource-badge-${pool.status}`}>
          {statusNames[pool.status]}
        </span>
      </div>
      <dl className="resource-availability">
        <div>
          <dt>Worker 估算可用</dt>
          <dd>
            {current
              ? amount(pool.available_for_worker, pool.unit)
              : "暂不可估算"}
          </dd>
        </div>
        <div>
          <dt>主 Commander 估算可用</dt>
          <dd>
            {current
              ? amount(pool.available_for_lead, pool.unit)
              : "暂不可估算"}
          </dd>
        </div>
      </dl>
      <dl className="resource-facts">
        <div>
          <dt>已报告池上限</dt>
          <dd>
            {pool.reported_limit === null
              ? "上限未知"
              : amount(pool.reported_limit, pool.unit)}
          </dd>
        </div>
        <div>
          <dt>上次报告剩余</dt>
          <dd>{amount(pool.reported_remaining, pool.unit)}</dd>
        </div>
        <div>
          <dt>本地待核对用量</dt>
          <dd>{amount(pool.local_uncovered, pool.unit)}</dd>
        </div>
        <div>
          <dt>已预留的未来用量</dt>
          <dd>{amount(pool.future_reserved, pool.unit)}</dd>
        </div>
        <div>
          <dt>安全余量</dt>
          <dd>{amount(pool.safety_margin, pool.unit)}</dd>
        </div>
        <div>
          <dt>Commander 保护量</dt>
          <dd>{amount(pool.lead_reserve, pool.unit)}</dd>
        </div>
      </dl>
      <p className="resource-coverage">
        {pool.coverage_status === "uncertain"
          ? "报告与本地用量尚未完全核对，可用量按保守方式估算。"
          : pool.coverage_status === "explicit_coverage"
            ? `报告已明确包含 ${pool.covered_usage_count} 条本地用量。`
            : "尚无需要与报告核对的本地用量。"}
      </p>
      <dl className="resource-metadata">
        <div>
          <dt>来源</dt>
          <dd>{source}</dd>
        </div>
        <div>
          <dt>报告时间</dt>
          <dd>{timeLabel(pool.observed_at)}</dd>
        </div>
        <div>
          <dt>接收时间</dt>
          <dd>{timeLabel(pool.received_at)}</dd>
        </div>
        <div>
          <dt>预计重置</dt>
          <dd>{timeLabel(pool.reset_at)}</dd>
        </div>
        <div>
          <dt>额度窗口</dt>
          <dd>{pool.window_id ?? "未知"}</dd>
        </div>
      </dl>
    </article>
  );
}

function editable(account: ResourceAccount): Draft | null {
  if (!account.policy || account.policy_revision === null) return null;
  return {
    account,
    revision: account.policy_revision,
    reserve: { ...account.policy.lead_reserve },
    slots: String(account.policy.lead_reserved_slots),
  };
}

export function ResourcePanel({
  csrf,
  onSessionExpired,
}: {
  csrf: string;
  onSessionExpired: () => void;
}) {
  const [view, setView] = useState<ResourceView | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const commands = useRef(new Map<string, string>());
  const session = useRef<SessionScope | null>(null);
  const renderedCsrf = useRef(csrf);
  renderedCsrf.current = csrf;
  const sessionExpired = useRef(onSessionExpired);
  sessionExpired.current = onSessionExpired;

  function isCurrent(scope: SessionScope) {
    return (
      scope.active &&
      session.current === scope &&
      renderedCsrf.current === scope.csrf
    );
  }

  async function readView(scope: SessionScope): Promise<ResourceView | null> {
    const response = await fetch("/v1/resources").catch(() => {
      throw new Error("无法连接本地工作台，请重试。");
    });
    if (!isCurrent(scope)) return null;
    if (response.status === 401) {
      sessionExpired.current();
      throw new Error("会话已过期，请重新登录。");
    }
    if (!response.ok) throw new Error("暂时无法读取额度，请重试。");
    const next: ResourceView = await response.json();
    return isCurrent(scope) ? next : null;
  }

  useEffect(() => {
    const scope = { csrf, active: true };
    session.current = scope;
    commands.current.clear();
    setView(null);
    setDraft(null);
    setSaving(false);
    setError("");
    setNotice("");
    setLoading(true);
    readView(scope)
      .then((next) => {
        if (isCurrent(scope)) setView(next);
      })
      .catch((cause: unknown) => {
        if (isCurrent(scope))
          setError(
            cause instanceof Error
              ? cause.message
              : "暂时无法读取额度，请重试。",
          );
      })
      .finally(() => {
        if (isCurrent(scope)) setLoading(false);
      });
    return () => {
      scope.active = false;
      if (session.current === scope) session.current = null;
    };
  }, [csrf]);

  async function refresh() {
    const scope = session.current;
    if (!scope || !isCurrent(scope)) return;
    setLoading(true);
    setError("");
    setDraft(null);
    try {
      const next = await readView(scope);
      if (isCurrent(scope)) setView(next);
    } catch (cause) {
      if (!isCurrent(scope)) return;
      setView(null);
      setError(
        cause instanceof Error ? cause.message : "暂时无法读取额度，请重试。",
      );
    } finally {
      if (isCurrent(scope)) setLoading(false);
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    const scope = session.current;
    if (!scope || !isCurrent(scope) || !draft?.account.policy) return;
    const slots = Number(draft.slots);
    if (
      !/^\d+$/.test(draft.slots) ||
      !Number.isSafeInteger(slots) ||
      slots > draft.account.policy.max_active_attempts ||
      Object.values(draft.reserve).some(
        (value) => !/^\d+(\.\d{1,6})?$/.test(value),
      )
    ) {
      setError(
        "保护量需为非负数，最多保留六位小数；并发名额需为不超过账户上限的整数。",
      );
      return;
    }
    const exceeded = draft.account.pools.find(
      (pool) =>
        pool.reported_limit !== null &&
        quotaUnits(draft.reserve[pool.id] ?? "0") >
          quotaUnits(pool.reported_limit),
    );
    if (exceeded) {
      setError(`${exceeded.id} 的保护量不能超过已报告池上限。`);
      return;
    }
    const policy = {
      ...draft.account.policy,
      lead_reserve: draft.reserve,
      lead_reserved_slots: slots,
    };
    const payload = JSON.stringify({ policy });
    const identity = JSON.stringify([
      draft.account.id,
      draft.revision,
      payload,
    ]);
    let key = commands.current.get(identity);
    if (!key) {
      key = crypto.randomUUID();
      commands.current.set(identity, key);
    }
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const response = await fetch(
        `/v1/resources/policy?account_id=${encodeURIComponent(draft.account.id)}`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": scope.csrf,
            "If-Match": `"${draft.revision}"`,
            "Idempotency-Key": key,
          },
          body: payload,
        },
      );
      if (!isCurrent(scope)) return;
      if (response.status === 401) {
        sessionExpired.current();
        throw new Error("会话已过期，请重新登录。");
      }
      if (response.status === 409) {
        const accountId = draft.account.id;
        setDraft(null);
        setView(null);
        const next = await readView(scope);
        if (!isCurrent(scope) || !next) return;
        setView(next);
        const current = next.accounts.find(
          (account) => account.id === accountId,
        );
        setDraft(current ? editable(current) : null);
        setError("额度设置已变化，已载入当前值。请重新调整后保存。");
        return;
      }
      if (!response.ok) throw new Error("无法保存保护设置，请检查数值后重试。");
      await response.json().catch(() => {
        throw new TypeError("Unconfirmed response");
      });
      if (!isCurrent(scope)) return;
      commands.current.delete(identity);
      setDraft(null);
      setNotice("保护设置已保存，后续分配会使用新设置。");
      setView(null);
      const next = await readView(scope);
      if (isCurrent(scope)) setView(next);
    } catch (cause) {
      if (!isCurrent(scope)) return;
      setError(
        cause instanceof TypeError
          ? "尚未确认操作结果，可重试同一份设置。"
          : cause instanceof Error
            ? cause.message
            : "保存失败，请重试。",
      );
    } finally {
      if (isCurrent(scope)) setSaving(false);
    }
  }

  return (
    <section className="resource-panel" aria-labelledby="resource-heading">
      <header className="resource-header">
        <div>
          <h2 id="resource-heading">账户额度</h2>
          <p>查看共享额度与在途用量，为主 Commander 留出余量。</p>
        </div>
        <button type="button" onClick={refresh} disabled={loading || saving}>
          刷新额度
        </button>
      </header>
      {error && (
        <p role="alert" className="resource-error">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="resource-notice">
          {notice}
        </p>
      )}
      {loading && <p role="status">正在读取额度…</p>}
      {!loading && view?.accounts.length === 0 && (
        <div className="resource-empty">
          <h3>还没有账户额度记录</h3>
          <p>登记账户和额度窗口后，这里会显示各账户的共享用量与保护设置。</p>
        </div>
      )}
      {view?.accounts.map((account) => (
        <section
          className="resource-account"
          key={account.id}
          aria-label={`${account.id} 账户`}
        >
          <div className="resource-account-heading">
            <div>
              <h3>{account.id}</h3>
              <p>
                在途任务 {account.active_attempts} · 等待核对{" "}
                {account.waiting_reconciliation}
              </p>
            </div>
            <button
              type="button"
              disabled={
                loading ||
                saving ||
                !account.policy ||
                account.policy_revision === null
              }
              onClick={() => {
                setDraft(editable(account));
                setError("");
                setNotice("");
              }}
              aria-label={`调整 ${account.id} 的保护量`}
            >
              调整保护量
            </button>
          </div>
          {!account.policy && <p>尚未设置账户分配规则。</p>}
          {(account.blockers ?? []).length > 0 && (
            <ul
              className="resource-blockers"
              aria-label={`${account.id} 的分配限制`}
            >
              {account.blockers?.map((blocker, index) => (
                <li key={`${blocker.reason_code}-${index}`}>
                  {blocker.reason_code === "ACCOUNT_COOLDOWN"
                    ? `账户正在等待冷却。${blocker.until === null ? "稍后可刷新核对。" : `预计可重新检查：${timeLabel(blocker.until)}。`}`
                    : blocker.reason_code === "QUOTA_EXHAUSTED" ||
                        blocker.reason_code.startsWith(
                          "EXHAUSTION_REQUIRES_NEW_OBSERVATION",
                        )
                      ? "额度已耗尽，等待新的有效报告。"
                      : "账户暂不可分配，等待核对。"}
                </li>
              ))}
            </ul>
          )}
          {draft?.account.id === account.id && (
            <form className="resource-editor" onSubmit={save}>
              <h4>主 Commander 保护设置</h4>
              <p>
                这些设置在同一账户的所有任务之间共享。当前账户最多同时运行{" "}
                {draft.account.policy?.max_active_attempts} 个任务。
              </p>
              <fieldset disabled={saving}>
                <legend>保留额度</legend>
                {account.pools.map((pool) => (
                  <label key={pool.id}>
                    {pool.id} 的 Commander 保护量
                    <span className="resource-field">
                      <input
                        inputMode="decimal"
                        value={draft.reserve[pool.id] ?? "0"}
                        onChange={(event) =>
                          setDraft({
                            ...draft,
                            reserve: {
                              ...draft.reserve,
                              [pool.id]: event.target.value,
                            },
                          })
                        }
                      />
                      <span>
                        {pool.unit === "requests"
                          ? "次"
                          : pool.unit === "percent"
                            ? "%"
                            : "token"}
                      </span>
                    </span>
                  </label>
                ))}
                <label>
                  为主 Commander 保留的并发名额
                  <input
                    inputMode="numeric"
                    value={draft.slots}
                    onChange={(event) =>
                      setDraft({ ...draft, slots: event.target.value })
                    }
                  />
                </label>
              </fieldset>
              <div className="resource-actions">
                <button type="submit" disabled={saving}>
                  {saving ? "正在保存…" : "保存保护设置"}
                </button>
                <button
                  type="button"
                  disabled={saving}
                  onClick={() => setDraft(null)}
                >
                  取消修改
                </button>
              </div>
            </form>
          )}
          <div className="resource-pools">
            {account.pools.map((pool) => (
              <PoolCard key={pool.id} pool={pool} />
            ))}
          </div>
        </section>
      ))}
      {view && (
        <p className="resource-footer">
          最近读取：{timeLabel(view.observed_at)}
          。本地预留无法锁住其他客户端的消费，实际服务额度仍可能变化。
        </p>
      )}
    </section>
  );
}
