"""M1 — Efficiency Audit: MFU/MBU, the GPU-Util lie, and idle waste (deck §5).

Run: python missions/m1_efficiency_audit.py
"""
from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from collections import defaultdict
from missions._common import load_csv, num, catalog_by_type
from finops import metrics


def run(verbose: bool = True) -> dict:
    tel = load_csv("gpu_telemetry.csv")
    cat = catalog_by_type()

    # per-row MFU/MBU, then aggregate per GPU
    agg = defaultdict(lambda: {"util": [], "mfu": [], "mbu": [], "type": None, "idle_hours": 0})
    for r in tel:
        gtype = r["gpu_type"]
        peak_fp16 = num(cat[gtype]["peak_tflops_fp16"])
        peak_bw = num(cat[gtype]["peak_bw_tbs"])
        mfu = metrics.compute_mfu(num(r["achieved_tflops"]), peak_fp16)
        mbu = metrics.compute_mbu(num(r["achieved_bw_tbs"]), peak_bw)
        a = agg[r["gpu_id"]]
        a["type"] = gtype
        a["util"].append(num(r["gpu_util_pct"]))
        a["mfu"].append(mfu)
        a["mbu"].append(mbu)
        if num(r["gpu_util_pct"]) < 10:  # effectively idle this interval (1h)
            a["idle_hours"] += 1

    summary = []
    for gid, a in agg.items():
        summary.append({
            "gpu_id": gid, "gpu_type": a["type"],
            "gpu_util_pct": round(sum(a["util"]) / len(a["util"]), 1),
            "mfu": round(sum(a["mfu"]) / len(a["mfu"]), 3),
            "mbu": round(sum(a["mbu"]) / len(a["mbu"]), 3),
            "idle_hours": a["idle_hours"],
        })

    lies = metrics.flag_util_lies(summary)
    idle_waste = 0.0
    for s in summary:
        on_demand = num(catalog_by_type()[s["gpu_type"]]["on_demand_hr"])
        idle_waste += metrics.idle_waste_usd(s["idle_hours"], on_demand)

    # Extension 2: Right-sizing theo MBU
    print("\n== Extension 2: Right-sizing Analysis (MBU-based) ==")
    # Calculate $/GB-VRAM for each GPU type
    gpu_cost_per_gb = {}
    for gtype, specs in catalog_by_type().items():
        hbm_gb = num(specs["hbm_gb"])
        on_demand_hr = num(specs["on_demand_hr"])
        cost_per_gb = on_demand_hr / hbm_gb if hbm_gb > 0 else float('inf')
        gpu_cost_per_gb[gtype] = cost_per_gb

    print(f"{'GPU Type':12}{'$/GB-VRAM':>12}{'Peak BW (TB/s)':>15}{'$/TB-BW':>12}")
    for gtype, cost_gb in sorted(gpu_cost_per_gb.items(), key=lambda x: x[1]):
        peak_bw = num(catalog_by_type()[gtype]["peak_bw_tbs"])
        cost_per_tb_bw = num(catalog_by_type()[gtype]["on_demand_hr"]) / peak_bw if peak_bw > 0 else float('inf')
        print(f"{gtype:12}{cost_gb:>12.4f}{peak_bw:>15.1f}{cost_per_tb_bw:>12.4f}")

    # Identify memory-bound GPUs (MBU < 0.3) and suggest alternatives
    memory_bound_gpus = [s for s in summary if s["mbu"] < 0.3]
    print(f"\nMemory-bound GPUs (MBU < 0.3): {len(memory_bound_gpus)}")
    right_sizing_suggestions = []
    monthly_savings_potential = 0.0

    for gpu in memory_bound_gpus:
        current_type = gpu["gpu_type"]
        current_cost_hr = num(catalog_by_type()[current_type]["on_demand_hr"])
        current_mb_cost = current_cost_hr / num(catalog_by_type()[current_type]["peak_bw_tbs"])

        # Find cheaper alternative with sufficient bandwidth
        best_alternative = None
        best_savings_pct = 0.0

        for alt_type, specs in catalog_by_type().items():
            if alt_type == current_type:
                continue
            alt_bw = num(specs["peak_bw_tbs"])
            alt_cost_hr = num(specs["on_demand_hr"])

            # Only consider if bandwidth is at least 80% of current (to avoid severe perf degradation)
            if alt_bw >= num(catalog_by_type()[current_type]["peak_bw_tbs"]) * 0.8:
                savings_pct = (current_cost_hr - alt_cost_hr) / current_cost_hr * 100
                if savings_pct > best_savings_pct:
                    best_savings_pct = savings_pct
                    best_alternative = alt_type

        if best_alternative:
            monthly_savings = (current_cost_hr - num(catalog_by_type()[best_alternative]["on_demand_hr"])) * 24 * 30
            monthly_savings_potential += monthly_savings
            right_sizing_suggestions.append({
                "gpu_id": gpu["gpu_id"],
                "current": current_type,
                "suggested": best_alternative,
                "savings_pct": best_savings_pct,
                "monthly_savings": monthly_savings
            })

    if right_sizing_suggestions:
        print(f"\nRight-sizing suggestions:")
        print(f"{'GPU ID':12}{'Current':10}{'Suggested':10}{'Savings %':>12}{'Monthly Savings':>15}")
        for sugg in right_sizing_suggestions:
            print(f"{sugg['gpu_id']:12}{sugg['current']:10}{sugg['suggested']:10}{sugg['savings_pct']:>11.1f}%${sugg['monthly_savings']:>14,.2f}")
        print(f"\nTotal potential monthly savings from right-sizing: ${monthly_savings_potential:,.2f}")
    else:
        print("No right-sizing opportunities found (all GPUs already optimized)")

    if verbose:
        print("\n== M1 Efficiency Audit ==")
        print(f"{'GPU':14}{'type':7}{'util%':>7}{'MFU':>7}{'MBU':>7}{'idle_h':>8}")
        for s in sorted(summary, key=lambda x: x["mfu"]):
            print(f"{s['gpu_id']:14}{s['gpu_type']:7}{s['gpu_util_pct']:>7}{s['mfu']:>7}{s['mbu']:>7}{s['idle_hours']:>8}")
        print(f"\nGPU-Util LIES (util>=90% but MFU<30%): {[l['gpu_id'] for l in lies]}")
        print(f"Idle waste (1 day): ${idle_waste:,.2f}  ->  ${idle_waste*30:,.0f}/month")

    return {
        "summary": summary,
        "lies": lies,
        "idle_waste_daily": round(idle_waste, 2),
        "right_sizing_suggestions": right_sizing_suggestions,
        "right_sizing_monthly_savings": round(monthly_savings_potential, 2)
    }


if __name__ == "__main__":
    run()
