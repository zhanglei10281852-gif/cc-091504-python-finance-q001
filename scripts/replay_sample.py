#!/usr/bin/env python3
"""端到端回放投诉样例：加载 reference/samples/complaint_scenario.json。

用法：
    python3 scripts/replay_sample.py [--runtime DIR] [--keep]

依次执行样例中的摄入/重算/快照/查询步骤，打印叙事化结果，
最后导出成本沿革并独立校验哈希链。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service import Service  # noqa: E402
from store import Store  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", default=".runtime-sample", help="运行数据目录")
    parser.add_argument("--keep", action="store_true", help="保留已有运行目录")
    args = parser.parse_args()

    runtime = Path(args.runtime)
    if runtime.exists() and not args.keep:
        shutil.rmtree(runtime)
    service = Service(Store(runtime))

    scenario = json.loads(
        (ROOT / "reference" / "samples" / "complaint_scenario.json").read_text("utf-8")
    )
    print(f"== {scenario['name']} ==")
    print(scenario["description"])
    print()

    last_snapshot_id = None
    for i, step in enumerate(scenario["steps"], 1):
        op = step["op"]
        if op == "ingest":
            response, status = service.ingest(step["message"])
            mid = step["message"]["message_id"]
            if response.get("idempotent_replay"):
                print(f"[{i:02d}] 摄入 {mid}: 幂等重放（重复消息被识别，不产生新效果）")
            elif status == 422:
                print(f"[{i:02d}] 摄入 {mid}: 已记录但重算失败 -> "
                      f"{response['recalculation']['error']}")
            else:
                rec = response.get("recalculation")
                suffix = f" -> 版本 v{rec['version_seq']}" if rec else ""
                print(f"[{i:02d}] 摄入 {mid}: {step['message']['kind']}{suffix}")
        elif op == "recalculate":
            result = service.recalculate()
            print(f"[{i:02d}] 重算 -> 版本 v{result['version_seq']}"
                  f"（待确认 {result['pending_count']} 项）  {step.get('note', '')}")
        elif op == "snapshot":
            snap = service.issue_snapshot(step["account_id"], step["date"],
                                          step.get("label", ""))
            last_snapshot_id = snap["snapshot_id"]
            print(f"[{i:02d}] 出具快照 {snap['snapshot_id']} "
                  f"@ {snap['date']}（钉住版本 v{snap['version_seq']}，此后不可改写）")
        elif op == "query":
            kind = step["kind"]
            if kind == "pending":
                data = service.pending(step["account_id"], step["date"])
                print(f"[{i:02d}] 待确认事项 {step['account_id']} @ {step['date']}:")
                for item in data["pending"]:
                    print(f"      - {item['kind']}: {item['detail']}")
            elif kind == "positions":
                data = service.positions(step["account_id"], step["date"])
                print(f"[{i:02d}] 持仓 {step['account_id']} @ {step['date']}"
                      f"（版本 v{data['version_seq']}）:")
                for pos in data["positions"]:
                    print(f"      {pos['security_id']}: 数量 {pos['quantity']}, "
                          f"可售 {pos['sellable']}, 冻结 {pos['frozen']}, "
                          f"成本 {pos['cost_total']} {pos['currency']}, "
                          f"基准成本 {pos['base_cost_total']} {pos['base_currency']}")
                for cash in data["cash"]:
                    print(f"      现金 {cash['currency']}: {cash['balance']}")
            elif kind == "compare_last_snapshot":
                data = service.compare_snapshot(last_snapshot_id)
                print(f"[{i:02d}] 快照 {last_snapshot_id} 对比 "
                      f"(v{data['snapshot_version']} -> v{data['current_version']}, "
                      f"restated={data['restated']}):")
                for diff in data["diffs"]:
                    print(f"      {diff['security_id']}: 数量 {diff['quantity_then']} -> "
                          f"{diff['quantity_now']} (Δ{diff['quantity_delta']}), "
                          f"基准成本 {diff['base_cost_then']} -> {diff['base_cost_now']}")
            elif kind == "lineage_export":
                export = service.export_lineage(step["account_id"],
                                                step["security_id"], step["date"])
                out = runtime / "lineage_export.json"
                out.write_text(json.dumps(export, ensure_ascii=False, indent=2), "utf-8")
                verify = service.verify_lineage(export)
                print(f"[{i:02d}] 成本沿革导出 -> {out} "
                      f"（{len(export['steps'])} 步流水，lineage_hash="
                      f"{export['lineage_hash'][:16]}…，独立校验 valid={verify['valid']}）")
                for lot in export["lots"]:
                    origin = lot["origin"]
                    print(f"      批次 {lot['lot_id']}: {lot['quantity']} 股, "
                          f"成本 {lot['cost_total']} {lot['currency']}, "
                          f"来源 {origin['kind']}({origin.get('ref')}), "
                          f"沿革 {len(lot['history'])} 步")
            elif kind == "realized":
                data = service.realized(step["account_id"])
                print(f"[{i:02d}] 已实现损益（含汇兑分解）:")
                for rec in data["realized"]:
                    print(f"      {rec['date']} 卖出 {rec['quantity']} 股: "
                          f"交易损益 {rec['realized_trading_base']} + "
                          f"汇兑损益 {rec['realized_fx_base']} = "
                          f"{rec['realized_total_base']} {rec['base_currency']}")
    print()
    print("完成。运行数据保存在", runtime)


if __name__ == "__main__":
    main()
