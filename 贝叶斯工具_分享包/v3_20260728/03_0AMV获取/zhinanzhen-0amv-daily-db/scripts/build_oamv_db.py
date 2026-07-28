# -*- coding: utf-8 -*-
"""
指南针 0AMV (活跃市值) 日线数据库构建器
=========================================
数据源: D:\\AIlianghua\\OAMV\\ANALYSE\\Data\\ChinaStk\\Z_SK\\day.vdat
        (指南针私有二进制; 0AMV 日线被切成多个 ~250 交易日的块,
         每块前 8 字节是代码标识 'Z_SK0AMV', 其后为 28 字节/条的记录)

记录格式 (28 字节 = 7 × 4 字节):
    [0:4 ] int32  日期 (YYYYMMDD)
    [4:8 ] float32 开 (亿元)
    [8:12] float32 高 (亿元)
    [12:16] float32 低 (亿元)
    [16:20] float32 收 (亿元)
    [20:24] float32 量 (原始整数, /1e8 = 亿)
    [24:28] float32 额 (原始整数, /1e8 = 亿元)

输出:
    - 0AMV日线数据库_2015至今.csv  (主交付: 数据库文件)
    - 0AMV日K图_2015至今.png       (蜡烛图)
    - 0AMV日K图_2015至今.html      (交互图, 含十字光标 + 存图按钮)
"""
import struct, re, csv, os

BASE = r"D:\AIlianghua\OAMV"
DAY_VDAT = os.path.join(BASE, "ANALYSE", "Data", "ChinaStk", "Z_SK", "day.vdat")
CODE = b"Z_SK0AMV"
START_DATE = 20150101          # 数据库起始日期 (含)
OUT_CSV = os.path.join(BASE, "0AMV日线数据库_2015至今.csv")
OUT_PNG = os.path.join(BASE, "0AMV日K图_2015至今.png")
OUT_HTML = os.path.join(BASE, "0AMV日K图_2015至今.html")


def parse_block(d, start, N):
    """从 start (第一条记录的字节位置) 向后解析 0AMV 日线记录, 直到非法为止。"""
    recs = []
    p = start
    while p + 28 <= N:
        date = struct.unpack("<i", d[p:p + 4])[0]
        if not (19900101 <= date <= 20270101):
            break
        o = struct.unpack("<f", d[p + 4:p + 8])[0]
        h = struct.unpack("<f", d[p + 8:p + 12])[0]
        lo = struct.unpack("<f", d[p + 12:p + 16])[0]
        c = struct.unpack("<f", d[p + 16:p + 20])[0]
        # 0AMV 量级过滤 + OHLC 一致性
        if not (5000 < o < 2_000_000 and 5000 < h < 2_000_000
                and 0 < lo < 2_000_000 and 5000 < c < 2_000_000):
            break
        if not (lo <= o <= h and lo <= c <= h):
            break
        f5 = struct.unpack("<f", d[p + 20:p + 24])[0]
        f6 = struct.unpack("<f", d[p + 24:p + 28])[0]
        recs.append((date, o, h, lo, c, f5, f6))
        p += 28
    return recs


def extract_all():
    d = open(DAY_VDAT, "rb").read()
    N = len(d)
    merged = {}
    for h in [m.start() for m in re.finditer(CODE, d)]:
        for off in (8, 48, 88, 128):
            recs = parse_block(d, h + off, N)
            if len(recs) >= 20:
                for r in recs:
                    merged[r[0]] = r
                break
    return merged


def main():
    merged = extract_all()
    dates = sorted(k for k in merged if k >= START_DATE)
    print(f"提取到 0AMV 日线交易日数 (>= {START_DATE}): {len(dates)}")
    print(f"时间范围: {dates[0]} ~ {dates[-1]}" if dates else "无数据")

    rows = []
    prev_close = None
    for dt in dates:
        o, h, lo, c, f5, f6 = merged[dt][1:7]
        vol_yi = f5 / 1e8
        amt_yi = f6 / 1e8
        if prev_close:
            chg = (c - prev_close) / prev_close * 100.0
            amp = (h - lo) / prev_close * 100.0
        else:
            chg = None
            amp = None
        prev_close = c
        rows.append([dt, round(o, 2), round(h, 2), round(lo, 2), round(c, 2),
                     round(vol_yi, 2), round(amt_yi, 2),
                     (round(chg, 2) if chg is not None else ""),
                     (round(amp, 2) if amp is not None else "")])

    # ---- 写 CSV ----
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["日期", "开(亿元)", "高(亿元)", "低(亿元)", "收(亿元)",
                    "量(亿)", "额(亿元)", "涨幅%", "振幅%"])
        w.writerows(rows)
    print(f"[OK] 已写出数据库: {OUT_CSV}  ({len(rows)} 行)")

    # ---- 校验锚点 ----
    chk = {
        20250818: (165633.6, 173341.2, 165633.6, 172964.0, 1776.87, 27636.66),
        20260525: (247496.2, 251840.8, 247147.3, 250198.9, None, None),
        20241115: (168270.3, 170430.7, 162011.9, 162051.6, None, None),
        20231211: (76330.9, 78439.8, 76260.2, 78439.8, None, None),
    }
    bydate = {r[0]: r for r in rows}
    print("\n==== 用户锚点校验 ====")
    for dt, (o, h, l, c, v, a) in chk.items():
        if dt in bydate:
            r = bydate[dt]
            ok = abs(r[1]-o) < 1 and abs(r[2]-h) < 1 and abs(r[3]-l) < 1 and abs(r[4]-c) < 1
            extra = ""
            if v is not None:
                extra = f" 量{r[5]}亿(对{v}) 额{r[6]}亿(对{a})"
            print(f"  {dt}: 抽=({r[1]},{r[2]},{r[3]},{r[4]}) 用户=({o},{h},{l},{c}) -> {'OK' if ok else 'ERR'}{extra}")

    # ---- 渲染 PNG ----
    try:
        render_png(rows, OUT_PNG)
        print(f"[OK] 已生成图表: {OUT_PNG}")
    except Exception as e:
        print(f"[!] PNG 渲染跳过: {e}")

    # ---- 渲染 HTML ----
    render_html(rows, OUT_HTML)
    print(f"[OK] 已生成交互图: {OUT_HTML}")


def render_png(rows, path):
    """红涨绿跌蜡烛图 + MA5/10/30 + 成交量 (Pillow)。"""
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1800, 1000
    PAD_L, PAD_R, PAD_T, PAD_B = 70, 70, 60, 160
    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B
    vol_h = int(plot_h * 0.22)
    price_h = plot_h - vol_h - 20

    dates = [str(r[0]) for r in rows]
    opens = [r[1] for r in rows]
    highs = [r[2] for r in rows]
    lows = [r[3] for r in rows]
    closes = [r[4] for r in rows]
    vols = [r[5] for r in rows]

    def ma(arr, n):
        out = []
        for i in range(len(arr)):
            if i < n - 1:
                out.append(None)
            else:
                out.append(sum(arr[i - n + 1:i + 1]) / n)
        return out

    ma5, ma10, ma30 = ma(closes, 5), ma(closes, 10), ma(closes, 30)

    pmin, pmax = min(lows), max(highs)
    pad = (pmax - pmin) * 0.05
    pmin -= pad; pmax += pad
    vmax = max(vols) * 1.1 or 1

    def x(i):
        return PAD_L + (plot_w * i / (len(rows) - 1)) if len(rows) > 1 else PAD_L
    def yp(v):
        return PAD_T + price_h * (1 - (v - pmin) / (pmax - pmin))
    def yv(v):
        return PAD_T + price_h + 20 + vol_h * (1 - v / vmax)

    img = Image.new("RGB", (W, H), (17, 19, 24))
    dr = ImageDraw.Draw(img)
    try:
        fnt = ImageFont.truetype("c:/windows/fonts/msyh.ttc", 18)
        fnt_s = ImageFont.truetype("c:/windows/fonts/msyh.ttc", 13)
    except Exception:
        fnt = ImageFont.load_default(); fnt_s = fnt

    UP = (231, 76, 60); DOWN = (46, 204, 113)
    # 网格 + 价格刻度
    for g in range(6):
        yy = PAD_T + price_h * g / 5
        dr.line([(PAD_L, yy), (W - PAD_R, yy)], fill=(40, 44, 52))
        val = pmax - (pmax - pmin) * g / 5
        dr.text((W - PAD_R + 6, yy - 9), f"{val:,.0f}", fill=(150, 160, 175), font=fnt_s)
    # 蜡烛
    bw = max(1.0, plot_w / len(rows) * 0.7)
    for i in range(len(rows)):
        up = closes[i] >= opens[i]
        col = UP if up else DOWN
        xc = x(i)
        dr.line([(xc, yp(highs[i])), (xc, yp(lows[i]))], fill=col, width=1)
        yo, yc = yp(opens[i]), yp(closes[i])
        top, bot = min(yo, yc), max(yo, yc)
        dr.rectangle([xc - bw / 2, top, xc + bw / 2, bot], fill=col)
        # 量
        dr.rectangle([xc - bw / 2, yv(vols[i]), xc + bw / 2, PAD_T + price_h + 20 + vol_h], fill=col)
    # MA 线
    def draw_ma(arr, color):
        pts = [(x(i), yp(arr[i])) for i in range(len(arr)) if arr[i] is not None]
        for j in range(1, len(pts)):
            dr.line([pts[j - 1], pts[j]], fill=color, width=2)
    draw_ma(ma5, (235, 235, 235))
    draw_ma(ma10, (241, 196, 15))
    draw_ma(ma30, (155, 89, 182))
    # 标题 + 轴
    dr.text((PAD_L, 16), "指南针 0AMV (活跃市值) 日K线  2015-至今  [红涨绿跌]", fill=(235, 238, 245), font=fnt)
    dr.text((PAD_L, PAD_T + price_h + 20 + vol_h + 30), "成交量 (亿)", fill=(150, 160, 175), font=fnt_s)
    # 日期刻度
    n = len(rows)
    for k in range(0, n, max(1, n // 8)):
        dr.text((x(k) - 20, H - PAD_B + 10), dates[k][:4] + "-" + dates[k][4:6], fill=(150, 160, 175), font=fnt_s)
    img.save(path)


def render_html(rows, path):
    data = [[str(r[0]), r[1], r[2], r[3], r[4], r[5], r[6]] for r in rows]
    n = len(rows)
    closes = [r[4] for r in rows]
    def ma_seq(arr, m):
        out = []
        for i in range(n):
            lo = max(0, i - m + 1)
            out.append(round(sum(arr[lo:i + 1]) / (i - lo + 1), 1))
        return out
    ma5, ma10, ma30 = ma_seq(closes, 5), ma_seq(closes, 10), ma_seq(closes, 30)
    html = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>0AMV 活跃市值 日K线</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>body{margin:0;background:#11131a;color:#e6ebf2;font-family:'Microsoft YaHei',sans-serif}
#c{width:100vw;height:100vh}</style></head>
<body><div id="c"></div><script>
var data=%s;
var dates=data.map(d=>d[0]);
var k=data.map(d=>[d[1],d[2],d[3],d[4]]);
var vol=data.map(d=>d[5]);
var ch=echarts.init(document.getElementById('c'),'dark');
ch.setOption({
  backgroundColor:'#11131a',
  animation:false,
  legend:{data:['MA5','MA10','MA30'],top:8,textStyle:{color:'#cfd6e0'}},
  tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
  grid:[{left:60,right:60,top:50,height:'62%%'},{left:60,right:60,top:'74%%',height:'16%%'}],
  xAxis:[{type:'category',data:dates,axisLabel:{color:'#9aa4b2'}},
         {type:'category',gridIndex:1,data:dates,axisLabel:{show:false}}],
  yAxis:[{scale:true,axisLabel:{color:'#9aa4b2'}},
         {gridIndex:1,scale:true,axisLabel:{color:'#9aa4b2'}}],
  dataZoom:[{type:'inside',xAxisIndex:[0,1]},{type:'slider',xAxisIndex:[0,1],bottom:10}],
  series:[
    {name:'K',type:'candlestick',data:k,
     itemStyle:{color:'#e74c3c',color0:'#2ecc71',borderColor:'#e74c3c',borderColor0:'#2ecc71'}},
    {name:'MA5',type:'line',data:%s,smooth:true,showSymbol:false,lineStyle:{width:1,color:'#ebedef'}},
    {name:'MA10',type:'line',data:%s,smooth:true,showSymbol:false,lineStyle:{width:1,color:'#f1c40f'}},
    {name:'MA30',type:'line',data:%s,smooth:true,showSymbol:false,lineStyle:{width:1,color:'#9b59b6'}},
    {name:'量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:vol,itemStyle:{color:'#5b8def'}}
  ]
});
</script></body></html>""" % (data, ma5, ma10, ma30)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
