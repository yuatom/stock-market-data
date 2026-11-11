# stock-market-data

美股可复用 Market Data Plane 的 **public compute / contract repository**。

负责：Provider adapters、Collector、Store implementation、Schemas、Data contracts、Recovery/Rebase 逻辑、Probe、Inventory 与 GitHub Actions 数据采集运行面。

不负责：Research、Strategy、Report、Evidence、Decision、Evaluation、canonical Finalization，也不直接保存第三方 Market Data payload。

持久化市场数据单独存入 private `yuatom/stock-market-data-store`；研究系统 `yuatom/stock-dairy` 通过 `market_data_contract_sha`（本仓）+ `market_data_read_sha`（private store）读取固定数据快照。

## 本地合同测试

新的手动保留候选追加对账入口及其授权、并发和凭证边界见 [Retained daily append reconciliation](docs/retained-daily-reconciliation.md)。代码或测试通过不等于已获生产执行授权。

在待验证的精确源码版本中使用 Python 3.12 虚拟环境，并安装 `PyYAML`、`pytest` 和 `jsonschema`。例如，依赖可用时运行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check PyYAML pytest jsonschema
PYTHON="$PWD/.venv/bin/python" sh scripts/run_contract_tests.sh
```

入口会检查 Python 版本与依赖，然后执行 `python -m pytest tests -v`；依赖不可用时退出，不会自动安装。原自动合同测试 workflow 已移除；正式采集及生产工作流保持各自的执行条件。本地测试结果需绑定实际源码版本，不能代替生产数据验收或未核实的服务器端合并门禁。

当前迁移状态：等待创建 private `stock-market-data-store`，随后迁移现有 256-session baseline / Session Capture / Stage Snapshot，并切换 Collector 与 `stock-dairy` consumer。

## Bounded append runtime

`python -m scripts.simulate_eleven_symbol_append --output /new/local/directory`
runs fabricated local acceptance data only. It performs no provider request or
remote publication and does not establish hosted durability.

The official runtime in `scripts/run_eleven_symbol_append.py` requires an exact
approved private contract for Store pins, counts, dates and append scope. No real
private contract is tracked in public source. The checked-in template is invalid
until populated and approved privately.

The manual workflow is deliberately deployment-gated. It accepts only a public
Compute identity and exits before any checkout, credential access or provider
operation because no approved private authority-input channel is configured.
There are no private approval dispatch inputs or public outputs. No new secret or
credential storage is provisioned. See [runtime contract](docs/eleven-symbol-append.md)
for immutability, qualification, receipt and failure behavior.

### Evidence-only SOXX hosted capture

The manual `soxx-capture-only.yml` deployment guard currently stops before
private input or credential access. The complete evidence-only runtime is
locally tested and requires a separately reviewed private authority channel.
See [hosted capture boundaries](docs/hosted-capture-routes.md) for immutable
private approval, retention and separate activation requirements.
