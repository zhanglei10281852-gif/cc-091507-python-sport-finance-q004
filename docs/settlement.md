# 结算引擎设计说明

## 1. 分层账

每个承诺（`Commitment`：合同 / 报名预测 / 赞助 / 场地 / 志愿者）在任意业务时点
`as_of` 都有六层金额，单位精确到分（数量精确到 6 位，见
`reference/domain.json`）：

| 层 | 含义 | 计算 |
|---|---|---|
| `committed` 承诺 | 按当前有效合同版本与范围应承担的金额 | `固定费 + 范围固定 + 单价 × (计划量 + 范围量)` |
| `earned` 挣得/完成 | 对方已实际完成的价值 | 支出类：服务日（赛事日）后按实际量确认；收入类（报名费/赞助）随流水即时确认 |
| `invoiced` 应付 | 有效发票净额（拆票/红字/重开按族折算） | 见 §3 |
| `accrued` 预提 | 已完成但尚未收到发票 | `max(0, earned − invoiced)` |
| `paid` 已付 | 有效发票上已支付金额 | 回执按业务时间归集 |
| `released` 释放 | 不再承担的承诺 | 范围调减按当时单价计价 + 取消/关闭时未结算部分 |

收入成本中心 `direction = -1`（报名费、赞助），支出中心 `direction = 1`；
预算超支与审批只对支出中心生效。

## 2. 时间模型（三个时间，互不混淆）

- `occurred_at` **业务时间**：报名、开票、支付、合同生效实际发生的时间；
- `received_at` **接收时间**：票据/回执到达本系统的时间，允许晚于业务时间；
- 所有事件只追加（event sourcing），状态由事件重放得到，序列号 `seq` 全局递增。

`GET /snapshot/<event>?as_of=T` 重建 **T 当时的认知**：业务时间晚于 T、
或接收时间晚于 T 的发票/回执都不出现在快照中。因此一张 3/20 开具、赛后补开
才寄达的发票，在 3/22 的历史快照里仍表现为"预提"，在当前视图里才是"应付/已付"。

## 3. 发票族：拆票、红字、重开

所有关联通过 `original_id`（指向原单）与 `replaces_id`（被取代的票）表达：

- **拆票**：原蓝字票 + 若干 `kind=normal, original_id=原票` 的子票。
  累计拆票不得超过原票金额；原票的**有效系数** =（原票额 − 已拆出额）/ 原票额，
  所以整族净额恒等于原票。只能对子票（或未拆部分）付款，拆满后原票不可付。
- **红字**：`kind=red, original_id=原票`，金额必须为负，冲减原票。
- **重开**：`kind=reissue, original_id=原票, replaces_id=原票`，必须先红冲再重开。
  重开后原票与承接它的红字票有效系数都为 0，账面只留重开票；被取代的票禁止付款。

一张发票只能属于一个成本中心（用于超支判定），但可以多行、对应多个承诺。

## 4. 取消 / 路线变更只影响未结算

`cancellation`（或 `commitment_closed`）在取消时点冻结
`frozen_value = max(已完成, 已开票, 已付)`：

- 承诺与挣得此后固定为冻结值，差额计入 `released`；
- 数量流水、合同变更、范围调整在冻结后一律拒绝；
- 冻结后到达的发票只能补开冻结额度内尚未结算的部分，超额拒绝；
- 已付款（已结算）永不回冲。

路线调整取消赞助权益用 `scope_adjusted`（`delta_qty` / `delta_fixed` 为负），
调减额按调整时点有效单价计价计入释放层。

## 5. 支付与幂等

- 支付必须指向具体发票；回执号 `receipt_no` 全局唯一，乱序到达或重放提交
  同一回执一律 `409 duplicate_receipt`，**绝不重复付款**；
- 支付不得早于开票时间，不得超过发票的有效可付额度（考虑拆票/红冲/重开）；
- 不得对被重开/红冲替代的发票付款。

## 6. 差异桥：人数、单价、合同变更

每个承诺在快照中给出从初始预算到当前预测的三因素桥，恒等式成立：

```
baseline + qty_variance + price_variance + contract_variance = current
```

- `qty_variance = (当前量 − 预算量) × 预算单价` —— 报名/退赛、实际出勤人数；
- `price_variance = 当前量 × (当前单价 − 预算单价)` —— 合同版本单价调整；
- `contract_variance` —— 固定费、燃油附加、范围增减等非量价因素。

当前量在开赛前用计划/预测人数（保证赛前即可解释预算偏差），有实际流水后用实际量；
承诺被冻结后 `current` 为冻结结算额。

## 7. 超支审批队列（带截止时间）

- 发票入账使某支出成本中心 `max(承诺, 应付) > 预算` 时，系统自动追加
  `approval_requested` 进入队列，金额为超支额；同一中心已有未决申请时合并；
- 每个成本中心配置 `approval_hours`，截止时间 = 发起时间 + 时限；
- 队列在快照中返回 `pending / approved / rejected / expired` 与 `deadline`；
- 截止之后才作出的决定无效（状态记为 `expired` 并补记系统过期事件），
  必须重新发起；可调用 `POST /admin/sweep` 把到点未决的申请批量置为过期；
- 支付时若中心超支且没有窗口内足够额度的批准，返回
  `402 approval_required` / `402 approval_expired`。

## 8. 持久化

事件以 JSONL 追加写入 `$RUNTIME_DIR/events.jsonl`（默认 `.runtime/`）。
重启后全量重放重建状态；审批入队、审批过期等系统事件也是事件，
重放时不再重复生成。

## 9. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/events` | 追加命令（见 §10），返回追加的事件（含系统事件） |
| GET | `/events?event_code=` | 不可变事件日志 |
| GET | `/snapshot/<event>?as_of=` | 分层账快照 + 成本中心汇总 + 差异桥 + 审批队列 |
| GET | `/invoices?event_code=` | 发票族台账（有效系数、关联、已付） |
| POST | `/admin/sweep` | body `{"at": "..."}`，过期审批出队 |

错误：`400` 业务校验、`404` 实体不存在、`402` 需审批/审批过期、
`409` 状态冲突（含重复回执）。

## 10. 命令类型

`event_created`、`cost_center_created`、`commitment_created`、
`registration`、`withdrawal`、`contract_versioned`、`scope_adjusted`、
`cancellation`、`commitment_closed`、`invoice_received`、`payment_received`、
`approval_requested`、`approval_decided`。

命令外层：`{"type": ..., "occurred_at": 业务时间, "received_at": 接收时间(可选),
"payload": {...}}`。完整示例见 `tests/test_settlement.py` 的 `build_scenario()`。
