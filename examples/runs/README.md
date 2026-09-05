# Run planning example

`create-request.template.json` 是待绑定模板，不是已批准需求或 Commander 输出。使用 M1-01 当前项目的 ID、revision、configuration digest，以及用户指定且已在配置中批准的 Profile revision/参与者替换示例值。零摘要故意不能通过当前项目核对；`fixture-profile` 只适用于离线项目夹具。

先准备已有的项目数据库和项目批准根目录，然后通过公开 CLI 创建需求：

```text
python -m karajan.runs --database RUNS.sqlite --projects PROJECTS.sqlite --allowed-root REPOSITORIES --principal owner create --input create-request.json --command-key create-report-requirement
python -m karajan.runs --database RUNS.sqlite --projects PROJECTS.sqlite --allowed-root REPOSITORIES --principal owner list
python -m karajan.runs --database RUNS.sqlite --projects PROJECTS.sqlite --allowed-root REPOSITORIES --principal owner get --run-id RUN_ID
```

`approve-plan --run-id RUN_ID --input approval.json --command-key KEY` 需要现有提案返回的五个确切字段：`term / plan_revision / plan_digest / authorization_digest / configuration_digest`。`decide-handoff` 输入是现有提案的 `term / handoff_id / handoff_digest / decision`。CLI 不生成计划、不伪造预算回执、不调用模型；没有已有提案时这些命令不能代替 Commander 或真实资格门。

公开进程回归由 `tests/runs/test_planning.py` 中 `test_public_cli_creates_only_a_requirement_and_returns_the_same_owned_run_after_restart` 自动准备真实临时 Git/ProjectRegistry，生成绑定输入，运行 CLI 并重启读取；不要求手工替换模板即可运行该测试。脚本化规划回执仅存在于测试层，用于验证领域状态，不能被生产入口导入来启用来源。

本切片的实际覆盖和红绿记录见 [实施文档](../../docs/implementation/m1-run-planning.md)。所有真实模型/现金规划验收仍是 not_run。
