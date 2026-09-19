# 赛事预算与供应商结算引擎

面向体育赛事报名收入、合同承诺和供应商结算的 Python 后端服务。接收报名、退赛、赞助权益变更、场地与供应商合同、完成量申报和供应商发票，按合同版本与实际完成量形成 **承诺 / 预提 / 应付 / 释放** 分层账；支持发票拆票、红字、重新开票的血缘关联，支付回执幂等防重复付款，按赛事日和成本中心重建历史预算快照，并把超支项目送入带截止时间的审批队列。

## 运行

需要 Python 3.11 或更高版本（仅标准库，无第三方依赖）：

```bash
python3 src/index.py
```

服务默认监听 `8000` 端口，状态持久化在 `.runtime/`（可用 `RUNTIME_DIR` 覆盖）。执行测试：

```bash
python3 -m unittest discover -s tests
```

也可以运行 `docker compose up --build` 启动容器。

## 接口概览

全部 JSON 响应；错误格式为 `{"error": {"code", "message", "details?"}}`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/contracts` | 创建合同（首版条款生效即形成承诺） |
| POST | `/contracts/{id}/versions` | 合同换版（只调整未结算部分） |
| POST | `/budgets` | 新建预算版本（赛事日 + 成本中心） |
| POST | `/events` | 摄入 `registration` / `withdrawal` / `sponsor_change` / `route_change` / `cancellation` / `fulfillment` |
| POST | `/invoices` | 接收发票（确认应付） |
| POST | `/invoices/{id}/split` | 拆票（子票合计须等于原票余额） |
| POST | `/invoices/{id}/red-letter` | 红字冲销（累计不超过原票金额） |
| POST | `/invoices/{id}/reissue` | 重新开票（冲销原票 + 新票关联原单） |
| GET | `/invoices/{id}/lineage` | 发票血缘（原单 + 全部子单） |
| POST | `/payments` | 支付回执（`idempotency_key` 幂等；`direction=inbound` 为赞助到账） |
| GET | `/ledger` | 分层账视图 |
| GET | `/budgets/snapshot?race_day&cost_center&as_of&received_before` | 重建当时预算快照 |
| GET | `/budgets/variance?race_day&cost_center&as_of` | 偏差分解（人数/单价/合同变更） |
| GET | `/approvals?status` | 超支审批队列（按截止时间排序） |
| POST | `/approvals/{id}/decision` | 审批决策 `{"decision": "approved" \| "rejected"}` |

## 示例

```bash
# 1. 预算：计时芯片 10000 × 12 元
curl -X POST localhost:8000/budgets -d '{"race_day":"2026-10-18","cost_center":"CC-OPS","currency":"CNY","lines":[{"item":"计时芯片","planned_quantity":"10000","unit_price":"12"}]}'

# 2. 供应商合同：按报名人数计价
curl -X POST localhost:8000/contracts -d '{"vendor":"计时公司","kind":"vendor","race_day":"2026-10-18","cost_center":"CC-OPS","currency":"CNY","lines":[{"item":"计时芯片","unit_price":"12","quantity_basis":"per_head","per_head_factor":"1"}]}'

# 3. 报名 10000 人 → 自动形成 12 万承诺
curl -X POST localhost:8000/events -d '{"type":"registration","race_day":"2026-10-18","count":10000,"fee":200}'

# 4. 实际完成 4000 → 预提 4.8 万
curl -X POST localhost:8000/events -d '{"type":"fulfillment","race_day":"2026-10-18","contract_id":"ctr-0001","item":"计时芯片","quantity":4000}'

# 5. 赛后补开发票 → 应付
curl -X POST localhost:8000/invoices -d '{"invoice_no":"INV-1","vendor":"计时公司","currency":"CNY","contract_id":"ctr-0001","lines":[{"item":"计时芯片","quantity":"4500","unit_price":"12"}]}'

# 6. 支付回执（重发安全）
curl -X POST localhost:8000/payments -d '{"idempotency_key":"BANK-1","vendor":"计时公司","amount":"54000","currency":"CNY"}'

# 7. 财务重建赛事日快照 / 查看偏差成因
curl 'localhost:8000/budgets/snapshot?race_day=2026-10-18&cost_center=CC-OPS&as_of=2026-10-31'
curl 'localhost:8000/budgets/variance?race_day=2026-10-18&cost_center=CC-OPS'
```

领域规则详见 `docs/domain.md`。
