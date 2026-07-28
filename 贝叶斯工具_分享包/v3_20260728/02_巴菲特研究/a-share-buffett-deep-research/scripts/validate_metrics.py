#!/usr/bin/env python3
"""校验A股研报中的基础估值算术。

输入JSON示例见同目录 metrics_sample.json。所有金额使用人民币元，股本使用股，
比例使用小数（例如2%写0.02）。脚本不联网、不修改输入文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any

getcontext().prec = 32


class ValidationError(ValueError):
    """输入数据不合法。"""


def to_decimal(value: Any, field: str) -> Decimal:
    if value is None:
        raise ValidationError(f"字段 {field} 不能为空")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"字段 {field} 不是有效数字: {value}") from exc
    if not number.is_finite():
        raise ValidationError(f"字段 {field} 必须是有限数字")
    return number


def positive(data: dict[str, Any], field: str, required: bool = False) -> Decimal | None:
    value = data.get(field)
    if value is None:
        if required:
            raise ValidationError(f"缺少必填字段: {field}")
        return None
    number = to_decimal(value, field)
    if number <= 0:
        raise ValidationError(f"字段 {field} 必须大于0")
    return number


def relative_error(actual: Decimal, declared: Decimal) -> Decimal:
    if actual == 0:
        return Decimal("0") if declared == 0 else Decimal("Infinity")
    return abs(declared - actual) / abs(actual)


def decimal_to_json(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        return float(value)
    if isinstance(value, dict):
        return {key: decimal_to_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decimal_to_json(item) for item in value]
    return value


def calculate(data: dict[str, Any]) -> dict[str, Any]:
    price = positive(data, "price", required=True)
    shares = positive(data, "shares", required=True)
    assert price is not None and shares is not None

    market_cap = price * shares
    computed: dict[str, Decimal] = {"market_cap": market_cap}

    metric_inputs = {
        "pe_static": positive(data, "net_profit"),
        "pe_ttm": positive(data, "net_profit_ttm"),
        "pe_forward": positive(data, "forecast_net_profit"),
        "pb": positive(data, "book_equity"),
        "ps": positive(data, "revenue_ttm"),
    }
    for metric, denominator in metric_inputs.items():
        if denominator is not None:
            computed[metric] = market_cap / denominator

    dividend_per_share = data.get("dividend_per_share")
    if dividend_per_share is not None:
        dividend = to_decimal(dividend_per_share, "dividend_per_share")
        if dividend < 0:
            raise ValidationError("字段 dividend_per_share 不能小于0")
        computed["dividend_yield"] = dividend / price

    target_results: list[dict[str, Decimal | str]] = []
    forecast_profit = positive(data, "forecast_net_profit")
    for index, item in enumerate(data.get("target_prices", [])):
        if isinstance(item, dict):
            name = str(item.get("name", f"目标价{index + 1}"))
            target_price = to_decimal(item.get("price"), f"target_prices[{index}].price")
        else:
            name = f"目标价{index + 1}"
            target_price = to_decimal(item, f"target_prices[{index}]")
        if target_price <= 0:
            raise ValidationError("目标价必须大于0")
        row: dict[str, Decimal | str] = {
            "name": name,
            "price": target_price,
            "implied_market_cap": target_price * shares,
        }
        if forecast_profit is not None:
            row["implied_forward_pe"] = target_price * shares / forecast_profit
        target_results.append(row)

    return {"computed": computed, "target_prices": target_results}


def compare_declared(
    computed: dict[str, Decimal], declared: dict[str, Any], tolerance: Decimal
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for metric, declared_raw in declared.items():
        if metric not in computed:
            checks.append({
                "metric": metric,
                "status": "SKIP",
                "message": "缺少计算该指标所需的输入",
            })
            continue
        declared_value = to_decimal(declared_raw, f"declared.{metric}")
        actual = computed[metric]
        error = relative_error(actual, declared_value)
        status = "PASS" if error <= tolerance else "FAIL"
        checks.append({
            "metric": metric,
            "status": status,
            "computed": actual,
            "declared": declared_value,
            "relative_error": error,
        })
    return checks


def render_summary(result: dict[str, Any], tolerance: Decimal) -> None:
    computed = result["computed"]
    print("估值算术校验结果")
    print(f"允许相对误差: {tolerance * 100:.2f}%")
    print(f"计算市值: {computed['market_cap'] / Decimal('100000000'):.4f} 亿元")
    for key in ("pe_static", "pe_ttm", "pe_forward", "pb", "ps"):
        if key in computed:
            print(f"{key}: {computed[key]:.4f} 倍")
    if "dividend_yield" in computed:
        print(f"dividend_yield: {computed['dividend_yield'] * 100:.4f}%")

    if result["target_prices"]:
        print("目标价隐含估值:")
        for row in result["target_prices"]:
            line = (
                f"- {row['name']}: {row['price']} 元，隐含市值 "
                f"{to_decimal(row['implied_market_cap'], 'implied_market_cap') / Decimal('100000000'):.4f} 亿元"
            )
            if "implied_forward_pe" in row:
                line += f"，隐含前瞻PE {to_decimal(row['implied_forward_pe'], 'implied_forward_pe'):.4f} 倍"
            print(line)

    checks = result.get("checks", [])
    if checks:
        print("正文声明值对比:")
        for check in checks:
            if check["status"] == "SKIP":
                print(f"- SKIP {check['metric']}: {check['message']}")
            else:
                print(
                    f"- {check['status']} {check['metric']}: 计算值={check['computed']:.6g}，"
                    f"声明值={check['declared']:.6g}，偏差={check['relative_error'] * 100:.3f}%"
                )


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle, parse_float=Decimal, parse_int=Decimal)
    except FileNotFoundError as exc:
        raise ValidationError(f"输入文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"JSON格式错误: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("JSON根节点必须是对象")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="校验A股研报基础估值算术")
    parser.add_argument("input", type=Path, help="输入JSON文件")
    parser.add_argument("--tolerance", default="0.02", help="允许的相对误差，默认0.02")
    parser.add_argument("--output", type=Path, help="可选：保存JSON校验结果")
    args = parser.parse_args()

    try:
        tolerance = to_decimal(args.tolerance, "tolerance")
        if tolerance < 0 or tolerance >= 1:
            raise ValidationError("tolerance必须在0到1之间")
        data = load_json(args.input)
        result = calculate(data)
        declared = data.get("declared", {})
        if declared and not isinstance(declared, dict):
            raise ValidationError("declared必须是对象")
        result["checks"] = compare_declared(result["computed"], declared, tolerance)
        render_summary(result, tolerance)
        if args.output:
            args.output.write_text(
                json.dumps(decimal_to_json(result), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        failures = [item for item in result["checks"] if item["status"] == "FAIL"]
        return 2 if failures else 0
    except ValidationError as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
