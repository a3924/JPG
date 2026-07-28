#!/usr/bin/env python3
"""机械检查A股深研底稿的覆盖、来源、时点和计算状态。

不联网、不修改输入文件、不做投资评分。输入格式见
schemas/research_bundle.schema.json 与 scripts/research_bundle_sample.json。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROFILE_REQUIRED = {
    "lite": {
        "identity", "filings", "business", "financials", "valuation", "risks"
    },
    "standard": {
        "identity", "filings", "business", "financials", "cashflow_owner_earnings",
        "industry_chain", "peers", "moat", "management_governance",
        "capital_allocation", "valuation", "research_consensus",
        "catalysts_events", "risks", "counterevidence_stress"
    },
    "deep": {
        "identity", "filings", "business", "financials", "cashflow_owner_earnings",
        "industry_chain", "peers", "moat", "management_governance",
        "capital_allocation", "valuation", "research_consensus",
        "catalysts_events", "market_signals", "capital_flow_ownership",
        "materials_macro_policy", "risks", "counterevidence_stress"
    },
}

IPO_REQUIRED = {
    "identity", "filings", "business", "financials", "cashflow_owner_earnings",
    "industry_chain", "peers", "moat", "management_governance",
    "capital_allocation", "valuation", "catalysts_events", "risks",
    "counterevidence_stress", "ipo_issuance"
}

CORE_DIMENSIONS = {"identity", "filings", "business", "financials", "valuation", "risks"}
ALLOWED_DIM_STATUS = {"complete", "partial", "missing", "not_applicable"}
ALLOWED_POINT_STATUS = {
    "available", "not_applicable", "not_disclosed", "source_unavailable",
    "stale", "conflict", "insufficient_evidence"
}
DEFAULT_FRESHNESS_HOURS = {
    "realtime": 1.0,
    "intraday": 8.0,
    "daily": 72.0,
    "news": 168.0,
    "static": 17520.0,
}


@dataclass
class Issue:
    severity: str
    category: str
    message: str
    dimension: str = "overall"
    evidence: str = ""
    suggested_fix: str = ""


class ReviewInputError(ValueError):
    """底稿输入错误。"""


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReviewInputError(f"输入文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReviewInputError(f"JSON格式错误: {exc}") from exc
    if not isinstance(data, dict):
        raise ReviewInputError("JSON根节点必须是对象")
    return data


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text + "T00:00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def required_dimensions(bundle: dict[str, Any]) -> set[str]:
    entity = bundle.get("entity") or {}
    research = bundle.get("research") or {}
    security_type = entity.get("security_type")
    if security_type == "ipo_prelisting":
        return set(IPO_REQUIRED)
    profile = research.get("profile", "standard")
    return set(PROFILE_REQUIRED.get(profile, PROFILE_REQUIRED["standard"]))


def check_root(bundle: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    for field in ("entity", "research", "dimensions", "validation", "conclusion"):
        if not isinstance(bundle.get(field), dict):
            issues.append(Issue(
                "critical", "schema", f"根字段 {field} 缺失或不是对象",
                suggested_fix=f"按schema补齐 {field}"
            ))
    entity = bundle.get("entity") or {}
    for field in ("name", "ticker", "market", "security_type", "listing_status"):
        if not entity.get(field):
            issues.append(Issue(
                "critical", "identity", f"entity.{field} 缺失",
                dimension="identity", suggested_fix="先完成标的身份和证券类型核对"
            ))
    security_type = entity.get("security_type")
    if security_type not in {"listed_stock", "ipo_prelisting", "etf", "convertible_bond", "other"}:
        issues.append(Issue(
            "critical", "identity", f"未知security_type: {security_type}",
            dimension="identity", suggested_fix="使用schema规定的证券类型"
        ))
    if security_type in {"etf", "convertible_bond", "other"}:
        issues.append(Issue(
            "critical", "applicability", "该证券类型不适用普通股巴菲特深研主流程",
            dimension="identity", evidence=str(security_type),
            suggested_fix="切换到对应资产Skill"
        ))
    profile = (bundle.get("research") or {}).get("profile")
    if profile not in PROFILE_REQUIRED:
        issues.append(Issue(
            "critical", "schema", f"未知研究档位: {profile}",
            suggested_fix="使用lite、standard或deep"
        ))
    return issues


def compute_coverage(bundle: dict[str, Any]) -> dict[str, Any]:
    dimensions = bundle.get("dimensions") or {}
    required = required_dimensions(bundle)
    complete = partial = missing = 0
    missing_names: list[str] = []
    for name in sorted(required):
        dim = dimensions.get(name)
        status = dim.get("status") if isinstance(dim, dict) else "missing"
        if status == "complete":
            complete += 1
        elif status == "partial":
            partial += 1
        else:
            missing += 1
            missing_names.append(name)
    total = len(required)
    ratio = (complete + partial * 0.5) / total if total else 1.0
    return {
        "required_total": total,
        "complete": complete,
        "partial": partial,
        "missing": missing,
        "ratio": round(ratio, 4),
        "missing_required": missing_names,
        "critical_gaps": [name for name in missing_names if name in CORE_DIMENSIONS],
    }


def check_dimensions(bundle: dict[str, Any], coverage: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    dimensions = bundle.get("dimensions") or {}
    required = required_dimensions(bundle)

    for name in sorted(required):
        dim = dimensions.get(name)
        if not isinstance(dim, dict):
            severity = "critical" if name in CORE_DIMENSIONS else "warning"
            issues.append(Issue(
                severity, "coverage", f"必需维度 {name} 完全缺失", dimension=name,
                suggested_fix="按恢复任务优先补充一手或结构化数据"
            ))
            continue
        status = dim.get("status")
        if status not in ALLOWED_DIM_STATUS:
            issues.append(Issue(
                "critical", "schema", f"维度状态非法: {status}", dimension=name,
                suggested_fix="使用complete、partial、missing或not_applicable"
            ))
        if status == "missing":
            severity = "critical" if name in CORE_DIMENSIONS else "warning"
            issues.append(Issue(
                severity, "coverage", f"必需维度 {name} 状态为missing", dimension=name,
                evidence="; ".join(dim.get("gaps") or []),
                suggested_fix="创建blocking恢复任务；无法恢复时降级为证据不足"
            ))
        if status == "not_applicable":
            issues.append(Issue(
                "warning", "applicability", f"必需维度 {name} 被标为not_applicable", dimension=name,
                suggested_fix="说明不适用依据，或调整档位/条件必需规则"
            ))

    declared = bundle.get("coverage") or {}
    for field in ("required_total", "complete", "partial", "missing"):
        if declared.get(field) != coverage.get(field):
            issues.append(Issue(
                "warning", "coverage", f"声明coverage.{field}与复算不一致",
                evidence=f"声明={declared.get(field)}, 复算={coverage.get(field)}",
                suggested_fix="以self_review复算结果更新coverage"
            ))
    declared_ratio = declared.get("ratio")
    if isinstance(declared_ratio, (int, float)) and abs(declared_ratio - coverage["ratio"]) > 0.01:
        issues.append(Issue(
            "warning", "coverage", "声明coverage.ratio与复算不一致",
            evidence=f"声明={declared_ratio}, 复算={coverage['ratio']}",
            suggested_fix="以适用且必需维度为分母重新计算"
        ))
    return issues


def freshness_limits(bundle: dict[str, Any]) -> dict[str, float]:
    configured = ((bundle.get("review_config") or {}).get("freshness_hours") or {})
    limits = dict(DEFAULT_FRESHNESS_HOURS)
    for key, value in configured.items():
        if isinstance(value, (int, float)) and value > 0:
            limits[key] = float(value)
    return limits


def check_data_points(bundle: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    dimensions = bundle.get("dimensions") or {}
    research = bundle.get("research") or {}
    collected_default = parse_time(research.get("collected_at")) or datetime.now(timezone.utc)
    limits = freshness_limits(bundle)

    for dim_name, dim in dimensions.items():
        if not isinstance(dim, dict):
            continue
        points = dim.get("data_points") or []
        if dim.get("status") in {"complete", "partial"} and not points:
            issues.append(Issue(
                "warning", "evidence", "维度有结论但没有data_points", dimension=dim_name,
                suggested_fix="添加字段级证据或将状态降为missing"
            ))
        for index, point in enumerate(points):
            if not isinstance(point, dict):
                issues.append(Issue(
                    "critical", "schema", f"data_points[{index}]不是对象", dimension=dim_name,
                    suggested_fix="按schema重写数据点"
                ))
                continue
            metric = point.get("metric") or f"index_{index}"
            status = point.get("status")
            if status not in ALLOWED_POINT_STATUS:
                issues.append(Issue(
                    "critical", "schema", f"数据点 {metric} 状态非法: {status}", dimension=dim_name,
                    suggested_fix="使用规范缺失语义"
                ))
                continue
            if status == "available":
                if "value" not in point:
                    issues.append(Issue(
                        "critical", "data", f"可用数据点 {metric} 没有value", dimension=dim_name,
                        suggested_fix="补值；真实的0可以保留"
                    ))
                if not point.get("source_name"):
                    issues.append(Issue(
                        "critical", "source", f"可用数据点 {metric} 缺少source_name", dimension=dim_name,
                        suggested_fix="补充来源名称和链接"
                    ))
            else:
                if not point.get("missing_reason"):
                    issues.append(Issue(
                        "warning", "data", f"非可用数据点 {metric} 未说明missing_reason", dimension=dim_name,
                        suggested_fix="记录未披露、源失败、过期或冲突原因"
                    ))

            tier = point.get("source_tier")
            if point.get("materiality") == "key" and tier == "D":
                issues.append(Issue(
                    "critical", "source", f"关键数据点 {metric} 仅使用D级来源", dimension=dim_name,
                    suggested_fix="补充A级一手来源或两个独立B/C级来源"
                ))
            if point.get("used_fallback") and not point.get("fallback_trace"):
                issues.append(Issue(
                    "warning", "trace", f"数据点 {metric} 使用fallback但没有尝试痕迹", dimension=dim_name,
                    suggested_fix="记录每个来源的success/empty/timeout/error/not_covered"
                ))
            if status == "conflict":
                conflict = point.get("conflict") or {}
                if not conflict.get("resolved") and point.get("materiality") == "key":
                    issues.append(Issue(
                        "critical", "conflict", f"关键数据点 {metric} 存在未解决冲突", dimension=dim_name,
                        evidence=str(conflict.get("explanation", "")),
                        suggested_fix="回到A级来源或保留区间，并禁止确定评级"
                    ))

            freshness_class = point.get("freshness_class")
            if status == "available" and freshness_class in limits:
                as_of = parse_time(point.get("as_of"))
                collected = parse_time(point.get("collected_at")) or collected_default
                if as_of is None:
                    issues.append(Issue(
                        "warning", "freshness", f"数据点 {metric} 缺少或无法解析as_of", dimension=dim_name,
                        suggested_fix="补充ISO日期/时间"
                    ))
                else:
                    age_hours = (collected - as_of).total_seconds() / 3600
                    if age_hours > limits[freshness_class]:
                        issues.append(Issue(
                            "warning", "freshness", f"数据点 {metric} 超过新鲜度告警值", dimension=dim_name,
                            evidence=f"age_hours={age_hours:.1f}, limit={limits[freshness_class]:.1f}",
                            suggested_fix="刷新数据或标记status=stale"
                        ))
    return issues


def check_financial_period(bundle: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    expected = str((bundle.get("research") or {}).get("latest_expected_financial_period") or "")
    if not expected:
        return [Issue(
            "critical", "freshness", "latest_expected_financial_period缺失",
            dimension="financials", suggested_fix="按基准日推断理论应披露最新财报期"
        )]
    financials = (bundle.get("dimensions") or {}).get("financials") or {}
    periods = {
        str(point.get("period"))
        for point in (financials.get("data_points") or [])
        if isinstance(point, dict) and point.get("status") == "available"
    }
    if expected not in periods:
        issues.append(Issue(
            "warning", "freshness", "财务数据未覆盖理论应披露最新期", dimension="financials",
            evidence=f"expected={expected}, periods={sorted(periods)}",
            suggested_fix="查询最新定期报告或说明尚未披露"
        ))
    return issues


def check_validation(bundle: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    validation = bundle.get("validation") or {}
    metrics = validation.get("metrics_status")
    dcf = validation.get("dcf_status")
    if metrics == "fail":
        issues.append(Issue(
            "critical", "calculation", "基础估值算术校验失败",
            dimension="valuation", suggested_fix="修正市值、PE、PB、PS或目标价隐含估值"
        ))
    elif metrics == "not_run":
        issues.append(Issue(
            "warning", "calculation", "尚未运行基础估值算术校验",
            dimension="valuation", suggested_fix="运行validate_metrics.py"
        ))
    if dcf == "fail":
        issues.append(Issue(
            "critical", "calculation", "DCF校验失败",
            dimension="valuation", suggested_fix="修正FCFF、WACC、g、净债务或股本"
        ))
    elif dcf == "not_run":
        issues.append(Issue(
            "warning", "calculation", "尚未运行DCF复算",
            dimension="valuation", suggested_fix="运行dcf_scenarios.py或标记not_applicable"
        ))
    return issues


def check_conclusion(bundle: dict[str, Any], issues: list[Issue]) -> list[Issue]:
    result: list[Issue] = []
    conclusion = bundle.get("conclusion") or {}
    verdict = conclusion.get("verdict")
    critical_count = sum(1 for issue in issues if issue.severity == "critical")
    if critical_count and verdict not in {"证据不足", "能力圈外"}:
        result.append(Issue(
            "critical", "conclusion", "存在critical问题但仍给出确定评级",
            evidence=f"critical_count={critical_count}, verdict={verdict}",
            suggested_fix="先执行恢复任务，或将评级降为证据不足"
        ))
    if not conclusion.get("thesis_breaker"):
        result.append(Issue(
            "warning", "conclusion", "结论缺少最可能证伪逻辑的事实",
            suggested_fix="补充一个可观察、可证伪的thesis_breaker"
        ))
    return result


def build_recovery_tasks(issues: list[Issue]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        if issue.severity not in {"critical", "warning"}:
            continue
        key = (issue.dimension, issue.category)
        if key in seen:
            continue
        seen.add(key)
        tasks.append({
            "priority": "critical" if issue.severity == "critical" else "high",
            "dimension": issue.dimension,
            "reason": issue.message,
            "preferred_source": "交易所/公司公告" if issue.category in {"source", "freshness", "conflict"} else "结构化金融数据或确定性脚本",
            "fallback_sources": ["Neodata", "腾讯自选股", "通达信", "公开原文搜索"],
            "suggested_query_or_action": issue.suggested_fix,
            "blocking": issue.severity == "critical",
        })
    return tasks


def render(coverage: dict[str, Any], issues: list[Issue]) -> None:
    print("A股深研底稿机械自审")
    print(
        f"必需维度: {coverage['required_total']}，完整: {coverage['complete']}，"
        f"部分: {coverage['partial']}，缺失: {coverage['missing']}，"
        f"加权覆盖率: {coverage['ratio'] * 100:.1f}%"
    )
    counts = {
        level: sum(1 for issue in issues if issue.severity == level)
        for level in ("critical", "warning", "info")
    }
    print(f"问题: critical={counts['critical']} warning={counts['warning']} info={counts['info']}")
    for issue in issues:
        print(f"- [{issue.severity.upper()}] {issue.dimension}/{issue.category}: {issue.message}")
        if issue.evidence:
            print(f"  证据: {issue.evidence}")
        if issue.suggested_fix:
            print(f"  修复: {issue.suggested_fix}")


def main() -> int:
    parser = argparse.ArgumentParser(description="检查A股深研底稿的结构、覆盖、来源和时点")
    parser.add_argument("input", type=Path, help="research_bundle JSON文件")
    parser.add_argument("--output", type=Path, help="可选：保存自审结果JSON")
    parser.add_argument("--strict", action="store_true", help="存在warning时也返回非零")
    args = parser.parse_args()

    try:
        bundle = load_json(args.input)
        issues = check_root(bundle)
        coverage = compute_coverage(bundle)
        issues.extend(check_dimensions(bundle, coverage))
        issues.extend(check_data_points(bundle))
        issues.extend(check_financial_period(bundle))
        issues.extend(check_validation(bundle))
        issues.extend(check_conclusion(bundle, issues))
        recovery_tasks = build_recovery_tasks(issues)
        render(coverage, issues)

        payload = {
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "coverage": coverage,
            "issues": [asdict(issue) for issue in issues],
            "recovery_tasks": recovery_tasks,
            "status": "fail" if any(i.severity == "critical" for i in issues) else (
                "warning" if any(i.severity == "warning" for i in issues) else "pass"
            ),
        }
        if args.output:
            args.output.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        if any(issue.severity == "critical" for issue in issues):
            return 2
        if args.strict and any(issue.severity == "warning" for issue in issues):
            return 3
        return 0
    except ReviewInputError as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
