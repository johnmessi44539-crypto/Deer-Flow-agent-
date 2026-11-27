# DeerFlow Agent 评测工作台

本仓库基于 [DeerFlow](https://github.com/bytedance/deer-flow) 二次开发，新增独立的 Agent 评测扩展。
宿主提供登录、权限、会话和 AgentRuns 执行能力；扩展负责任务集、后台实验、规则判分、历史比较与报告。
上游代码与版权声明保留，许可证见仓库根目录 `LICENSE`。

## 从哪里阅读

- `examples/deerflow-extension-eval-center/`：可独立安装的扩展源码、完整测试和运行脚本。
- `deerflow_extension_eval_center/data/fixed-cases-v1.json`：三条固定输入、参考答案和判分配置，位于上述扩展目录内。
- `agent_adapter.py`：通过宿主 AgentRuns 建立独立线程、观察执行和提取最终输出。
- `service.py`：后台任务、幂等请求、取消、超时和实验生命周期。
- `graders.py`：严格 JSON 结构、字段、类型和数值检查。
- `store.py`：SQLite 持久化、用户隔离、版本快照与并发一致性。
- `compare.py`：同数据集实验的逐条对比。
- `static/index.mjs`：宿主评测页面，离线预览复用同一页面与服务。
- `backend/extensions/sources/deerflow-extension-eval-center/`：宿主扩展管理器生成的部署快照；后端依赖已锁定到该仓库内路径。

修改扩展时编辑 `examples/` 下的源包，验证后通过扩展管理器升级部署快照，重启 Gateway。
不要直接修改部署快照后遗漏源包。

## 先运行离线版本

离线版本不需要模型 API Key，也不需要启动整个 DeerFlow。准备 Python 3.12 和 `uv`；
轻量页面交互自检还需要 Node.js。以下命令从新克隆的项目开始，不依赖开发者机器上的虚拟环境。

### Windows PowerShell

```powershell
git clone https://github.com/johnmessi44539-crypto/Deer-Flow-agent-.git
cd Deer-Flow-agent-
cd examples/deerflow-extension-eval-center
uv venv --python 3.12
uv pip install --python .venv/Scripts/python.exe ../../backend/packages/extension-api ".[dev,live]"

powershell -NoProfile -ExecutionPolicy Bypass -File ./run-local.ps1 -Mode test -Runtime local
powershell -NoProfile -ExecutionPolicy Bypass -File ./run-local.ps1 -Mode offline -Runtime local
powershell -NoProfile -ExecutionPolicy Bypass -File ./run-local.ps1 -Mode negative -Runtime local
powershell -NoProfile -ExecutionPolicy Bypass -File ./run-local.ps1 -Mode preview -Runtime local
```

最后一条命令启动 `http://127.0.0.1:2027/`。按 Ctrl+C 停止；实验数据库在本地 `local-data/`，报告在 `reports/`。
这些运行数据不会提交到 Git。

### Linux 和 macOS

```bash
git clone https://github.com/johnmessi44539-crypto/Deer-Flow-agent-.git
cd Deer-Flow-agent-/examples/deerflow-extension-eval-center
uv venv --python 3.12
uv pip install --python .venv/bin/python ../../backend/packages/extension-api '.[dev,live]'
.venv/bin/python -m pytest tests -q
.venv/bin/python -m deerflow_extension_eval_center.cli --report-dir reports
.venv/bin/python -m deerflow_extension_eval_center.cli --report-dir reports --negative
.venv/bin/python -m uvicorn deerflow_extension_eval_center.preview:create_app --factory --host 127.0.0.1 --port 2027
```

## 固定用例和预期结果

1. 按月汇总销售额：1 月为 300，2 月为 80，总额为 **380**。
2. 扣除退款后：净收入为 **230**，全额退款订单数为 **1**。
3. 第二行金额非法：返回 **INVALID_AMOUNT**，定位 **第 2 行**。

正常离线演示应为 3/3 通过。负向自检故意把第一条总额改成 999，应为 2/3 通过；
唯一失败项是 `monthly-sales-001` 的 `$/grand_total`。与正常实验比较应检出 1 条退步、2 条不变。

离线响应是固定合成数据，验证的是调度、存储和评分流程，不能作为模型能力成绩。
执行完成与评分通过分别展示；未完成、异常和未评分结果不会算作通过。

## 接入完整 DeerFlow

完整宿主的依赖与启动方式见[运行指南](deployment.md)和[配置指南](configuration.md)。
按上游流程执行 `make setup`，在本机配置模型供应商，再启动宿主。
本仓库已包含部署快照及对应 `backend/uv.lock`，无需引用开发者机器上的外部目录。

在本机 `config.yaml` 的 `plugins` 列表中添加以下配置；若已有 `plugins`，合并列表，不要重复声明根键：

```yaml
plugins:
  - name: eval-center
    package: deerflow-extension-eval-center
    use: deerflow_extension_eval_center:install
    enabled: true
    config:
      enabled: true
      storage_path: /app/backend/.deer-flow/eval-center/experiments.sqlite
      assistant_id: lead_agent
      timeout_seconds: 120
```

上述绝对存储路径适用于 Docker 容器；本机直接启动 Gateway 时改成该机器上持久、可写的绝对路径。
同一个 SQLite 文件只支持一个服务实例。配置或包版本变化后重启 Gateway。

模型 API Key 保存在本机 `.env` 或环境变量中，`config.yaml` 使用环境变量引用，例如 `$DEEPSEEK_API_KEY`。
本仓库不包含真实 Key、登录密码或数据库。首次运行按宿主流程创建自己的账号。

通过 Nginx 入口 `http://localhost:2026` 登录，打开：

```text
/workspace/extensions/local.eval-center/experiments
```

离线入口不开放真实 Agent。宿主页面中的真实执行需要用户主动启动，会使用当前登录用户的 AgentRuns 权限。
每条用例独立会话，参考答案只进入评分器，不会发送给被测 Agent。

若不方便使用网页，可在扩展目录安装 `live` 依赖后运行：

```bash
python -m deerflow_extension_eval_center.live_cli --show-cases
python -m deerflow_extension_eval_center.live_cli --run-agent --output reports/live-agent.json
```

第一条进行登录与预检，第二条发起真实模型执行。账号密码在交互终端输入；Cookie、CSRF 校验和宿主鉴权沿用正常流程。
真实调用产生供应商用量，完整 Token 与费用无法汇总时显示未知。

## 测试与验收

在扩展虚拟环境中运行：

```bash
python -m pytest tests -q
python -m ruff check .
python -m ruff format --check .
node tests/ui-smoke.mjs
```

测试覆盖严格 JSON 判分、负向检错、用户隔离、数据集版本、并发幂等、超时取消、中断恢复、实验比较和接口处理。
轻量 UI 自检使用 mock DOM，真实浏览器与真实模型需要单独联调。

2026-11-27 本机验收记录：Windows 与 Linux 离线容器各 258 项测试通过；Ruff 与轻量 UI 自检通过。
宿主页面验证正常离线演示 3/3、负向自检 2/3，并检出 1 条退步。
通过正常登录会话完成 DeepSeek 三条固定任务 3/3，以及导入合成任务 1/1 的真实执行。
这些结论只覆盖本次固定输入，不代表更广泛业务场景的质量或生产性能。

## 当前边界

支持短文本 JSON 任务、每个数据集最多 20 条用例；自定义数据集使用真实 Agent 模式。
版本以 `id + revision` 标识，内容由 SHA-256 固定；不同数据集内容不能直接比较。
服务重启后无法确认结束的任务保留 Run IDs 并标记需要核对，不自动重放。
暂未提供文件评分、大批量并行评测、分布式任务队列或完整 Token 费用归因。
