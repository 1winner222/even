#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云上信号哨兵 —— 自包含，只依赖标准库，可在 GitHub Actions 里跑。

为什么要有它：
    手机里的本地进程一旦被杀，本地那套就全哑了。这一份跑在 GitHub 云上，
    每 15 分钟自己扫一遍，出信号就推到企业微信。手机不开机也能收到。

判断逻辑与本地完全一致（同一套均线/MACD/空间/破位规则）。
只有新信号才推（靠 signal_state.json 去重，工作流会把它提交回仓库）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))

# ---------------- 配置 ----------------
PRODUCTS = {"SA": "SA2701", "FG": "FG2701"}   # 只做主力合约
MA_SHORT, MA_MID, MA_LONG = 9, 25, 69
MIN_SPACE, MAX_SPACE = 30, 20
FLAT_GAP = 12.0            # 振幅不足 -> 困盘，不提醒
STATE_FILE = "signal_state.json"
WEBHOOK = os.environ.get("WECOM_WEBHOOK", "").strip()

# ---- 收盘盘点 ----
# 云端不读手机上的 config.json，所以休市日与推送时间写在这里，改的时候两边都要改。
MARKET_CLOSED = ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
                 "2026-10-05", "2026-10-06", "2026-10-07")
BRIEF_HOUR, BRIEF_MINUTE, BRIEF_WINDOW_MIN = 15, 0, 20
# 盘点的"今天推过了没"直接记在 signal_state.json 里（键名前缀 __brief__），
# 这样云端工作流不需要改，本来就提交这个文件。
BRIEF_KEY = "__brief__"

_ACTIONABLE = {"做空开仓", "空单平仓", "做多开仓", "多单持仓", "全平多单",
               "半平多单", "放弃开空", "反手开空", "观察开空",
               "下穿前低", "上穿前高"}


def now_cn():
    return datetime.now(CN)


# ---------------- 数据 ----------------
def _get(url, referer="https://finance.sina.com.cn", encoding="gbk"):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": referer})
    return urllib.request.urlopen(req, timeout=20).read().decode(encoding, "replace")


def fetch_bars(code, period):
    """取新浪 K 线，返回 [{dt, open, high, low, close, volume}]，只留已收盘 + 当前一根。"""
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
           f"var%20_/InnerFuturesNewService.getFewMinLine?symbol={code}&type={period}")
    raw = _get(url)
    s, e = raw.find("("), raw.rfind(")")
    if s < 0 or e < 0:
        return []
    data = json.loads(raw[s + 1:e])
    out = []
    for b in data:
        try:
            # 新浪字段是 "d":"2026-01-19 11:15:00"（日期+时间在同一个字段里）
            dt = datetime.strptime(b["d"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
            out.append({"dt": dt, "open": float(b["o"]), "high": float(b["h"]),
                        "low": float(b["l"]), "close": float(b["c"]),
                        "volume": float(b.get("v") or 0)})
        except Exception:  # noqa: BLE001
            continue
    return out


# ---------------- 指标 ----------------
def sma(v, n):
    out = [None] * len(v)
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(v, n):
    out = [None] * len(v)
    if len(v) < n:
        return out
    k = 2.0 / (n + 1)
    out[n - 1] = sum(v[:n]) / n
    for i in range(n, len(v)):
        out[i] = out[i - 1] + k * (v[i] - out[i - 1])
    return out


def macd(v, f=12, s=26, sig=9):
    ef, es = ema(v, f), ema(v, s)
    dif = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    idx = [i for i, x in enumerate(dif) if x is not None]
    dea = [None] * len(dif)
    if len(idx) >= sig:
        e = ema([dif[i] for i in idx], sig)
        for k, i in enumerate(idx):
            dea[i] = e[k]
    return dif, dea


def dmi(bars, n=14):
    L = len(bars)
    pdi = [None] * L
    mdi = [None] * L
    adx = [None] * L
    tr, pdm, mdm = [0.0] * L, [0.0] * L, [0.0] * L
    for i in range(1, L):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        ph, pl = bars[i - 1]["high"], bars[i - 1]["low"]
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
        up, dn = h - ph, pl - l
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
    if L <= n:
        return adx, pdi, mdi
    atr = sum(tr[1:n + 1]) / n
    ap, am = sum(pdm[1:n + 1]) / n, sum(mdm[1:n + 1]) / n
    dxs = []
    for i in range(n + 1, L):
        atr = (atr * (n - 1) + tr[i]) / n
        ap = (ap * (n - 1) + pdm[i]) / n
        am = (am * (n - 1) + mdm[i]) / n
        p = 100 * ap / atr if atr else 0
        m = 100 * am / atr if atr else 0
        pdi[i], mdi[i] = p, m
        dxs.append(100 * abs(p - m) / (p + m) if (p + m) else 0)
    if len(dxs) >= n:
        a = sum(dxs[:n]) / n
        adx[n + n] = a
        for k, dx in enumerate(dxs[n:]):
            a = (a * (n - 1) + dx) / n
            i = n + n + k + 1
            if i < L:
                adx[i] = a
    return adx, pdi, mdi


def prep(bars):
    c = [b["close"] for b in bars]
    m9, m25, m69 = sma(c, MA_SHORT), sma(c, MA_MID), sma(c, MA_LONG)
    dif, dea = macd(c)
    adx, pdi, mdi = dmi(bars)
    for i, b in enumerate(bars):
        b.update(MA9=m9[i], MA25=m25[i], MA69=m69[i],
                 DIF=dif[i], DEA=dea[i], ADX=adx[i], PDI=pdi[i], MDI=mdi[i])
    return bars


# ---------------- 判断 ----------------
def signals(bars):
    i = len(bars) - 1
    if i < 70:
        return []
    cur, prev = bars[i], bars[i - 1]
    if None in (cur["MA69"], prev["MA69"], cur["DIF"], cur["DEA"]):
        return []
    out = []

    warn_s = prev["MA9"] >= prev["MA25"] and cur["MA9"] < cur["MA25"]
    macd_dn = (prev["DIF"] >= prev["DEA"] and cur["DIF"] < cur["DEA"]
               and cur["DIF"] < 0)
    if warn_s and macd_dn and cur["MA9"] < cur["MA69"] and cur["MA25"] < cur["MA69"]:
        out.append(("做空开仓",))
    if cur["MA9"] > cur["MA25"] and abs(cur["MA9"] - cur["MA25"]) < 2:
        out.append(("空单平仓",))
    warn_l = prev["MA9"] <= prev["MA25"] and cur["MA9"] > cur["MA25"]
    if warn_l and cur["MA9"] > cur["MA69"] and cur["MA25"] > cur["MA69"]:
        out.append(("做多开仓",))
    if cur["close"] > cur["MA69"]:
        out.append(("多单持仓",))
    if prev["MA9"] >= prev["MA25"] and cur["MA9"] < cur["MA25"]:
        if (cur["MA9"] - bars[i - 2]["MA9"]) / 2 < -1.5:
            out.append(("全平多单",))
    if abs(cur["MA9"] - cur["MA25"]) < 3:
        out.append(("半平多单",))
    w = bars[max(0, i - 20):i]
    if w:
        pl = min(b["low"] for b in w)
        ph = max(b["high"] for b in w)
        if cur["low"] < pl:
            out.append(("下穿前低", f"跌破前低{pl:.0f}"))
        if cur["high"] > ph:
            out.append(("上穿前高", f"突破前高{ph:.0f}"))
    if cur["DIF"] < 0 and cur["DEA"] < 0 and (prev["DIF"] >= 0 or prev["DEA"] >= 0):
        rl = min(b["low"] for b in bars[max(0, i - 19):i + 1])
        sp = cur["close"] - rl
        out.append(("放弃开空" if sp < MAX_SPACE else
                    "反手开空" if sp > MIN_SPACE else "观察开空",))
    return [x if isinstance(x, tuple) else (x,) for x in out]


PRODUCT_ZH = {"SA": "纯碱", "FG": "玻璃", "RM": "菜粕", "UR": "尿素",
              "SM": "锰硅", "C": "玉米", "CS": "淀粉", "HC": "热卷",
              "RB": "螺纹", "M": "豆粕", "V": "PVC", "TA": "PTA"}

_TITLE_EMOJI = (
    ("下穿前低", "\U0001FA78"), ("上穿前高", "\U0001F680"),
    ("反手开空", "\U0001F504"), ("做空开仓", "\u2b07\ufe0f"),
    ("做多开仓", "\u2b06\ufe0f"), ("空单平仓", "\U0001F6D1"),
    ("全平多单", "\u26a0\ufe0f"), ("半平多单", "\u2702\ufe0f"),
    ("多单持仓", "\U0001F402"), ("放弃开空", "\U0001F914"),
    ("观察开空", "\U0001F440"),
)
_FALLBACK_EMOJI = ("\U0001F40B", "\U0001F30A", "\U0001F3A3", "\U0001F9ED",
                   "\U0001F4E1", "\U0001F52D", "\u2693", "\U0001F3AF")


def product_zh(symbol):
    key = "".join(ch for ch in str(symbol).upper() if ch.isalpha())
    return PRODUCT_ZH.get(key, str(symbol))


def title_emoji(kind, seed=""):
    for key, emo in _TITLE_EMOJI:
        if key in kind:
            return emo
    h = sum(ord(c) for c in f"{kind}{seed}")
    return _FALLBACK_EMOJI[h % len(_FALLBACK_EMOJI)]


def title_for(product, code, timeframe, kind, seed=""):
    """标题带 K 线时间 —— 去重按 K 线记，带上时间才分得清是新的一次还是延续。"""
    hm = ""
    m = re.search(r"(\d{1,2}:\d{2})", str(seed))
    if m:
        hm = " " + m.group(1)
    return (f"{title_emoji(kind, seed)}【{product_zh(product)} "
            f"{code} {timeframe}{hm}】{kind}")


def tail_phrase(kind, seed=""):
    T = {
        "down": ["黑云压城城欲摧", "山雨欲来风满楼", "风萧萧兮易水寒", "铁骑突出刀枪鸣"],
        "up": ["大风起兮云飞扬", "长风破浪会有时", "扶摇直上九万里", "星河欲转千帆舞"],
        "enter": ["三线开花缓缓「进」", "量起势成稳稳「进」", "风生水起大步「进」"],
        "exit": ["丝粘线乱趁早「离」", "月满则亏从容「退」", "花开茶凉缓缓「走」"],
        "watch": ["线缠丝绕且慢「等」", "雾里看花慢慢「观」", "稳住别慌慢慢「等」"],
    }
    if kind == "下穿前低":
        g = T["down"]
    elif kind == "上穿前高":
        g = T["up"]
    elif kind in ("做空开仓", "做多开仓", "反手开空"):
        g = T["enter"]
    elif kind in ("空单平仓", "全平多单", "半平多单"):
        g = T["exit"]
    else:
        g = T["watch"]
    return g[sum(ord(ch) for ch in kind + seed) % len(g)]


def describe(bars, kind, i=-1):
    if i < 0:
        i = len(bars) + i      # 负数下标必须先转正，否则下面切片算出来是空列表
    cur, prev = bars[i], bars[i - 1]
    m9, m25, m69 = cur["MA9"], cur["MA25"], cur["MA69"]
    if m9 > m25 > m69:
        ma = "三线多头排列"
    elif m9 < m25 < m69:
        ma = "三线空头排列"
    else:
        ma = "均线交织"
    if prev["MA9"] < prev["MA25"] and m9 > m25:
        ma = "MA9上穿MA25"
    elif prev["MA9"] > prev["MA25"] and m9 < m25:
        ma = "MA9下穿MA25"
    vol = cur["volume"] or 0
    pv = [b["volume"] or 0 for b in bars[i - 5:i]]
    av = sum(pv) / len(pv) if pv else 0
    vs = "放量" if av and vol >= av * 1.2 else ("缩量" if av and vol <= av * 0.8 else "量平")
    pos = ("快慢线皆在零轴上" if cur["DIF"] > 0 and cur["DEA"] > 0 else
           "快慢线皆在零轴下" if cur["DIF"] < 0 and cur["DEA"] < 0 else
           "快线轴上慢线轴下" if cur["DIF"] > 0 > cur["DEA"] else "快线轴下慢线轴上")
    if prev["DIF"] <= prev["DEA"] and cur["DIF"] > cur["DEA"]:
        pos += "刚金叉"
    elif prev["DIF"] >= prev["DEA"] and cur["DIF"] < cur["DEA"]:
        pos += "刚死叉"
    dmi_s = ""
    if None not in (cur["ADX"], cur["PDI"], cur["MDI"]):
        ap = bars[i - 1]["ADX"]
        d = ("ADX上行" if ap and cur["ADX"] > ap + 0.05 else
             "ADX回落" if ap and cur["ADX"] < ap - 0.05 else "ADX走平")
        dmi_s = f"｜{d}，{'+DI占优' if cur['PDI'] > cur['MDI'] else '−DI占优'}"
    w = bars[max(0, i - 19):i + 1]
    sup = min(b["low"] for b in w)
    res = max(b["high"] for b in w)
    down, up = cur["close"] - sup, res - cur["close"]

    def grade(sp):
        return ("不值得动手" if sp < 20 else "宜轻仓试" if sp < 30
                else "可正常进" if sp < 60 else "可略加")
    if kind == "上穿前高" or "多" in kind:
        sp = f"上方前高{res:.0f}，多单{grade(up)}"
    elif "空" in kind or "反手" in kind or kind == "下穿前低":
        sp = f"下方前低{sup:.0f}，空单{grade(down)}"
    else:
        sp = f"前低{sup:.0f}（空单{grade(down)}）｜前高{res:.0f}（多单{grade(up)}）"
    avg6 = max(b["high"] for b in bars[i - 5:i + 1]) - min(b["low"] for b in bars[i - 5:i + 1])
    rng = ("动能睡着啦，盘面困住不动" if avg6 < 5 else
           "来回十来点，横盘震荡" if avg6 < FLAT_GAP else
           "波动正常，方向还没挑明" if avg6 < 35 else "波动剧烈，多空正在激战")
    return "\n".join([f"{ma}｜{vs}", f"{pos}{dmi_s}", sp, rng])


# ---------------- 推送 ----------------
def push(title, body):
    if not WEBHOOK:
        print(f"[未配置 WECOM_WEBHOOK] {title}\n{body}")
        return False
    payload = json.dumps({"msgtype": "markdown",
                          "markdown": {"content": f"**{title}**\n{body}"[:4000]}}).encode()
    # 同一条连发两次（与本地一致）
    ok_any = False
    for n in range(1):   # 只发一次
        try:
            req = urllib.request.Request(WEBHOOK, data=payload,
                                         headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=10)
            if '"errcode":0' in r.read().decode("utf-8", "replace"):
                ok_any = True
        except Exception as e:  # noqa: BLE001
            print(f"  推送第{n+1}次失败: {e}")
        time.sleep(5)      # 两条之间隔 5 秒
    print(f"  推送 {title} -> {'成功' if ok_any else '失败'}")
    return ok_any


def is_flat(bars):
    w = bars[-6:]
    return (max(b["high"] for b in w) - min(b["low"] for b in w)) < FLAT_GAP


# ---------------- 收盘盘点 ----------------
# ---- 新闻筛选口径（用户 2026-10-01 定，与本地一致）----
# 只认①供需端异动 ②政府政策异动；上下游联动与宏观一律不进。
SUPPLY_DEMAND = (
    "检修", "停产", "减产", "增产", "限产", "产能", "装置", "开工", "产量",
    "库存", "累库", "去库", "补库", "进口", "出口", "关税", "需求", "订单",
    "产销", "开工率", "复产", "事故", "爆炸", "安全",
)
POLICY = (
    "政策", "环保", "能耗双控", "限电", "调控", "国常会", "发改委", "工信部",
    "生态环境部", "反倾销", "退税", "督察", "安监", "标准", "补贴", "规划",
)
BULLISH = ("检修", "停产", "限产", "减产", "去库", "库存下降", "环保限产",
           "能耗双控", "限电", "事故", "爆炸", "出口增加", "需求回暖")
BEARISH = ("增产", "复产", "产能投放", "累库", "库存增加", "库存上升",
           "需求走弱", "需求疲弱", "进口增加", "开工回升", "供应宽松")
TOPICS = (
    ("检修", "装置检修"), ("停产", "停车"), ("限产", "限产"), ("减产", "减产"),
    ("增产", "增产"), ("复产", "复产"), ("产能", "产能变动"), ("开工", "开工率"),
    ("库存", "库存"), ("累库", "累库"), ("去库", "去库"), ("进口", "进口"),
    ("出口", "出口"), ("关税", "关税"), ("需求", "需求"), ("订单", "订单"),
    ("产销", "产销率"), ("环保", "环保"), ("能耗双控", "能耗双控"),
    ("限电", "限电"), ("政策", "政件"), ("发改委", "发改委"),
    ("工信部", "工信部"), ("事故", "事故"), ("安全", "安监"),
)


def is_trading_day():
    n = now_cn()
    if n.weekday() >= 5:
        return False
    return n.strftime("%Y-%m-%d") not in MARKET_CLOSED


def _idx(bars, i):
    """把默认的负下标转成正下标。

    这里踩过坑：写成 bars[max(0, i-1)]，i=-1 时会取到 bars[0]，
    等于拿最后一根和最早一根比；而 ap = bars[i-1] if i >= 1 else None
    在 i=-1 时直接变成 None，判断永远走"走平"分支。
    """
    return i if i >= 0 else len(bars) + i


def _ma_words(bars, i=-1):
    i = _idx(bars, i)
    c, p = bars[i], bars[i - 1] if i >= 1 else bars[i]
    m9, m25, m69 = c.get("MA9"), c.get("MA25"), c.get("MA69")
    if None in (m9, m25, m69):
        return ""
    s = ("三线多头排列" if m9 > m25 > m69 else
         "三线空头排列" if m9 < m25 < m69 else "三线交织")
    if None not in (p.get("MA9"), p.get("MA25")):
        if p["MA9"] < p["MA25"] and m9 > m25:
            s += "，MA9 刚上穿 MA25"
        elif p["MA9"] > p["MA25"] and m9 < m25:
            s += "，MA9 刚下穿 MA25"
    return s


def _macd_words(bars, i=-1):
    i = _idx(bars, i)
    c, p = bars[i], bars[i - 1] if i >= 1 else bars[i]
    dif, dea = c.get("DIF"), c.get("DEA")
    if None in (dif, dea):
        return ""
    z = ("零轴上方" if dif > 0 and dea > 0 else
         "零轴下方" if dif < 0 and dea < 0 else "跨零轴")
    if None not in (p.get("DIF"), p.get("DEA")):
        if p["DIF"] <= p["DEA"] and dif > dea:
            z += "，刚金叉"
        elif p["DIF"] >= p["DEA"] and dif < dea:
            z += "，刚死叉"
    return f"MACD {z}"


def _adx_words(bars, i=-1):
    i = _idx(bars, i)
    a = bars[i].get("ADX")
    if a is None:
        return ""
    ap = bars[i - 1].get("ADX") if i >= 1 else None
    d = "上行" if ap and a > ap + 0.05 else ("回落" if ap and a < ap - 0.05 else "走平")
    pdi, mdi = bars[i].get("PDI"), bars[i].get("MDI")
    if None not in (pdi, mdi):
        d += "，" + ("+DI 占优" if pdi > mdi else "−DI 占优")
    return f"ADX {d}"


def _fetch_news(limit=30):
    import json as _json
    out = []
    for url, ref in (
        ("https://news.10jqka.com.cn/tapp/news/push/stock/", "https://news.10jqka.com.cn"),
        (f"https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size={limit}&zhibo_id=152",
         "https://finance.sina.com.cn"),
        (f"https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num={limit}&page=1",
         "https://finance.sina.com.cn"),
    ):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": ref})
            d = _json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "replace"))
            if "10jqka" in url:
                out += [(x.get("title") or "") + " " + (x.get("digest") or "")
                        for x in d.get("data", {}).get("list", []) or []]
            elif "zhibo" in url:
                out += [str(x.get("rich_text") or x.get("text") or "")
                        for x in d.get("result", {}).get("data", {}).get("feed", {}).get("list", [])]
            else:
                out += [str(x.get("title") or "") for x in d.get("result", {}).get("data", [])]
        except Exception as e:  # noqa: BLE001
            print(f"  新闻源失败 {url[:32]}: {str(e)[:40]}")
    return [t for t in out if t]


def news_brief():
    """只陈述供需与政策异动，不搬原文，不做上下游推演。空仓时只看能开仓的品种。"""
    try:
        titles = _fetch_news()
    except Exception as e:  # noqa: BLE001
        print(f"  新闻抓取失败 {e}")
        return "　抓取失败"
    if not titles:
        return "　暂无可用报道"
    # 云端不读手机持仓；默认按"能开仓"口径，即两个监控品种里波动够的那个
    scope = []
    for prod, code in PRODUCTS.items():
        try:
            d15 = prep(closed_only(fetch_bars(code, 15), 15))
            if len(d15) < 12:
                continue
            w = d15[-12:]
            hi = max(b["high"] for b in w); lo = min(b["low"] for b in w)
            if hi - lo >= 12:
                scope.append(prod)
        except Exception:  # noqa: BLE001
            pass
    if not scope:
        scope = list(PRODUCTS)
    lines = []
    for prod in scope:
        zh = product_zh(prod)
        hit = [t for t in titles
               if zh in t and any(k in t for k in SUPPLY_DEMAND + POLICY)]
        if not hit:
            lines.append(f"　{zh}（可开仓）　无驱动型大事件及政件")
            continue
        j = " ".join(hit)
        tp = [lb for kw, lb in TOPICS if kw in j][:5]
        bull = sum(1 for k in BULLISH if k in j)
        bear = sum(1 for k in BEARISH if k in j)
        tone = ("供应收缩方向，短期偏多" if bull > bear else
                "供应宽松或需求走弱，短期偏空" if bear > bull else
                "供需两向都有说法，方向未定")
        if any(k in j for k in POLICY):
            tone += "；含政件，留意执行力度"
        lines.append(f"　{zh}（可开仓）　{len(hit)} 条，涉及"
                     f"{'、'.join(tp) if tp else '相关异动'} → {tone}")
    return "\n".join(lines) if lines else "　无驱动型大事件及政件"


# ---- 现货价（生意社基差页，取"现货价格"列）----
SPOT_UNIT = {"SA": "元/吨", "FG": "元/平方米"}
_SPOT = {}


def closed_only(bars, minutes):
    """只保留已经收线的 K 线。

    云端拿的是全量，最后一根常常是正在走的那根；本地一直只用已收线的。
    两边口径必须一致，否则同一时刻会给出不同的趋势描述。
    """
    if not bars:
        return bars
    cutoff = now_cn() - timedelta(minutes=minutes)
    return [b for b in bars if b["dt"] <= cutoff] or bars


def _shape(bars):
    """是震荡整理还是趋势推进：三线走平 + 反复缠绕 = 震荡（与本地同口径）。"""
    if len(bars) < 62:
        return "数据不足"
    win = bars[-60:]
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[-14:]) / 14 if trs else 0
    if a <= 0:
        return "数据不足"
    limit = a * 0.12
    slopes = []
    for k in ("MA9", "MA25", "MA69"):
        vals = [b[k] for b in win if b.get(k) is not None]
        if len(vals) < 5:
            return "数据不足"
        slopes.append((vals[-1] - vals[0]) / (len(vals) - 1))
    flat = max(abs(v) for v in slopes) <= limit
    cross = 0
    for p, q in zip(win, win[1:]):
        if None in (p.get("MA9"), p.get("MA25"), q.get("MA9"), q.get("MA25")):
            continue
        if (p["MA9"] - p["MA25"]) * (q["MA9"] - q["MA25"]) < 0:
            cross += 1
    return "震荡整理" if (flat and cross >= 2) else "趋势推进中"


def spot_price(prod):
    if prod in _SPOT:
        return _SPOT[prod]
    zh = product_zh(prod)
    val = None
    try:
        req = urllib.request.Request("https://www.100ppi.com/sf/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://www.100ppi.com"})
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        raw = raw.replace("&nbsp;", " ")
        txt = re.sub(r"<script.*?</script>", "", raw, flags=re.S)
        txt = re.sub(r"<style.*?</style>", "", txt, flags=re.S)
        txt = re.sub(r"<[^>]+>", "\n", txt)
        lines = [l.strip() for l in txt.split("\n") if l.strip()]
        for i, l in enumerate(lines):
            if l == zh or l.startswith(zh):
                for nxt in lines[i + 1:i + 6]:
                    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)$", nxt.strip())
                    if m:
                        val = float(m.group(1))
                        break
                break
    except Exception as e:  # noqa: BLE001
        print(f"  现货价抓取失败（{zh}）: {str(e)[:50]}")
    _SPOT[prod] = val
    return val


# ---- 末尾随机句（用户提供）----
CLOSING_TAGS = (
    "沉没成本不参与重大决策", "承兑附加条件视为拒承兑", "保持怀疑，独立性高于一切",
    "一切皆有可能，但是依然要怀疑一切", "利益之所在，风险之所在",
    "权力只对权力的来源负责", "外在言行，皆为内心映射",
    "人只会为自身利益权衡取舍",
    "没投资过你人生的人，就没讨好的资格，更没平等对待的义务",
    "出钱的有话语权，出力的有建议权，而我有决策权", "做时间的朋友，",
    "艹，地球online的金币也太难获取了，", "开整   ， 开整", "快开饭了，宝贝",
    "又活过了一天。", "牛逼坏了现在，这也能行  !", "行吧， 静能生慧",
    "💰稳可周全💰", "大肥鱼🐟的生活也并非一帆风顺",
)
TAGS_KEY = "__tags__"
FW = "\u3000"


def _w(text):
    return sum(2 if ord(c) > 0x2E80 else 1 for c in text)


def _center(text, width):
    pad = max(0, width - _w(text))
    left = (pad // 2) // 2 * 2
    right = pad - left
    return FW * (left // 2) + text + FW * (right // 2) + (" " if right % 2 else "")


def closing_tag(state):
    """每次只发一句，顺序随机，19 句跑完一轮才重来。"""
    import random
    used = [i for i in (state.get(TAGS_KEY) or [])
            if isinstance(i, int) and 0 <= i < len(CLOSING_TAGS)]
    pool = [i for i in range(len(CLOSING_TAGS)) if i not in used]
    if not pool:
        pool = list(range(len(CLOSING_TAGS)))
        used = []
    idx = random.choice(pool)
    used.append(idx)
    state[TAGS_KEY] = used
    line = "-  " + CLOSING_TAGS[idx] + "  -"
    width = max(_w("-  " + t + "  -") for t in CLOSING_TAGS)
    return _center(line, width)


def compose_brief(state):
    n = now_cn()
    wd = "一二三四五六日"[n.weekday()]
    out = [f"\U0001F4CA **{n:%m-%d} \u5468{wd}**", ""]
    for prod, code in PRODUCTS.items():
        try:
            d4 = prep(closed_only(fetch_bars(code, 240), 240))
            d60 = prep(closed_only(fetch_bars(code, 60), 60))
            if len(d4) < 30:
                continue
            out.append(f"**{product_zh(prod)} {code}**")
            # 今日趋势：标签独占一行，内容另起一行缩进六字
            out.append("\u3000今日趋势")
            segs = [_ma_words(d4), _macd_words(d4), _adx_words(d4)]
            p60 = _ma_words(d60) if len(d60) > 2 else ""
            if p60:
                segs.append(f"60分{p60}")
            out.append("\u3000\u3000\u3000\u3000\u3000\u3000"
                       + "；".join([x for x in segs if x]))
            wk = d4[-10:]
            wo, wc = wk[0]["open"], wk[-1]["close"]
            chg = (wc - wo) / wo * 100 if wo else 0
            tone = "上行" if chg > 0.5 else ("下行" if chg < -0.5 else "横向")
            hi = max(b["high"] for b in wk)
            lo = min(b["low"] for b in wk)
            shape = _shape(d4)
            out.append(f"\u3000本周趋势\u3000本周{tone} {abs(chg):.1f}%，"
                       f"周内 {lo:.0f}~{hi:.0f}，{shape}")
            sp = spot_price(prod)
            unit = SPOT_UNIT.get(prod, "元/吨")
            sp_txt = f"{sp:g} {unit}" if isinstance(sp, (int, float)) else "—（未取到）"
            out.append(f"\u3000现货收盘 {sp_txt}\u3000期货收盘 {d4[-1]['close']:.0f}")
            out.append("")
        except Exception as e:  # noqa: BLE001
            out.append(f"\u3000{prod} 取数失败：{str(e)[:50]}")
            out.append("")
    out.append("**消息面**")
    out.append(news_brief())
    out.append("")
    out.append(closing_tag(state))
    return "\n".join(out)


def brief_due(state):
    if not is_trading_day():
        return False
    n = now_cn()
    cur = n.hour * 60 + n.minute
    tgt = BRIEF_HOUR * 60 + BRIEF_MINUTE
    if not (tgt <= cur <= tgt + BRIEF_WINDOW_MIN):
        return False
    return state.get("last") != n.strftime("%Y-%m-%d")


def main():
    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        state = {}
    # ---- 收盘盘点：交易日 15:00 推一条，休市一律不推 ----
    bstate = state.get(BRIEF_KEY) or {}
    if brief_due(bstate):
        print("  到收盘盘点时间，开始生成")
        body = compose_brief(state)
        push("收盘盘点", body)
        bstate["last"] = now_cn().strftime("%Y-%m-%d")
        state[BRIEF_KEY] = bstate
        changed = True
        print("  盘点状态已并入 signal_state.json")
    elif not is_trading_day():
        print(f"  今日休市（{now_cn():%Y-%m-%d}），收盘盘点静默")
    changed = False
    if not is_trading_day():
        print(f"  今日休市（{now_cn():%Y-%m-%d}），不扫描信号")
        if changed:
            json.dump(state, open(STATE_FILE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        return 0
    for prod, code in PRODUCTS.items():
        for period, label in ((240, "4小时"), (60, "60分")):
            try:
                bars = prep(fetch_bars(code, period))
            except Exception as e:  # noqa: BLE001
                print(f"  {code}/{label} 取数失败 {e}")
                continue
            if len(bars) < 71:
                continue
            bar = bars[-1]["dt"].strftime("%Y-%m-%d %H:%M")
            sigs = signals(bars)
            print(f"  {code}/{label} {bar} 收{bars[-1]['close']:.0f} -> "
                  f"{[k for k, *_ in sigs] or '无信号'}")
            brk = any(k in ("下穿前低", "上穿前高") for k, *_ in sigs)
            if is_flat(bars) and not brk:
                print("      困盘静默")
                continue
            for k, *_ in sigs:
                if k not in _ACTIONABLE:
                    continue
                key = f"{code}|{period}|{bar}|{k}"
                if state.get(key):
                    continue
                state[key] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
                changed = True
                push(title_for(prod, code, label, k, bar),
                     describe(bars, k) + "\n\n" + tail_phrase(k, bar))
    if len(state) > 400:
        for k in sorted(state)[:len(state) - 400]:
            state.pop(k, None)
        changed = True
    if changed:
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("  已更新 signal_state.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
