# 赛事预算与供应商结算引擎

面向城市马拉松等体育赛事的报名收入、合同承诺与供应商结算的 Python 后端服务。
接收报名、退赛、赞助权益调整、场地租赁、志愿者补贴与供应商发票，按合同版本与
实际完成量形成 **承诺 / 挣得 / 应付 / 预提 / 释放 / 已付** 分层账；支持发票
拆票、红字、重开关联原单，赛事取消/路线变更只影响未结算项目，支付回执乱序
到达也不会重复付款；可按赛事日和成本中心重建当时预算快照，分解每笔偏差来自
人数、单价还是合同变更，超支项目进入带截止时间的审批队列。

- 事件溯源：业务时间（发生）与接收时间（到达）分离，历史快照可重建当时认知
- 金额 `Decimal` 精确到分，数量精确到 6 位
- 仅依赖 Python 3.11 标准库；事件日志 JSONL 落盘 `.runtime/`

## 运行

```bash
python3 src/index.py          # 默认 0.0.0.0:8000，RUNTIME_DIR / PORT 可配
```

- `GET /health` 健康检查
- `POST /events` 追加一条命令
- `GET /snapshot/<赛事>?as_of=<ISO时间>` 预算快照（分层账/成本中心/差异桥/审批队列）
- `GET /invoices?event_code=` 发票族台账
- `GET /events?event_code=` 不可变事件日志
- `POST /admin/sweep` 过期审批出队

领域规则、分层账口径与命令格式见 [`docs/settlement.md`](docs/settlement.md)，
完整端到端示例见 `tests/test_settlement.py`。

## 测试

```bash
python3 -m unittest discover -s tests
```

也可以运行 `docker compose up --build` 启动容器。
