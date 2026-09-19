"""端到端演示：按样例数据分阶段入账、重建版本、出具快照、迟到更正、导出校验。

运行：python3 src/demo.py  （持久化写入 .runtime-demo/，可随时删除重跑）
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service import Service  # noqa: E402
from store import Store  # noqa: E402

RUNTIME = ROOT / ".runtime-demo"
SAMPLE = ROOT / "reference" / "sample_data.json"


def show(title: str, obj) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def main() -> None:
    shutil.rmtree(RUNTIME, ignore_errors=True)
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    svc = Service(Store(RUNTIME), holidays=set(sample["holidays"]))
    snapshot_id = None

    for stage in sample["stages"]:
        print(f"\n########## 阶段 {stage['name']} ##########")
        if "snapshot" in stage:
            snap = svc.publish_snapshot(**stage["snapshot"])
            snapshot_id = snap["snapshot_id"]
            show("快照已出具（此后迟到事件不得改写）", snap)
            continue
        result = svc.ingest(stage["messages"])
        for r in result["results"]:
            print(f"  接入 {r['message_id']}: {r['status']} — {r.get('detail', '')}")
        show("重建版本", svc.rebuild())

    pos = svc.positions("ACC-A", "2026-07-10")
    show("任一持仓日查询（2026-07-10，当前版本）", pos)

    lineage = svc.lineage("ACC-A", "0700.HK", "2026-07-10")
    show("成本沿革（总额如何由原始批次演变、采用了哪版公告与汇率）", {
        "as_of": lineage["as_of"], "version": lineage["version"],
        "totals": lineage["totals"],
        "roots": [{"root_lot_id": r["root_lot_id"], "origin": r["origin"],
                   "lots": [{"lot_id": l["lot_id"], "quantity": l["quantity"],
                             "cost_total": l["cost_total"]} for l in r["lots"]]}
                  for r in lineage["roots"]],
        "announcements": lineage["announcements"],
        "fx_usages": lineage["fx_usages"],
        "pending": lineage["pending"],
    })

    show("快照保持原版本内容（不受迟到更正影响）", svc.get_snapshot(snapshot_id))

    export = svc.export_lineage("ACC-A", "0700.HK", "2026-07-10")
    out = RUNTIME / "lineage-export.json"
    out.write_text(json.dumps(export, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n导出件已写入 {out}")
    show("独立校验导出件", Service.verify_export(export))


if __name__ == "__main__":
    main()
