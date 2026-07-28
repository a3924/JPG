#!/usr/bin/env python3
"""复算A股研报的多情景FCFF DCF。

输入JSON示例见同目录 dcf_sample.json。所有金额使用人民币元，比例使用小数。
脚本仅执行本地确定性计算，不联网、不修改输入文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any

getcontext().prec = 32


class DCFError(ValueError):
    """DCF输入不合法。"""


def dec(value: Any, field: str) -> Decimal:
    if value is None:
        raise DCFError(f"字段 {field} 不能为空")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DCFError(f"字段 {field} 不是有效数字: {value}") from exc
    if not number.is_finite():
        raise DCFError(f"字段 {field} 必须是有限数字")
    return number


def positive(value: Any, field: str) -> Decimal:
    number = dec(value, field)
    if number <= 0:
        raise DCFError(f"字段 {field} 必须大于0")
    return number


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else str(value)
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle, parse_float=Decimal, parse_int=Decimal)
    except FileNotFoundError as exc:
        raise DCFError(f"输入文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DCFError(f"JSON格式错误: {exc}") from exc
    if not isinstance(data, dict):
        raise DCFError("JSON根节点必须是对象")
    return data


def run_scenario(
    scenario: dict[str, Any], shares: Decimal, cash: Decimal, debt: Decimal,
    non_operating_assets: Decimal, minority_interest: Decimal
) -> dict[str, Any]:
    name = str(scenario.get("name", "未命名情景"))
    wacc = positive(scenario.get("wacc"), f"{name}.wacc")
    growth = dec(scenario.get("terminal_growth"), f"{name}.terminal_growth")
    if growth < 0:
        raise DCFError(f"{name}: 永续增长率不能小于0")
    if wacc <= growth:
        raise DCFError(f"{name}: WACC必须大于永续增长率")

    fcff_raw = scenario.get("fcff")
    if not isinstance(fcff_raw, list) or not fcff_raw:
        raise DCFError(f"{name}: fcff必须是非空数组")
    fcff = [dec(value, f"{name}.fcff[{index}]") for index, value in enumerate(fcff_raw)]

    pv_fcff = Decimal("0")
    schedule: list[dict[str, Decimal | int]] = []
    one = Decimal("1")
    for year, cash_flow in enumerate(fcff, start=1):
        discount_factor = (one + wacc) ** year
        present_value = cash_flow / discount_factor
        pv_fcff += present_value
        schedule.append({
            "year": year,
            "fcff": cash_flow,
            "discount_factor": discount_factor,
            "present_value": present_value,
        })

    terminal_value = fcff[-1] * (one + growth) / (wacc - growth)
    pv_terminal = terminal_value / ((one + wacc) ** len(fcff))
    enterprise_value = pv_fcff + pv_terminal
    equity_value = (
        enterprise_value + cash + non_operating_assets - debt - minority_interest
    )
    per_share_value = equity_value / shares
    terminal_share = pv_terminal / enterprise_value if enterprise_value != 0 else Decimal("0")

    warnings: list[str] = []
    if len(fcff) < 5:
        warnings.append("预测期少于5年，终值敏感度较高")
    if wacc < Decimal("0.07") or wacc > Decimal("0.15"):
        warnings.append("WACC位于常见7%-15%区间之外，请核实依据")
    if growth > Decimal("0.04"):
        warnings.append("永续增长率高于4%，需要强有力的长期依据")
    if terminal_share > Decimal("0.75"):
        warnings.append("终值现值占企业价值超过75%，估值高度依赖终值")
    if any(value <= 0 for value in fcff):
        warnings.append("预测FCFF包含零或负值，检查终值和持续经营假设")

    return {
        "name": name,
        "wacc": wacc,
        "terminal_growth": growth,
        "forecast_years": len(fcff),
        "schedule": schedule,
        "pv_explicit_fcff": pv_fcff,
        "terminal_value_at_horizon": terminal_value,
        "pv_terminal_value": pv_terminal,
        "terminal_value_share": terminal_share,
        "enterprise_value": enterprise_value,
        "equity_value": equity_value,
        "per_share_value": per_share_value,
        "warnings": warnings,
    }


def render(results: list[dict[str, Any]]) -> None:
    print("FCFF DCF多情景复算结果")
    for result in results:
        print(
            f"- {result['name']}: 每股价值 {result['per_share_value']:.4f} 元，"
            f"企业价值 {result['enterprise_value'] / Decimal('100000000'):.4f} 亿元，"
            f"股权价值 {result['equity_value'] / Decimal('100000000'):.4f} 亿元，"
            f"终值占比 {result['terminal_value_share'] * 100:.2f}%"
        )
        for warning in result["warnings"]:
            print(f"  警告: {warning}")


def main() -> int:
    parser = argparse.ArgumentParser(description="复算多情景FCFF DCF")
    parser.add_argument("input", type=Path, help="输入JSON文件")
    parser.add_argument("--output", type=Path, help="可选：保存JSON结果")
    args = parser.parse_args()

    try:
        data = load_json(args.input)
        shares = positive(data.get("shares"), "shares")
        cash = dec(data.get("cash", 0), "cash")
        debt = dec(data.get("debt", 0), "debt")
        non_operating_assets = dec(data.get("non_operating_assets", 0), "non_operating_assets")
        minority_interest = dec(data.get("minority_interest", 0), "minority_interest")
        for field, value in (
            ("cash", cash),
            ("debt", debt),
            ("non_operating_assets", non_operating_assets),
            ("minority_interest", minority_interest),
        ):
            if value < 0:
                raise DCFError(f"字段 {field} 不能小于0")

        scenarios = data.get("scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            raise DCFError("scenarios必须是非空数组")
        results = [
            run_scenario(
                scenario, shares, cash, debt, non_operating_assets, minority_interest
            )
            for scenario in scenarios
            if isinstance(scenario, dict)
        ]
        if len(results) != len(scenarios):
            raise DCFError("每个scenario必须是对象")

        render(results)
        payload = {
            "shares": shares,
            "cash": cash,
            "debt": debt,
            "non_operating_assets": non_operating_assets,
            "minority_interest": minority_interest,
            "results": results,
        }
        if args.output:
            args.output.write_text(
                json.dumps(jsonable(payload), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return 0
    except DCFError as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
