import { useEffect, useRef, useState, type FormEvent } from "react";
import { ProjectRuns } from "./ProjectRuns";

type Project = {
  id: string;
  name: string;
  revision: number;
  repository: { root: string; base_ref: string; base_sha: string };
  target_branch: string;
  configuration: {
    status: string;
    revision: number;
    dispatch_eligible: boolean;
  };
};
type Preview = {
  preview_id: string;
  project_revision: number;
  status: string;
  can_apply: boolean;
  issues: { code: string; path: string }[];
};

export function App() {
  const [ready, setReady] = useState(false);
  const [csrf, setCsrf] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [repositoryPath, setRepositoryPath] = useState("");
  const [baseRef, setBaseRef] = useState("main");
  const [targetBranch, setTargetBranch] = useState("main");
  const createCommand = useRef<{ payload: string; key: string } | null>(null);
  const [selected, setSelected] = useState<Project | null>(null);
  const [runProject, setRunProject] = useState<Project | null>(null);
  const [configuration, setConfiguration] = useState("{}");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [notice, setNotice] = useState("");
  const configCommand = useRef<{ payload: string; key: string } | null>(null);
  const applyCommand = useRef<{ payload: string; key: string } | null>(null);

  async function loadProjects() {
    const response = await fetch("/v1/projects");
    if (!response.ok) throw new Error("暂时无法读取项目，请重新登录后重试。");
    setProjects((await response.json()).items);
  }

  useEffect(() => {
    let active = true;
    fetch("/v1/session")
      .then(async (response) => {
        if (response.ok) {
          const session = await response.json();
          if (active) {
            setCsrf(session.csrf_token);
            await loadProjects();
          }
        } else if (response.status !== 401)
          throw new Error("本地服务暂时不可用。");
      })
      .catch(() => {
        if (active) setError("无法连接本地工作台，请检查服务是否已启动。");
      })
      .finally(() => {
        if (active) setReady(true);
      });
    return () => {
      active = false;
    };
  }, []);

  async function login(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/v1/session/bootstrap", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: code }),
      });
      setCode("");
      if (!response.ok)
        throw new Error(
          response.status === 429
            ? "尝试次数较多，请稍等一分钟。"
            : "访问码无效或已使用，请取得新的本机访问码。",
        );
      setCsrf((await response.json()).csrf_token);
      await loadProjects();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "登录失败，请重试。");
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/v1/session/logout", {
        method: "POST",
        headers: { "X-CSRF-Token": csrf ?? "" },
      });
      if (!response.ok && response.status !== 401)
        throw new Error("暂时无法退出，请重试。");
      setCsrf(null);
      setProjects([]);
      setSelected(null);
      setRunProject(null);
      setPreview(null);
      setConfiguration("{}");
      setProjectName("");
      setRepositoryPath("");
      setShowCreate(false);
      setNotice("");
      setCode("");
      createCommand.current = null;
      configCommand.current = null;
      applyCommand.current = null;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "退出失败，请重试。");
    } finally {
      setBusy(false);
    }
  }

  async function saveProject(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    const payload = JSON.stringify({
      name: projectName,
      repository_path: repositoryPath,
      base_ref: baseRef,
      target_branch: targetBranch,
      allowed_target_branches: [targetBranch],
    });
    if (createCommand.current?.payload !== payload)
      createCommand.current = { payload, key: crypto.randomUUID() };
    try {
      const response = await fetch("/v1/projects", {
        method: "POST",
        body: payload,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf ?? "",
          "Idempotency-Key": createCommand.current.key,
        },
      });
      if (response.status === 401) {
        setCsrf(null);
        throw new Error("会话已过期，请重新登录。");
      }
      if (!response.ok) {
        const reason = (await response.json()).reason_code;
        throw new Error(
          reason === "REPOSITORY_OUTSIDE_ROOTS"
            ? "这个仓库不在允许的项目目录内。"
            : "无法保存项目，请检查仓库路径和分支是否有效。",
        );
      }
      createCommand.current = null;
      setShowCreate(false);
      setProjectName("");
      setRepositoryPath("");
      await loadProjects();
    } catch (cause) {
      setError(
        cause instanceof TypeError
          ? "尚未确认保存结果，可重试同一操作。"
          : cause instanceof Error
            ? cause.message
            : "保存失败。",
      );
    } finally {
      setBusy(false);
    }
  }

  async function previewConfiguration(event: FormEvent) {
    event.preventDefault();
    if (!selected) return;
    setBusy(true);
    setError("");
    setNotice("");
    setPreview(null);
    try {
      const parsed = JSON.parse(configuration);
      const payload = JSON.stringify(parsed);
      const identity = selected.id + payload;
      if (configCommand.current?.payload !== identity)
        configCommand.current = { payload: identity, key: crypto.randomUUID() };
      const response = await fetch(
        `/v1/projects/${selected.id}/configuration/preview`,
        {
          method: "POST",
          body: payload,
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf ?? "",
            "Idempotency-Key": configCommand.current.key,
          },
        },
      );
      if (!response.ok)
        throw new Error("无法检查配置，请确认会话和配置格式后重试。");
      setPreview(await response.json());
    } catch (cause) {
      setError(
        cause instanceof SyntaxError
          ? "配置内容需要是有效的 JSON。"
          : "尚未取得配置检查结果，请重试。",
      );
    } finally {
      setBusy(false);
    }
  }

  async function openConfiguration(project: Project) {
    setRunProject(null);
    setBusy(true);
    setError("");
    setNotice("");
    setPreview(null);
    setSelected(null);
    try {
      const response = await fetch(`/v1/projects/${project.id}/configuration`);
      if (!response.ok) throw new Error("无法读取已保存配置，请重试。");
      const saved = await response.json();
      setConfiguration(JSON.stringify(saved.configuration ?? {}, null, 2));
      configCommand.current = null;
      applyCommand.current = null;
      setSelected(project);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取配置。");
    } finally {
      setBusy(false);
    }
  }

  async function applyConfiguration() {
    if (!selected || !preview?.can_apply) return;
    setBusy(true);
    setError("");
    const identity = JSON.stringify([
      selected.id,
      preview.preview_id,
      preview.project_revision,
    ]);
    if (applyCommand.current?.payload !== identity)
      applyCommand.current = { payload: identity, key: crypto.randomUUID() };
    try {
      const response = await fetch(
        `/v1/projects/${selected.id}/configuration/apply`,
        {
          method: "POST",
          body: JSON.stringify({ preview_id: preview.preview_id }),
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf ?? "",
            "Idempotency-Key": applyCommand.current.key,
            "If-Match": `"${preview.project_revision}"`,
          },
        },
      );
      if (response.status === 409) {
        setPreview(null);
        configCommand.current = null;
        throw new Error("项目已有新版本，请重新打开项目并预览。");
      }
      if (!response.ok)
        throw new Error("尚未确认配置保存结果，可重试同一操作。");
      const updated = await response.json();
      setSelected(updated);
      setProjects((previous) =>
        previous.map((project) =>
          project.id === updated.id ? updated : project,
        ),
      );
      setPreview(null);
      configCommand.current = null;
      applyCommand.current = null;
      setNotice("配置已保存。执行资格仍需单独验证。");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "配置保存失败。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="workbench">
      <aside className="sidebar">
        <a className="brand" href="/">
          K<span>Karajan</span>
        </a>
        <p>让模型协作，让你掌舵。</p>
        <div className="nav-item">
          ◈ <span>项目工作台</span>
        </div>
        <div className="local-mark">
          <i /> 本机 · 私人工作空间
        </div>
      </aside>
      <main>
        <header className="topbar">
          <span>WORKSPACE / 项目</span>
          {csrf ? (
            <button
              className="secondary"
              disabled={busy}
              onClick={() => void logout()}
            >
              退出登录
            </button>
          ) : (
            <span className="status-dot">本机工作台</span>
          )}
        </header>
        {!ready ? (
          <p role="status">正在连接工作台…</p>
        ) : !csrf ? (
          <section className="login-panel">
            <p className="eyebrow">你的协作空间</p>
            <h1>从这里，指挥下一次交付。</h1>
            <p className="muted">使用本机访问码登录，查看项目和模型配置。</p>
            <form onSubmit={login}>
              <label htmlFor="access-code">本机访问码</label>
              <input
                id="access-code"
                type="password"
                autoComplete="off"
                value={code}
                onChange={(event) => setCode(event.target.value)}
                required
              />
              <p className="field-help">
                访问码保存在启动时生成的本地文件中，使用一次即失效。
              </p>
              <button disabled={busy || !code}>
                {busy ? "正在连接…" : "进入工作台"}
              </button>
            </form>
          </section>
        ) : (
          <section className="project-space">
            <div className="section-heading">
              <div>
                <p className="eyebrow">YOUR ORCHESTRA</p>
                <h1>你的项目</h1>
                <p className="muted">
                  选择一个仓库，为下一次协作准备好规则与资源。
                </p>
              </div>
              <button
                onClick={() => {
                  setShowCreate(true);
                  setError("");
                }}
              >
                登记项目
              </button>
            </div>
            {showCreate && (
              <form className="project-form" onSubmit={saveProject}>
                <h2>登记本机仓库</h2>
                <fieldset disabled={busy}>
                  <div className="form-grid">
                    <div>
                      <label htmlFor="project-name">项目名称</label>
                      <input
                        id="project-name"
                        value={projectName}
                        onChange={(event) => setProjectName(event.target.value)}
                        required
                        maxLength={120}
                      />
                    </div>
                    <div>
                      <label htmlFor="repository-path">本机仓库路径</label>
                      <input
                        id="repository-path"
                        value={repositoryPath}
                        onChange={(event) =>
                          setRepositoryPath(event.target.value)
                        }
                        required
                      />
                    </div>
                    <div>
                      <label htmlFor="base-ref">起始分支或提交</label>
                      <input
                        id="base-ref"
                        value={baseRef}
                        onChange={(event) => setBaseRef(event.target.value)}
                        required
                      />
                    </div>
                    <div>
                      <label htmlFor="target-branch">PR 目标分支</label>
                      <input
                        id="target-branch"
                        value={targetBranch}
                        onChange={(event) =>
                          setTargetBranch(event.target.value)
                        }
                        required
                      />
                    </div>
                  </div>
                  <p className="field-help">
                    仅登记和验证仓库，不启动模型任务。
                  </p>
                  <div className="form-actions">
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => setShowCreate(false)}
                    >
                      收起
                    </button>
                    <button>{busy ? "正在保存…" : "保存项目"}</button>
                  </div>
                </fieldset>
              </form>
            )}
            <div className="project-grid">
              {projects.length ? (
                projects.map((project) => (
                  <article className="project-card" key={project.id}>
                    <h2>{project.name}</h2>
                    <p>{project.repository.root}</p>
                    <span>目标分支 · {project.target_branch}</span>
                    <span>
                      {project.configuration.status === "unconfigured"
                        ? "待配置"
                        : "配置已保存"}
                    </span>
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() => void openConfiguration(project)}
                    >
                      检查配置
                    </button>
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() => {
                        setSelected(null);
                        setRunProject(project);
                        setError("");
                        setNotice("");
                      }}
                    >
                      需求与计划
                    </button>
                  </article>
                ))
              ) : (
                <div className="empty-state">
                  <span className="empty-symbol">◈</span>
                  <h2>从第一个项目开始</h2>
                  <p>登记本机已有仓库，再检查规则、模型来源与预算。</p>
                </div>
              )}
            </div>
            {runProject && csrf && (
              <ProjectRuns
                key={runProject.id}
                project={runProject}
                csrf={csrf}
              />
            )}
            {selected && (
              <section className="project-form">
                <h2>{selected.name} · 配置预览</h2>
                <p className="field-help">
                  粘贴规则与资源配置，先查看检查结果，再保存指定版本。凭据仅填写引用，勿粘贴密钥。
                </p>
                <form onSubmit={previewConfiguration}>
                  <fieldset disabled={busy}>
                    <label htmlFor="configuration">配置内容</label>
                    <textarea
                      id="configuration"
                      rows={12}
                      spellCheck={false}
                      value={configuration}
                      onChange={(event) => {
                        setConfiguration(event.target.value);
                        setPreview(null);
                        setNotice("");
                      }}
                    />
                    <div className="form-actions">
                      <button type="submit">预览配置</button>
                    </div>
                  </fieldset>
                </form>
                {preview && (
                  <div className="preview-result">
                    <h3>
                      {preview.status === "offline_valid"
                        ? "结构检查通过"
                        : "配置仍有待补充项"}
                    </h3>
                    <p className="field-help">
                      {preview.can_apply
                        ? "可保存为配置草稿；执行资格仍需单独验证。"
                        : "此内容未保存，请修正格式或移除凭据值后重新预览。"}
                    </p>
                    {preview.issues.length > 0 && (
                      <ul>
                        {preview.issues.map((issue, index) => (
                          <li key={index}>
                            <code>{issue.path}</code>
                            <span>{configurationIssue(issue.code)}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                    {preview.can_apply && (
                      <button disabled={busy} onClick={applyConfiguration}>
                        保存这份配置
                      </button>
                    )}
                  </div>
                )}
              </section>
            )}
          </section>
        )}
        {notice && (
          <div className="notice success" role="status">
            {notice}
          </div>
        )}
        {error && (
          <div className="notice error" role="alert">
            {error}
          </div>
        )}
      </main>
    </div>
  );
}

function configurationIssue(code: string): string {
  const messages: Record<string, string> = {
    CONFIGURATION_SCHEMA_INVALID: "配置格式不完整，请使用完整配置文件。",
    CREDENTIAL_VALUE_FORBIDDEN: "请移除密钥或密码，改用凭据引用。",
    BUDGET_INVALID: "请填写有币种、次数和时长上限的预算。",
    RULEBOOK_REQUIRED: "缺少调度规则。",
    RESOURCES_REQUIRED: "缺少模型与资源配置。",
    RULEBOOK_HARD_CONSTRAINT_INVALID: "规则与已确认的执行约束不一致。",
    RESOURCE_REFERENCE_INVALID: "账户、通道或共享配额的引用不完整。",
    PROFILE_NOT_APPROVED: "此模型配置不在批准集合中。",
    PROFILE_UNAVAILABLE: "此模型配置尚不可用。",
    PROFILE_PERMISSION_UNVERIFIED: "所需能力尚未完成验证。",
    PROFILE_CLASS_INSUFFICIENT: "此配置尚未满足任务难度要求。",
  };
  return messages[code] ?? "此项尚未满足配置要求，请核对对应字段。";
}
