# DeerFlow Agent 评测工作台

独立全栈扩展，v0.2 使用三条固定合成用例验证任务执行、结果留存和严格 JSON 判分，支持导入版本化数据集及逐用例对比实验。
源码通过 `deerflow-extension-api` 接入宿主；页面、SQLite 数据库和评分规则由扩展自己维护。

## 固定用例 v1

1. `monthly-sales-001`：1 月销售额 100、200，2 月 80。期望 `month_totals` 中 1 月 300、2 月 80，`grand_total` 为 380。
2. `net-revenue-001`：三笔实收 100、150、50，退款 20、0、50。期望 `net_revenue` 为 230，`fully_refunded_orders` 为 1。
3. `invalid-amount-001`：第二行金额为 `abc`。期望 `error_code` 为 `INVALID_AMOUNT`，`invalid_row` 为 2。

完整输入、参考答案和规则在包内 `data/fixed-cases-v1.json`。参考答案不会发送给被测 Agent。
输出应为一个 JSON 对象，键顺序不影响结果；额外文本、代码围栏、重复键、NaN、类型或字段错误判失败。
布尔值不能冒充数字。数据集内容经 SHA-256 固定在实验记录中。

## 两种模式

- **离线演示**：使用独立固定响应验证调度、持久化、评分和报告，不是模型能力成绩。
- **真实 Agent**：用户在宿主页面主动启动，复用该用户的 AgentRuns 能力。每个用例独立线程，串行运行，最多每条 120 秒。

实验结束与答案通过是独立状态。异常、待审批/待输入、无法确认结束的执行不会被算作通过。
Token 和费用缺少完整宿主汇总接口时显示未知。配置证据缺失单独标记。
输出按试验冻结并记录 SHA-256；后续会话变化不覆盖已有报告。当前范围是短文本 JSON 任务，尚无文件判分。

## 本地验证

在依赖已安装的 Python 3.12 环境执行：

```sh
python -m pytest tests -q
python -m deerflow_extension_eval_center.cli --report-dir reports
python -m deerflow_extension_eval_center.cli --report-dir reports --negative
```

负向演示故意把总额改成 999，期望只检出一条失败；这用于验证评分器的检出能力。
报告会记录 `offline_scenario: wrong_total`，与正常离线演示分别留存。
Windows 用户可使用配套 `run-local.ps1 -Mode test|offline|negative|lint|preview` 运行测试和演示。
默认优先使用已经准备的本地 `.venv`，也可加 `-Runtime docker` 使用离线容器。源码修改保存后直接重跑。
页面轻量交互校验使用 `node tests/ui-smoke.mjs`，覆盖按钮请求、数据集切换、对比和卸载；它使用 mock DOM，真实浏览器仍需验收。

## 负向自检与实验对比

页面点击“运行离线演示”应得到 3 条通过；点击“运行负向自检”应得到 2 条通过、1 条未通过。
负向的唯一错误应为 `monthly-sales-001` 的 `$/grand_total`：期望 380、实际 999。
在负向实验详情选择正常离线实验为基线并比较，应显示 1 条退步、2 条不变。
这些成绩验证固定响应与判分流程，不代表模型能力。不同数据集 SHA-256 不可直接比较；未评分不会当作通过。
耗时差为候选减基线，缺失时显示未知。历史列表只返回摘要，详情和报告按需读取。

## 导入自己的用例

页面“导入 JSON 数据集”接受与包内 `data/fixed-cases-v1.json` 相同的声明式结构：
数据集包含 `id`、`name`、正整数 `revision`、`cases`；每条用例包含 `id`、`title`、`prompt`、`expected`、`grader`。
可先导入仓库的 `examples/custom-sales-v1.json`。支持的评分器为 `json_exact_v1`，数据集不执行自带代码。
最多 20 条用例，单条输入最多 12000 字符，页面导入文件最多 240 KiB。

同一用户的同一 `id + revision` 内容不可覆盖；内容相同可重复导入，修改后须增加 `revision`。
内置数据集 ID 保留。自定义数据集只支持宿主真实 Agent 模式，离线预览可导入和查看。
实验启动后保存所选版本的完整快照与 SHA-256，后来导入新版本不会改变旧实验。
数据集与实验都按登录用户隔离；本地最多保存每用户 100 个数据集版本、200 个实验。
实验历史当前显示最近 50 条，暂未提供分页。大规模数据集与长输出的批量评测仍需要响应分页和内存预算。

## 不依赖前端编译的真实联调

`live_cli` 通过正常的 Gateway 登录、会话 Cookie 和 CSRF 校验调用同一插件动作；不会跳过鉴权。
安装本包的 `live` 依赖后，在交互终端执行以下命令并输入已有 DeerFlow 用户名和密码，密码不回显：

```sh
python -m deerflow_extension_eval_center.live_cli --show-cases
python -m deerflow_extension_eval_center.live_cli --run-agent --output reports/live-agent.json
```

第一条只做登录与插件预检，第二条明确发起真实模型执行。默认入口为 `http://localhost:2026`。
非交互环境使用 `DEERFLOW_EVAL_USERNAME`、`DEERFLOW_EVAL_PASSWORD`，不要把凭据写入仓库或命令参数。
凭据与 Cookie 仅在进程内存中使用，不进入报告。
启动请求结果不确定时，保留终端打印的幂等键，通过 `--run-agent --idempotency-key <原键>` 查询同一批任务。
已有实验可用 `--experiment-id <ID>` 观察；增加 `--cancel` 请求取消。超时或 Ctrl+C 仅主动取消本次新启动的任务。

## 本机离线页面

`run-local.ps1 -Mode preview` 使用本地 Python 启动 `http://127.0.0.1:2027/`，按 Ctrl+C 停止。
它复用同一页面、任务服务、SQLite 和评分器，独立保存到 `local-data/preview.sqlite`。
这是离线演示入口，不读取宿主凭据，不开放真实 Agent 模式。页面展示完整固定输入与参考答案，
可启动演示、查看逐项检查并导出报告。仅绑定本机地址，并对写请求校验同源和每进程 CSRF token。
首次克隆后须创建本地 `.venv` 并安装依赖，具体命令见仓库的[运行指南](../../docs/guide/evaluation-workbench.md)。

## 安装到 DeerFlow

按宿主 extension manager 安装本包，并在对应 `plugins` 配置中设置：

```yaml
use: deerflow_extension_eval_center:install
enabled: true
config:
  enabled: true
  storage_path: /app/backend/.deer-flow/eval-center/experiments.sqlite
  assistant_id: lead_agent
  timeout_seconds: 120
```

使用持久可写目录；一次只支持一个服务实例打开同一 SQLite 数据库，文件锁拒绝重复启动。
安装或升级后重启 Gateway。浏览器页面：`/workspace/extensions/local.eval-center/experiments`。
页面显示每条完整输入与参考答案，可执行演示、启动真实实验、查看检查项并下载 JSON 报告。
插件后端动作沿用宿主鉴权，写操作还要求真实 session 来源，资源始终按登录用户隔离。

## 中断与恢复

启动请求按用户与幂等键去重；同键不同设置返回冲突。服务拥有任务生命周期，整批工作不占住请求处理器。
取消真实执行时请求宿主取消；无法确认终止或服务重启后保留已知 Run IDs，标记 `needs_reconciliation`。
首版不自动重放这些任务，用户应在原始会话核对结果后发起新实验。

## 参与评测

先阅读三条用例的输入和答案，再运行离线正向与负向演示。逐条核对检查项和报告中的 `mode`。
真实执行前在页面检查用例；执行后核对原始回答、解析结果、Run ID 和评分原因。
修改任务或规则前增加数据集 revision，并保留旧实验结果用于比较。

## 本机验收记录（2026-11-27）

v0.2 已完成 Windows 与 Linux 离线容器验收，两边各 258 项测试通过，Ruff 和轻量 UI 交互校验通过。
正式宿主页面已验证正常离线演示 3/3、负向自检 2/3，以及对比检出 1 条退步、2 条无变化。

通过正常登录会话实际运行 `deepseek-flash`：三条固定任务 3/3 通过；导入 `examples/custom-sales-v1.json`
后，自定义任务返回 `{"grand_total":100}`，1/1 通过。不同数据集 SHA-256 的实验已确认拒绝直接对比。
实际会话 ID、Run ID、模型装配证据、输出和评分保存在报告中；完整 Token 与费用仍显示未知。
这些结果覆盖本次固定合成输入，业务场景需要继续扩展数据集。

本机报告保存在 gitignored 的 `reports/live-agent-20261127.json`、`reports/live-custom-20261127.json`
和 `reports/acceptance-20261127.json`；页面可再次下载原始报告。
历史实验与数据集按账号隔离，切换登录账号后不会显示其他账号的记录。

若浏览器显示页面但操作报 403，而正常终端登录预检成功，可退出该浏览器的旧会话并重新登录，
刷新会话的 CSRF 校验字段。本次验收已完成上述正式页面验证。
