# stock-market-data

美股可复用 Market Data Plane 的 **public compute / contract repository**。

负责：Provider adapters、Collector、Store implementation、Schemas、Data contracts、Recovery/Rebase 逻辑、Probe、Inventory 与 GitHub Actions 数据采集运行面。

不负责：Research、Strategy、Report、Evidence、Decision、Evaluation、canonical Finalization，也不直接保存第三方 Market Data payload。

持久化市场数据单独存入 private `yuatom/stock-market-data-store`；研究系统 `yuatom/stock-dairy` 通过 `market_data_contract_sha`（本仓）+ `market_data_read_sha`（private store）读取固定数据快照。

当前迁移状态：等待创建 private `stock-market-data-store`，随后迁移现有 256-session baseline / Session Capture / Stage Snapshot，并切换 Collector 与 `stock-dairy` consumer。
