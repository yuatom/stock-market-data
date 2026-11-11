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
