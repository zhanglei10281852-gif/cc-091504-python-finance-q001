# 公司行动核算服务

面向私人银行资产核算的 Python 后端服务：从成交、持仓批次、汇率、市场日历与
公司行动公告出发，按业务真实顺序重放账户变化，为每一批证券重建可解释的
成本沿革、可售数量与汇兑损益。

## 解决的问题

同一只股票接连发生拆股、配股、现金红利再投资、跨账户转入后，日终总持仓
虽然能对上，但每批证券的成本、可售数量和汇兑损益无法解释。本服务：

- **按真实业务顺序推演**：公告版本、除权日、登记日、到账日、撤销更正各自
  生效，重放结果与消息到达顺序无关（同日排序规则见 `src/ordering.py`）；
- **迟到事件只产生新核算版本**：已出具的客户快照永远不被覆盖，新旧版本
  可对比（重述差异一目了然）；
- **同一消息重传保持幂等**：`message_id` 去重，重传返回首次结果；
- **每一分钱、每一股都有明确去向**：配股不足一股（现金替代或残余待处理）、
  现金选择权、冻结数量、跨币种费用、再投资零头全部登记为可查询的去向记录；
- **批次重算原子提交**：校验失败不留下半套结果，当前版本保持不变；
- **可校验的成本沿革**：任一持仓日可导出带哈希链的沿革文档，独立校验。

## 运行

需要 Python 3.11 或更高版本（仅标准库，无第三方依赖）：

```bash
python3 src/index.py                 # 默认监听 8000，数据写入 .runtime/
RUNTIME_DIR=/data PORT=8000 python3 src/index.py
```

执行测试与样例回放：

```bash
python3 -m unittest discover -s tests
python3 scripts/replay_sample.py     # 端到端回放投诉样例场景
```

也可以运行 `docker compose up --build` 启动容器。

## 样例场景

`reference/samples/complaint_scenario.json` 完整还原投诉场景：客户账户
（基准币 CNY）持有港股 STK-007，依次发生——

1. 买入 10,003 股（含港币佣金与**美元**征费，跨币种费用分别入账）；
2. 1 拆 2（除权 2026-02-02）；
3. 5 配 1 配股：不足一股的 0.2 股现金替代、另一账户选择**现金选择权**、
   配股到账后冻结至上市日；
4. 现金红利（10% 税 + 港币手续费）同日**再投资**（零头现金保留去向）；
5. 跨账户转入 3,000 股（保留原始成本与取得日）；
6. 部分冻结后卖出（先进先出，冻结部分不可售，汇兑损益单独分解）；
7. **迟到事件**：配股到账日汇率补发、红利公告更正（0.50→0.55）——
   各自只产生新核算版本，已出具月结快照不变；
8. 成交消息**重传**——幂等返回，无新效果。

## 消息类型（POST /v1/messages）

统一摄入信封：`{"message_id": "...", "kind": "...", "payload": {...},
"auto_recalculate": true}`。`auto_recalculate=false` 用于批量装载后统一重算。

| kind | 说明 | 关键字段 |
| --- | --- | --- |
| `account` | 账户 | account_id, base_currency |
| `security` | 证券 | security_id, currency, market |
| `fx_rate` | 汇率（带日期与来源） | base, quote, date, rate, source |
| `calendar` | 市场日历 | market, days[] |
| `cash_movement` | 现金存取 | account_id, currency, amount, date |
| `trade` | 成交（费用可为任意币种） | trade_id, side, quantity, price, currency, trade_date, fees[] |
| `transfer` | 跨账户转入转出（外部转入须带 cost_basis） | transfer_id, from_account, to_account, quantity, transfer_date |
| `corporate_action` | 公司行动公告（version 递增；status=preliminary/confirmed/cancelled） | action_id, type, version, status, ex_date, record_date, pay_date, terms |
| `election` | 客户选择（现金选择权 / 退出再投资） | action_id, account_id, mode |
| `freeze` / `unfreeze` | 冻结 / 解冻 | account_id, security_id, quantity, date, reason |
| `position_snapshot` | 持仓核对（同一日期以最新收到的为准） | account_id, security_id, date, quantity |

公司行动 `type`：`split`（terms: from/to, 可选 cash_in_lieu_price）、
`rights_issue`（ratio_base/ratio_rights, subscription_price, currency,
可选 cash_option_price、cash_in_lieu_price、listing_date、fee、default_mode）、
`cash_dividend`（amount_per_share, currency, 可选 tax_rate、fee）、
`reinvestment`（dividend_action_id, price, currency）。

更正与撤销：同一 `action_id` 发布更高 `version` 即更正；`status=cancelled`
即撤销（重放时整体剔除）。资格规则：除权日重放时点持有且买入日早于除权日
的批次参与确权；同日拆股先于确权（红利/配股按拆股后数量计算）。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /health | 健康检查 |
| POST | /v1/messages | 摄入消息（幂等） |
| POST | /v1/recalculate | 手动触发重算（事件日志未变则空转） |
| GET | /v1/versions · /v1/versions/{n} | 核算版本列表 / 详情 |
| GET | /v1/accounts/{id}/positions?date=&version= | 任一日持仓（含批次、可售、冻结、成本） |
| GET | /v1/accounts/{id}/cash?date= | 现金分币种 |
| GET | /v1/accounts/{id}/pending?date= | 待确认事项（待到账/待汇率/碎股残余/条款未确认/现金缺口） |
| GET | /v1/accounts/{id}/realized | 已实现损益（交易/汇兑分解） |
| GET | /v1/accounts/{id}/lineage?security_id=&date= | 成本沿革（批次历史、去向、采用的公告版本） |
| GET | /v1/accounts/{id}/lineage/export?security_id=&date= | 导出可校验沿革（含哈希链） |
| POST | /v1/lineage/verify | 独立校验沿革文档哈希链 |
| POST | /v1/snapshots · GET /v1/snapshots/{id} | 出具 / 查询客户快照（出具后不可变） |
| GET | /v1/snapshots/{id}/compare | 快照口径 vs 当前版本（解释客户看到的跳变） |
| GET | /v1/corporate-actions | 公司行动及实际采用的公告版本 |
| GET | /v1/fx-rates/lookup?base=&quote=&date= | 查询某日实际采用的汇率 |
| GET | /v1/events | 事件日志审计 |

错误统一为 `{"error": {"code", "message", "details"}}`：400 参数校验、
404 不存在、409 幂等冲突（同 message_id 不同内容）、422 重算失败
（事件已入日志但版本未提交，修正后重新触发即可）。

## 持久化与原子性

`.runtime/` 下：`events.jsonl`（追加式事件日志，唯一事实来源）、
`messages.json`（幂等记录）、`versions/v{N}.json` + `current.json`
（先写版本文件再拨指针，崩溃只留孤儿文件不留半套结果）、
`snapshots/`（客户快照，不可覆盖）、`failed_rebuilds.jsonl`（失败诊断）。
每个核算版本记录 `event_log_hash`（事件链式哈希），版本与产生它的事件
序列一一绑定。

## 目录结构

```
src/
  index.py     服务入口
  app.py       HTTP 路由与错误格式
  service.py   摄入幂等、版本重算、查询、快照、沿革导出/校验
  engine.py    重放引擎（公司行动、成交、转入转出、冻结、核对）
  ordering.py  同日事件稳定排序规则
  store.py     事件日志 / 版本 / 快照的原子持久化
  util.py      Decimal 精度、规范 JSON、哈希链
reference/
  domain.json                    公开枚举与精度约定
  samples/complaint_scenario.json 投诉样例场景
scripts/replay_sample.py         样例端到端回放
tests/                           单元 / 服务 / 接口测试
```
