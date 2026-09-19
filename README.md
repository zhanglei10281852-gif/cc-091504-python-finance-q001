# 公司行动核算服务

面向证券持仓批次与公司行动处理的 Python 后端服务（仅依赖标准库，Python 3.11+）。

服务以事件溯源方式重建持仓成本：成交、持仓批次、汇率、市场日历与公司行动公告按
**公告版本、除权日、登记日、到账日、撤销更正**的真实业务顺序推演，迟到事件只产生
新的核算版本，已出具的客户快照永不改写。领域规则详见 `docs/domain.md`。

## 运行

```bash
python3 src/index.py          # 默认监听 8000 端口
```

环境变量：`PORT`（端口）、`RUNTIME_DIR`（持久化目录，默认 `.runtime/`）、
`HOLIDAYS_FILE`（市场假日 JSON 列表，用于到账日顺延）。

执行测试：

```bash
python3 -m unittest discover -s tests
```

端到端演示（投诉案例：拆股→配股→红利再投资→跨账户转入→迟到更正）：

```bash
python3 src/demo.py           # 写入 .runtime-demo/，可删除重跑
```

也可以运行 `docker compose up --build` 启动容器。

## 接口

所有接口返回 JSON。写接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/ingest` | 接入消息（`{"messages": [...]}`），幂等；`?rebuild=true` 可联动重建 |
| POST | `/rebuild` | 全量重放并原子发布新版本；失败时当前版本不变 |
| POST | `/snapshots` | 出具客户快照 `{"account", "date"}`，不可变 |
| POST | `/verify` | 校验成本沿革导出件（重算哈希） |

查询接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/positions?account=&date=` | 任一持仓日的批次、数量、冻结、可售、成本与现金 |
| GET | `/lineage?account=&security=&date=` | 成本沿革：总额如何由原始批次演变、采用了哪版公告与汇率、哪些事件待确认 |
| GET | `/export/lineage?account=&security=&date=` | 导出可校验的成本沿革 |
| GET | `/pending` | 待确认事件 |
| GET | `/corporate-actions?security=` | 生效公告及其版本 |
| GET | `/fx?pair=&date=` | 生效汇率及来源版本 |
| GET | `/realized?account=` | 已实现损益 |
| GET | `/versions` | 核算版本列表 |
| GET | `/snapshots` / `/snapshots/{id}` | 快照列表 / 快照内容 |

## 消息类型

`cash_movement`（入金/支取）、`trade`（成交）、`transfer`（跨账户转入/转出，
转入须随券结转成本 `cost_total`，未确认转出会冻结对应批次）、
`corporate_action`（`split` / `rights_issue` / `cash_dividend` / `reinvestment`，
含更正与撤销）、`election`（账户层选择：配股认购 `subscribe` / 现金选择权 `cash` /
放弃 `lapse`，红利再投资 `reinvest`）、`confirmation`（确认 pending 消息）、
`fx_rate`（汇率，带 `source_version`）。样例见 `reference/sample_data.json`。

## 持久化

`.runtime/` 下：`events.jsonl`（只追加事件日志）、`versions/`（各核算版本，
原子落盘）、`current.json`（当前版本指针）、`snapshots/`（已出具快照）、
`messages.json`（幂等台账）。
