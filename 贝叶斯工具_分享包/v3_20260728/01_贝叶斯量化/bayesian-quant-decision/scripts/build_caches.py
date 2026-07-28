# -*- coding: utf-8 -*-
"""
通用「换股票一键」缓存构建器 —— 贝叶斯量化决策引擎配套 (v1.12)
=================================================================
把一份 raw_input JSON（agent 读 MCP 后填入的原始数值 + AI 判定项）
一次性转写成 run_report.py 需要的全部 11 类真实数据缓存，自动完成：
  · schema 组装（保证 key 名/文件名/编码 100% 正确，杜绝手写 py 的语法/笔误）
  · 派生计算（eps_ttm / 毛利率 / 净利率 / ROE / 资产负债率 / OCF比 /
              融资余额占流通市值比 / corp_quality 由 FunmScore 映射 …）
  · 输出到  <skill>/data/{category}/{std}.json

用法:
    python build_caches.py <raw_input.json>
    # 例：python build_caches.py D:\\AILIANGHUA\\贝叶斯工具\\raw_600428.json

raw_input.json 顶层字段(见 references/raw_input_template.json)：
    std / code / code_ex / name / date / close_price
    valuation / corp / industry / north / margin / psi / news /
    chip / consensus / shareholder / fund_flow
缺 sentiment（故意）——留空让 bayesian_engine 用 Tec+资金+动量合成 ACSI。
每一类都可整块省略，脚本会跳过并提示（缺失越多，报告默认项越多）。
"""
import json
import os
import sys

# 缓存根目录（skill 自身 data/）
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
ROOT = os.path.abspath(ROOT)


def _round(x, n):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


def _write(category, std, obj):
    d = os.path.join(ROOT, category)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, std + ".json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print(u"  [写入] %-12s -> %s" % (category, p))


def build(raw):
    std = raw["std"]
    code = raw.get("code", std.replace("_", "."))
    code_ex = raw.get("code_ex", "")
    name = raw.get("name", "")
    date = raw.get("date", "")
    close = raw.get("close_price")
    written = []

    # ---------- 1. valuation ----------
    if "valuation" in raw:
        v = dict(raw["valuation"])
        mc = v.get("market_cap")
        ts = v.get("total_shares")
        fmc = v.get("float_market_cap", mc)
        fs = v.get("float_shares", ts)
        eps = v.get("eps_ttm")
        if eps is None and v.get("np_ttm") and ts:
            eps = _round(v["np_ttm"] / ts, 4)
        out = {
            "PE_TTM": v.get("PE_TTM"),
            "PB": v.get("PB"),
            "PS_TTM": v.get("PS_TTM"),
            "BPS": v.get("BPS"),
            "eps_ttm": eps,
            "market_cap": mc,
            "float_market_cap": fmc,
            "total_shares": ts,
            "float_shares": fs,
            "f_score": v.get("f_score", 7),
            "source": v.get("source", ""),
        }
        _write("valuation", std, out)
        written.append("valuation")

    # ---------- 2. corp ----------
    if "corp" in raw:
        c = dict(raw["corp"])
        fy = dict(c.get("fy2025", {}))
        rev = fy.get("revenue")
        npp = fy.get("np_parent")
        gp = fy.get("gross_profit")
        eq = fy.get("equity")
        ta = fy.get("total_assets")
        tl = fy.get("total_liab")
        ocf = fy.get("ocf")
        fin = {
            "revenue": rev,
            "np_parent": npp,
            "np_deduct": fy.get("np_deduct"),
            "gross_margin": _round(gp / rev, 4) if gp and rev else fy.get("gross_margin"),
            "net_margin": _round(npp / rev, 4) if npp and rev else fy.get("net_margin"),
            "roe": _round(npp / eq, 4) if npp and eq else fy.get("roe"),
            "debt_ratio": _round(tl / ta, 4) if tl and ta else fy.get("debt_ratio"),
            "goodwill": fy.get("goodwill", 0.0),
            "ocf": ocf,
            "ocf_np_ratio": _round(ocf / npp, 2) if ocf and npp else fy.get("ocf_np_ratio"),
            "fcff": fy.get("fcff"),
            "interest_bearing_debt": fy.get("interest_bearing_debt"),
            "inventory": fy.get("inventory"),
            "receivables": fy.get("receivables"),
            "note": fy.get("note", "FY2025 年报；比率由本脚本据三大报表派生"),
        }
        scores = c.get("scores", {})
        funm = scores.get("funm")
        cq = c.get("corp_quality")
        if cq is None:
            cq = 1.0 if (funm is not None and funm >= 80) else 0.0
        out = {
            "code": code,
            "name": name,
            "date": date,
            "business": c.get("business", ""),
            "industry": c.get("industry", ""),
            "sector": c.get("sector", c.get("industry", "")),
            "board": c.get("board", ""),
            "listed_date": c.get("listed_date", ""),
            "scores": scores,
            "market_cap": c.get("market_cap", raw.get("valuation", {}).get("market_cap")),
            "float_market_cap": c.get("float_market_cap", raw.get("valuation", {}).get("float_market_cap") or raw.get("valuation", {}).get("market_cap")),
            "total_shares": c.get("total_shares", raw.get("valuation", {}).get("total_shares")),
            "float_shares": c.get("float_shares", raw.get("valuation", {}).get("float_shares") or raw.get("valuation", {}).get("total_shares")),
            "target_price": c.get("target_price"),
            "finance_fy2025": fin,
            "revenue_trend_ttm": c.get("revenue_trend_ttm", []),
            "moat": c.get("moat", {}),
            "risk_flags": c.get("risk_flags", []),
            "positive_flags": c.get("positive_flags", []),
            "corp_quality": cq,
            "corp_quality_note": c.get("corp_quality_note",
                u"由 FunmScore(%s) 映射：>=80→+1，否则 0" % funm),
            "source": c.get("source", ""),
        }
        _write("corp", std, out)
        written.append("corp")

    # ---------- 3. industry ----------
    if "industry" in raw:
        ind = dict(raw["industry"])
        ind.setdefault("date", date)
        _write("industry", std, ind)
        written.append("industry")

    # ---------- 4. north（引擎读 cur.holding_ratio_pct 等小写键） ----------
    if "north" in raw:
        n = dict(raw["north"])
        n.setdefault("code", code)
        n.setdefault("name", name)
        n.setdefault("date", date)
        # 若误传 camelCase，做一次兜底映射
        _CAMEL = {
            "HoldingRatio": "holding_ratio_pct", "HoldingShares": "holding_shares",
            "HoldingMarketCap": "holding_cap", "HoldingCap": "holding_cap",
            "ShareChgQ": "shares_chg_q", "CapChgQ": "cap_chg_q",
            "ShareChgY": "shares_chg_y", "CapChgY": "cap_chg_y", "EndDate": "end_date",
        }
        for slot in ("cur", "prev"):
            blk = n.get(slot)
            if isinstance(blk, dict):
                for ck, lk in _CAMEL.items():
                    if ck in blk and lk not in blk:
                        blk[lk] = blk.pop(ck)
        _write("north", std, n)
        written.append("north")

    # ---------- 5. margin（融资融券，自动算占比/净买/合计） ----------
    if "margin" in raw:
        m = dict(raw["margin"])
        fb = m.get("finance_balance")
        sb = m.get("security_balance", 0.0) or 0.0
        fmc = m.get("float_market_cap") or raw.get("valuation", {}).get("float_market_cap") or raw.get("valuation", {}).get("market_cap")
        buy = m.get("finance_buy_today")
        refund = m.get("finance_refund_today")
        out = {
            "code": code,
            "name": name,
            "date": date,
            "finance_balance": fb,
            "security_balance": sb,
            "total_balance": m.get("total_balance", (fb + sb) if fb is not None else None),
            "finance_balance_dod": m.get("finance_balance_dod"),
            "security_balance_dod": m.get("security_balance_dod"),
            "finance_buy_today": buy,
            "finance_refund_today": refund,
            "finance_net_buy_today": m.get("finance_net_buy_today",
                (buy - refund) if (buy is not None and refund is not None) else None),
            "close_price": m.get("close_price", close),
            "float_market_cap": fmc,
            "finance_balance_ratio": m.get("finance_balance_ratio",
                _round(fb / fmc, 6) if (fb and fmc) else None),
            "source": m.get("source", "westock data_fund_margin + tdx 总市值"),
            "note": m.get("note", ""),
        }
        _write("margin", std, out)
        written.append("margin")

    # ---------- 6. psi（AI 判定，原样透传） ----------
    if "psi" in raw:
        p = dict(raw["psi"])
        p.setdefault("psi_policy_type", "structural")
        p.setdefault("psi_months_since", 6)
        p.setdefault("source", "AI 据近期政策/行业新闻判定")
        p.setdefault("date", date)
        _write("psi", std, p)
        written.append("psi")

    # ---------- 7. news ----------
    if "news" in raw:
        _write("news", std, raw["news"])
        written.append("news")

    # ---------- 8. chip ----------
    if "chip" in raw:
        ch = dict(raw["chip"])
        ch.setdefault("code", code_ex or code)
        ch.setdefault("name", name)
        ch.setdefault("date", date)
        ch.setdefault("closePrice", close)
        _write("chip", std, ch)
        written.append("chip")

    # ---------- 9. consensus ----------
    if "consensus" in raw:
        _write("consensus", std, raw["consensus"])
        written.append("consensus")

    # ---------- 10. shareholder（科创/中报前可空） ----------
    if "shareholder" in raw:
        sh = dict(raw["shareholder"])
        sh.setdefault("code", code_ex or code)
        sh.setdefault("name", name)
        _write("shareholder", std, sh)
        written.append("shareholder")

    # ---------- 11. fund_flow（原始数组，30日主力资金流） ----------
    if "fund_flow" in raw:
        ff = raw["fund_flow"]
        if isinstance(ff, list):
            ff = {"code": code_ex or code, "data": ff}
        _write("fund_flow", std, ff)
        written.append("fund_flow")

    return written


def main():
    if len(sys.argv) < 2:
        print(u"用法: python build_caches.py <raw_input.json>")
        sys.exit(1)
    path = sys.argv[1]
    if not os.path.exists(path):
        print(u"[错误] 找不到输入文件: %s" % path)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    print(u"=== 构建缓存: %s (%s) 目标目录 %s ===" % (raw.get("name", ""), raw.get("std", ""), ROOT))
    written = build(raw)
    print(u"\n=== 完成，共写入 %d/11 类缓存: %s ===" % (len(written), ", ".join(written)))
    skipped = [c for c in ["valuation", "corp", "industry", "north", "margin",
                           "psi", "news", "chip", "consensus", "shareholder", "fund_flow"]
               if c not in written]
    if skipped:
        print(u"[提示] 未提供(将走默认/合成): %s" % ", ".join(skipped))
    print(u"[提示] sentiment 故意留空 -> 引擎用 Tec+资金+动量合成 ACSI")
    print(u"\n下一步:  cd scripts && python run_report.py %s %s" % (
        raw.get("std", "").split("_")[0], raw.get("name", "")))


if __name__ == "__main__":
    main()
