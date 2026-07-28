#!/usr/bin/env python3
"""运行本Skill的本地示例校验。

不联网、不调用子进程，直接复用同目录下的校验函数，避免任何外部命令执行风险。
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

# 直接 import 同目录模块，避免依赖包安装
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dcf_scenarios  # noqa: E402
import validate_metrics  # noqa: E402


def render_metrics(data: dict, result: dict, tolerance: Decimal) -> None:
    """与 validate_metrics.render_summary 等价的简化输出。"""
    computed = result["computed"]
    print("估值算术校验结果")
    print(f"允许相对误差: {tolerance * 100:.2f}%")
    market_cap_yi = computed["market_cap"] / Decimal("100000000")
    print(f"计算市值: {market_cap_yi:.4f} 亿元")
    for key in ("pe_static", "pe_ttm", "pe_forward", "pb", "ps"):
        if key in computed:
            print(f"{key}: {computed[key]:.4f} 倍")
    if "dividend_yield" in computed:
        print(f"dividend_yield: {computed['dividend_yield'] * 100:.4f}%")

    if result.get("target_prices"):
        print("目标价隐含估值:")
        for row in result["target_prices"]:
            line = (
                f"- {row['name']}: {row['price']} 元，"
                f"隐含市值 {Decimal(str(row['implied_market_cap'])) / Decimal('100000000'):.4f} 亿元"
            )
            if "implied_forward_pe" in row:
                line += f"，隐含前瞻PE {Decimal(str(row['implied_forward_pe'])):.4f} 倍"
            print(line)

    checks = result.get("checks", [])
    if checks:
        print("正文声明值对比:")
        for item in checks:
            status = item["status"]
            if status == "SKIP":
                print(f"- SKIP {item['metric']}: {item['message']}")
                continue
            error_pct = Decimal(str(item["relative_error"])) * 100
            print(
                f"- {status} {item['metric']}: 计算值={Decimal(str(item['computed']))}, "
                f"声明值={Decimal(str(item['declared']))}, 偏差={error_pct:.3f}%"
            )


def render_dcf(results: list[dict]) -> None:
    """与 dcf_scenarios.render 等价的简化输出。"""
    print("FCFF DCF多情景复算结果")
    for result in results:
        print(
            f"- {result['name']}: 每股价值 {Decimal(str(result['per_share_value'])):.4f} 元，"
            f"企业价值 {Decimal(str(result['enterprise_value'])) / Decimal('100000000'):.4f} 亿元，"
            f"股权价值 {Decimal(str(result['equity_value'])) / Decimal('100000000'):.4f} 亿元，"
            f"终值占比 {Decimal(str(result['terminal_value_share'])) * 100:.2f}%"
        )
        for warning in result["warnings"]:
            print(f"  警告: {warning}")


def main() -> int:
    base = Path(__file__).resolve().parent
    metrics_path = base / "metrics_sample.json"
    dcf_path = base / "dcf_sample.json"

    tolerance = Decimal("0.02")

    print("\n运行示例: validate_metrics.py")
    metrics_data = validate_metrics.load_json(metrics_path)
    metrics_result = validate_metrics.calculate(metrics_data)
    declared = metrics_data.get("declared", {})
    if declared and isinstance(declared, dict):
        metrics_result["checks"] = validate_metrics.compare_declared(
            metrics_result["computed"], declared, tolerance
        )
    render_metrics(metrics_data, metrics_result, tolerance)
    metrics_failed = [c for c in metrics_result.get("checks", []) if c["status"] == "FAIL"]

    print("\n运行示例: dcf_scenarios.py")
    dcf_data = dcf_scenarios.load_json(dcf_path)
    common_kwargs = {
        "shares": Decimal(str(dcf_data["shares"])),
        "cash": Decimal(str(dcf_data["cash"])),
        "debt": Decimal(str(dcf_data["debt"])),
        "non_operating_assets": Decimal(str(dcf_data.get("non_operating_assets", 0))),
        "minority_interest": Decimal(str(dcf_data.get("minority_interest", 0))),
    }
    dcf_results = [
        dcf_scenarios.run_scenario(scenario, **common_kwargs)
        for scenario in dcf_data.get("scenarios", [])
    ]
    render_dcf(dcf_results)

    print("\n说明：基础估值示例故意保留一个错误的前瞻PE声明，用于验证脚本能发现问题。")
    if metrics_failed:
        print(f"示例校验发现 {len(metrics_failed)} 个声明值错误，需要修复后再次复算。")
        # 故意保留FAIL样本时仍以0返回，便于本地示例一次跑通
    print("示例运行完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
