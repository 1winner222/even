#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云上信号哨兵 -- 自包含,只依赖标准库,可在 GitHub Actions 里跑.

为什么要有它:
    手机里的本地进程一旦被杀,本地那套就全哑了.这一份跑在 GitHub 云上,
    每 15 分钟自己扫一遍,出信号就推到企业微信.手机不开机也能收到.

判断逻辑与本地完全一致(同一套均线/MACD/空间/破位规则).
只有新信号才推(靠 signal_state.json 去重,工作流会把它提交回仓库).
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
FLAT_GAP = 12.0            # 振幅不足 -> 困盘,不提醒
STATE_FILE = "signal_state.json"
WEBHOOK = os.environ.get("WECOM_WEBHOOK", "").strip()

# ---- 收盘盘点 ----
# 云端不读手机上的 config.json,所以休市日与推送时间写在这里,改的时候两边都要改.
MARKET_CLOSED = ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
                 "2026-10-05", "2026-10-06", "2026-10-07")
# 时间用户 2026-10-04 从 15:00 提前到 14:45 -- Actions 执行会慢一点,
# 排在 14:45 实际发出来正好接近收盘.窗口 20 分钟不变.
BRIEF_HOUR, BRIEF_MINUTE, BRIEF_WINDOW_MIN = 14, 45, 20
# 盘点的"今天推过了没"直接记在 signal_state.json 里(键名前缀 __brief__),
# 这样云端工作流不需要改,本来就提交这个文件.
BRIEF_KEY = "__brief__"

# 云端跑在 GitHub 上,拿不到持仓信息 -- 反手必须知道手上有没有多单才成立,
# 所以这里把"反手开空"摘掉:不知道就不写(用户 2026-10-04 定).
# 本地 sa_notify 的 ACTIONABLE 仍保留它,但加了一道"持多单才推"的闸.
_ACTIONABLE = {"做空开仓", "空单平仓", "做多开仓", "多单持仓", "全平多单",
               "半平多单", "放弃开空", "观察开空",
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
    """取新浪 K 线,返回 [{dt, open, high, low, close, volume}],只留已收盘 + 当前一根."""
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
            # 新浪字段是 "d":"2026-01-19 11:15:00"(日期+时间在同一个字段里)
            # "p" = 持仓量(以前被丢掉,用户 2026-10-04 指出副图里本来就有)
            dt = datetime.strptime(b["d"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
            out.append({"dt": dt, "open": float(b["o"]), "high": float(b["h"]),
                        "low": float(b["l"]), "close": float(b["c"]),
                        "volume": float(b.get("v") or 0),
                        "oi": float(b["p"]) if b.get("p") else None})
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


RSI_PERIOD = 14                # 与本地一致
RSI_FLAT = 0.15                # 方向死区:变化小于它算走平


def RSI(v, n=RSI_PERIOD):
    """Wilder RSI,零依赖.前 n 根为 None.与本地 sa_strategy.RSI 同算法."""
    out = [None] * len(v)
    if len(v) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        ch = v[i] - v[i - 1]
        g += max(ch, 0.0)
        l += max(-ch, 0.0)
    g /= n
    l /= n
    out[n] = 100.0 if l == 0 else 100.0 - 100.0 / (1 + g / l)
    for i in range(n + 1, len(v)):
        ch = v[i] - v[i - 1]
        g = (g * (n - 1) + max(ch, 0.0)) / n
        l = (l * (n - 1) + max(-ch, 0.0)) / n
        out[i] = 100.0 if l == 0 else 100.0 - 100.0 / (1 + g / l)
    return out


def rsi_words(bars, i=-1, pdi=None, mdi=None):
    """RSI 方向 + DMI 占优 -> 中文一句话.与本地 sa_strategy.rsi_words 同口径.

    主体是[占优的一方],动词说的是[被点名那一方]的动能在涨还是落:
        多头占优 + RSI 上行 -> 多头动能上升
        多头占优 + RSI 下行 -> 多头动能下跌   (顶背离时读出来就是这个)
        空头占优 + RSI 上行 -> 空头动能下跌   (空头占优但空头在减弱 = 反弹)
        空头占优 + RSI 下行 -> 空头动能上升
    不写数值,只写文字(用户要求副图只用文字描述).
    """
    try:
        r = RSI([b["close"] for b in bars])
        if i < 0:
            i = len(bars) + i
        if i <= 0 or i >= len(r) or r[i] is None or r[i - 1] is None:
            return ""
        d = r[i] - r[i - 1]
        rsi_up = True if d > RSI_FLAT else (False if d < -RSI_FLAT else None)
        if pdi is not None and mdi is not None and pdi != mdi:
            bull = pdi > mdi
            verb = ("走平" if rsi_up is None else
                    "上升" if (bull == rsi_up) else "下跌")
            return ("多头动能" if bull else "空头动能") + verb
        return "动能" + ("回升" if rsi_up else
                        "回落" if rsi_up is False else "走平")
    except Exception as exc:  # noqa: BLE001
        # 兜底必须留痕,否则 bug 被吞成"看着正常"(用户定的规矩)
        print(f"[rsi_words 兜底] {type(exc).__name__}: {exc}")
        return ""


ADX_FLAT = 0.05                # ADX 变化死区,与本地一致


def adx_phrase(adx_now, adx_prev=None, pdi=None, mdi=None):
    """ADX + DMI 合成一句:涨势/跌势 + 在升温/在降温/走平.与本地同口径."""
    if adx_now is None:
        return ""
    if pdi is not None and mdi is not None:
        if mdi > pdi:
            subj = "跌势"
        elif pdi > mdi:
            subj = "涨势"
        else:
            return "多空僵持"
    else:
        subj = "趋势"
    if adx_prev is None:
        return subj
    d = adx_now - adx_prev
    return subj + ("在升温" if d > ADX_FLAT else
                   "在降温" if d < -ADX_FLAT else "走平")


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


PRODUCT_ZH = {'SA': '纯碱', 'FG': '玻璃', 'RM': '菜粕', 'UR': '尿素', 'SM': '锰硅', 'C': '玉米', 'CS': '淀粉', 'HC': '热卷', 'RB': '螺纹', 'M': '豆粕', 'V': 'PVC', 'TA': 'PTA', 'OI': '菜油', 'CF': '棉花', 'SR': '白糖'}

_TITLE_EMOJI = (('下穿前低', '🩸'), ('上穿前高', '🚀'), ('反手开空', '🔄'), ('做空开仓', '⬇️'), ('做多开仓', '⬆️'), ('空单平仓', '🛑'), ('全平多单', '⚠️'), ('半平多单', '✂️'), ('多单持仓', '🐂'), ('放弃开空', '🤔'), ('观察开空', '👀'), ('即将上穿', '⚡'), ('即将下穿', '🌩️'), ('已上穿', '💥'), ('已下穿', '💢'), ('MA25撞MA69', '🎯'), ('MA9撞MA25', '📡'), ('高位做空', '🏔️'), ('低位做多', '🕳️'), ('多单快撤', '🏃'), ('空单快跑', '🏃'), ('高波动', '🌊'), ('中价值', '🎣'))
_FALLBACK_EMOJI = ('🐋', '🌊', '🎣', '🧭', '📡', '🔭', '🪝', '⚓', '🧨', '🎯', '🌗', '🪶')


def product_zh(symbol):
    key = "".join(ch for ch in str(symbol).upper() if ch.isalpha())
    return PRODUCT_ZH.get(key, str(symbol))


def title_emoji(kind, seed=""):
    for key, emo in _TITLE_EMOJI:
        if key in kind:
            return emo
    h = sum(ord(c) for c in f"{kind}{seed}")
    return _FALLBACK_EMOJI[h % len(_FALLBACK_EMOJI)]


# 开仓类的标题只留一个字(用户 2026-10-04),前面加警示符让它在消息列表里跳出来
_TITLE_SHORT = {"做多开仓": "\u26a0\ufe0f多", "做空开仓": "\u26a0\ufe0f空"}


def title_for(product, code, timeframe, kind, seed=""):
    """标题带 K 线时间 -- 去重按 K 线记,带上时间才分得清是新的一次还是延续."""
    hm = ""
    m = re.search(r"(\d{1,2}:\d{2})", str(seed))
    if m:
        hm = " " + m.group(1)
    return (f"{title_emoji(kind, seed)}[{product_zh(product)} "
            f"{code} {timeframe}{hm}]{_TITLE_SHORT.get(kind, kind)}")


BREAK_LOOKBACK = 240           # 破位后往前找"下一扇门"的回看根数
SWING_K = 3                    # 局部极值半窗
SPACE_WIN = 20                 # 普通情况下区间参照的回看根数


def next_level(bars, i, side, lookback=BREAK_LOOKBACK, k=SWING_K):
    """破位之后的"下一扇门"(用户 2026-10-04 定的口径,与本地同算法).

    破了前低,刚破的那扇门已经被走过去了,不能再拿它量前方空间.
    新参照 = 在那扇门形成之前,比它更低的最近一个前低.
    找不到 -> None,调用方显示"历史最低位".
    """
    if i < 0:
        i = len(bars) + i
    lo = max(0, i - SPACE_WIN + 1)
    if side == "down":
        door = min(b["low"] for b in bars[lo:i + 1])
        di = max(j for j in range(lo, i + 1) if bars[j]["low"] == door)
        cand = [bars[j]["low"]
                for j in range(max(k, di - lookback), di)
                if bars[j]["low"] == min(b["low"] for b in bars[j - k:j + k + 1])
                and bars[j]["low"] < door]
        return max(cand) if cand else None
    door = max(b["high"] for b in bars[lo:i + 1])
    hi = max(j for j in range(lo, i + 1) if bars[j]["high"] == door)
    cand = [bars[j]["high"]
            for j in range(max(k, hi - lookback), hi)
            if bars[j]["high"] == max(b["high"] for b in bars[j - k:j + k + 1])
            and bars[j]["high"] > door]
    return min(cand) if cand else None


def space_refs(bars, i, kind):
    """第 3 行用的参照位与价差,返回 (support, down, resist, up).与本地同算法."""
    if i < 0:
        i = len(bars) + i
    win = bars[max(0, i - SPACE_WIN + 1):i + 1]
    sup = min(b["low"] for b in win)
    res = max(b["high"] for b in win)
    c = bars[i]["close"]
    if kind == "下穿前低":
        sup = next_level(bars, i, "down")
    if kind == "上穿前高":
        res = next_level(bars, i, "up")
    return (sup, None if sup is None else c - sup,
            res, None if res is None else res - c)


def worth_acting(bars, kind, i=-1):
    """正文第 3 行那个判语的布尔版.参照位与正文同源,否则尾句又会对不上."""
    sup, down, res, up = space_refs(bars, i, kind)
    if kind == "上穿前高" or "多" in kind:
        sp = up
    elif "空" in kind or "反手" in kind or kind == "下穿前低":
        sp = down
    else:
        return None
    if sp is None:
        return None
    return sp >= 20                    # 与 describe 里 grade() 的 20 点门槛一致


def tail_phrase(kind, seed="", worth=None):
    """结尾的一句话.worth=False(正文判了"不值得动手")时,
    开仓类不能再用[进]那一组,否则一条消息自相矛盾(用户 2026-10-04 指出)."""
    T = {
        "down": [
            "黑云压城城欲摧",
            "山雨欲来风满楼",
            "风萧萧兮易水寒",
            "铁骑突出刀枪鸣",
        ],
        "up": [
            "大风起兮云飞扬",
            "长风破浪会有时",
            "扶摇直上九万里",
            "星河欲转千帆舞",
        ],
        "enter": [
            "三线开花缓缓[进]",
            "量起势成稳稳[进]",
            "风生水起大步[进]",
            "雾散云开轻轻[进]",
            "东风已至扬帆[进]",
            "热热闹闹一块[进]",
        ],
        "exit": [
            "丝粘线乱趁早[离]",
            "月满则亏从容[退]",
            "云散风停慢慢[收]",
            "浪过礁石静静[离]",
            "花开茶凉缓缓[走]",
            "拍拍屁股快快[溜]",
        ],
        "watch": [
            "线缠丝绕且慢[等]",
            "风未起时静静[守]",
            "雾里看花慢慢[观]",
            "潮未到时且自[歇]",
            "乖乖坐好别乱[动]",
            "稳住别慌慢慢[等]",
        ],
    }
    if kind == "下穿前低":
        g = T["down"]
    elif kind == "上穿前高":
        g = T["up"]
    elif kind in ("做空开仓", "做多开仓", "反手开空"):
        g = T["watch"] if worth is False else T["enter"]
    elif kind in ("空单平仓", "全平多单", "半平多单"):
        g = T["exit"]
    else:
        g = T["watch"]
    return g[sum(ord(ch) for ch in kind + seed) % len(g)]


def ma_state(bars, i=-1):
    """均线排列的一句话(共振行用).与本地 sa_strategy.ma_state 同口径."""
    if not bars:
        return ""
    if i < 0:
        i = len(bars) + i
    if i < 0 or i >= len(bars):
        return ""
    b = bars[i]
    m9, m25, m69 = b.get("MA9"), b.get("MA25"), b.get("MA69")
    if None in (m9, m25, m69):
        return ""
    if m9 > m25 > m69:
        return "三线多头"
    if m9 < m25 < m69:
        return "三线空头"
    return "均线交织"


def resonance_line(d4, d60, d15):
    """三周期共振行(用户 2026-10-04 定的口径,与本地同算法).

    4 小时定方向 -> 60 分(就是 1 小时)看是否同向 -> 15 分查验.
    1 小时与 4 小时不同向时标出"⚠️ 1H共振方向有变".
    """
    s4, s60, s15 = ma_state(d4), ma_state(d60), ma_state(d15)
    segs = []
    if s4:
        segs.append(f"4小时 {s4}")
    if s60:
        segs.append(f"1小时 {s60}" +
                    (" ⚠️ 1H共振方向有变" if (s4 and s60 != s4) else ""))
    if s15:
        segs.append(f"15分钟 {s15}")
    if not segs:
        return ""
    return "共振   " + " | ".join(segs)


def sub_words(bars, i=-1):
    """副图三样合成一句:MACD零轴/交叉 | RSI+DI | ADX+DI.与本地同口径.

    抽出来是因为用户 2026-10-04 要求 4小时 和 1小时 各出一行,各自标周期,
    那就得对两个周期分别算一次,不能只算触发周期那一次.
    """
    if i < 0:
        i = len(bars) + i
    if i < 1 or i >= len(bars):
        return ""
    cur, prev = bars[i], bars[i - 1]
    dif, dea = cur["DIF"], cur["DEA"]
    if None in (dif, dea):
        return ""
    pos = ("快慢线皆在零轴上" if dif > 0 and dea > 0 else
           "快慢线皆在零轴下" if dif < 0 and dea < 0 else
           "快线在轴上慢线在轴下" if dif > 0 > dea else "快线在轴下慢线在轴上")
    if prev["DIF"] is not None:
        if prev["DIF"] <= prev["DEA"] and dif > dea:
            pos += "刚金叉"
        elif prev["DIF"] >= prev["DEA"] and dif < dea:
            pos += "刚死叉"
    dmi_s = ""
    if None not in (cur.get("ADX"), cur.get("PDI"), cur.get("MDI")):
        ap = bars[i - 1].get("ADX") if i >= 1 else None
        _t = adx_phrase(cur["ADX"], ap, cur["PDI"], cur["MDI"])
        if _t:
            dmi_s = "|" + _t
    _r = rsi_words(bars, i, pdi=cur.get("PDI"), mdi=cur.get("MDI"))
    return pos + (f"|{_r}" if _r else "") + dmi_s


VOL_STUB_RATIO = 0.10      # 兜底:量不足前几根中位数 10% -> 认作坏根
VOL_CUTOFF_HM = (14, 40)   # 用户 2026-10-04 定:14:40 之后那几根的量不算


def real_bar_index(bars, i=-1, lookback=5, ratio=VOL_STUB_RATIO):
    """最后一根[有真实成交量]的 K 线下标.

    两条判据,按优先级:
      ① 显式时间线(用户 2026-10-04 定):K 线时间 >= 14:40 的量不算 --
         每天收官那根是坏的(实测 4小时 15:00 那根 344,前一根 172,149).
      ② 兜底:量小得离谱的也跳过.
    """
    if i < 0:
        i = len(bars) + i
    if i < lookback:
        return i
    while i >= lookback:
        dt = bars[i].get("dt")
        if dt is None or (dt.hour, dt.minute) < VOL_CUTOFF_HM:
            break
        i -= 1
    if i < lookback:
        return i
    prior = sorted(b.get("volume") or 0 for b in bars[i - lookback:i])
    med = prior[len(prior) // 2]
    if med and (bars[i].get("volume") or 0) < med * ratio:
        return i - 1
    return i


def volume_word(bars, i=-1):
    """成交在增 / 成交在减 / 成交持平.用真实那根比前 5 根均值."""
    j = real_bar_index(bars, i)
    if j < 5:
        return ""
    v = bars[j].get("volume") or 0
    prior = [b.get("volume") or 0 for b in bars[j - 5:j]]
    avg = sum(prior) / len(prior) if prior else 0
    if not avg:
        return ""
    if v >= avg * 1.2:
        return "成交在增"
    if v <= avg * 0.8:
        return "成交在减"
    return "成交持平"


def fetch_cum_volume(code):
    """当日累计成交量(新浪实时快照第 15 个字段)."""
    txt = _get(f"https://hq.sinajs.cn/list=nf_{code}",
               referer="https://finance.sina.com.cn", encoding="gbk")
    m = re.search(r'"(.*)"', txt)
    if not m or not m.group(1).strip():
        return 0
    f = m.group(1).split(",")
    if len(f) < 18:
        return 0
    try:
        return int(float(f[14] or 0))
    except ValueError:
        return 0


def volume_push_word(state, code):
    """较上次成交在增 / 在减 / 持平.与本地同口径.

    口径是[这次推送跟上次推送]比:把当日累计成交量换成"每分钟成交"再比快慢.
    样本存在 signal_state.json 里(工作流会把它提交回仓库),键名 __vol__<code>.
    累计量回落 = 换日了,旧样本作废.
    """
    cum = fetch_cum_volume(code)
    if not cum:
        return ""
    now = now_cn()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    key = f"__vol__{code}"
    arr = [x for x in (state.get(key) or []) if isinstance(x, list) and len(x) == 2]
    if arr and cum < (arr[-1][1] or 0):
        arr = []

    def _rate(a, b):
        try:
            t0 = datetime.strptime(a[0], "%Y-%m-%d %H:%M:%S")
            t1 = datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None
        mins = (t1 - t0).total_seconds() / 60.0
        if mins <= 0:
            return None
        return (b[1] - a[1]) / mins

    word = ""
    if len(arr) >= 2:
        r_now = _rate(arr[-1], [stamp, cum])
        r_prev = _rate(arr[-2], arr[-1])
        if r_now is not None and r_prev and r_prev > 0:
            k = r_now / r_prev
            word = ("较上次成交在增" if k >= 1.2 else
                    "较上次成交在减" if k <= 0.8 else "较上次成交持平")
    arr.append([stamp, cum])
    state[key] = arr[-4:]
    return word


def oi_line(bars, i=-1):
    """4 小时的持仓量变化:持仓在增 / 持仓在减 / 持仓持平.

    每根 K 线自带持仓量(新浪的 "p" 字段),跟上一根同周期比即可.
    用户 2026-10-04:不要日增,要 4 小时是增是减.
    """
    if i < 0:
        i = len(bars) + i
    if i < 1 or i >= len(bars):
        return ""
    now_oi = bars[i].get("oi")
    prev_oi = bars[i - 1].get("oi")
    if not now_oi:
        return ""
    wan = now_oi / 10000.0
    if not prev_oi:
        return f"持仓 {wan:.1f}万"
    delta = now_oi - prev_oi
    word = ("持仓在增" if delta > 0 else
            "持仓在减" if delta < 0 else "持仓持平")
    return f"{word} {delta:+,.0f}({wan:.1f}万)"


def describe(bars, kind, i=-1, reso=None, sub_lines=None, vol_text=""):
    if i < 0:
        i = len(bars) + i      # 负数下标必须先转正,否则下面切片算出来是空列表
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
    # 量比:调用方给了"较上次成交在增/减"就用它 -- 那是[这次推送跟上次推送]比,
    # 不是 K 线跟 K 线比(用户 2026-10-04 定的口径).没给就退回按 K 线比.
    vs = vol_text or volume_word(bars, i) or "成交持平"
    # 副图那段搬到 sub_words() 了 -- 用户要求 4小时 / 1小时 各出一行,各自标周期,
    # 所以得能对任意周期单独算(用户 2026-10-04).
    dmi_s = ""
    # 破位时参照位换成"下一扇门"(刚破的门已经走过去了,不能拿它量前方空间)
    sup, down, res, up = space_refs(bars, i, kind)

    def grade(sp):
        return ("不值得动手" if sp < 20 else "宜轻仓试" if sp < 30
                else "可正常进" if sp < 60 else "可略加")

    def side_text(prefix, level, gap, side_word):
        if level is None:      # 门外再没有更低了 -> 用户定:叫历史最低位
            return ("下方历史最低位,无参照" if side_word == "空单"
                    else "上方历史最高位,无参照")
        return f"{prefix}{level:.0f}(差{gap:.0f}),{side_word}{grade(gap)}"

    if kind == "上穿前高" or "多" in kind:
        sp = side_text("上方次前高" if kind == "上穿前高" else "上方前高",
                       res, up, "多单")
    elif "空" in kind or "反手" in kind or kind == "下穿前低":
        sp = side_text("下方次前低" if kind == "下穿前低" else "下方前低",
                       sup, down, "空单")
    else:
        left = ("下方历史最低位,无参照" if sup is None else
                f"前低{sup:.0f}(差{down:.0f},空单{grade(down)})")
        right = ("上方历史最高位,无参照" if res is None else
                 f"前高{res:.0f}(差{up:.0f},多单{grade(up)})")
        sp = left + "|" + right
    avg6 = max(b["high"] for b in bars[i - 5:i + 1]) - min(b["low"] for b in bars[i - 5:i + 1])
    rng = ("动能睡着啦,盘面困住不动" if avg6 < 5 else
           "来回十来点,横盘震荡" if avg6 < FLAT_GAP else
           # 原来写"方向还没挑明",与第 1 行的三线排列自相矛盾 -- 改成只说振幅.
           # 2026-10-04 用户改:25 点以上就算激战(原来 35,设高了).
           "波动正常,还在区间里磨" if avg6 < 25 else "波动剧烈,多空正在激战")
    # 给了共振行就用共振(4小时/1小时/15分同不同向一眼看出),量能并到第 4 行;
    # 没给就回退成原来的单周期写法.与本地 brief_compact 完全一致.
    first = reso if reso else f"{ma}|{vs}"
    # 第 4 行:量能 | 4小时持仓量变化 | 振幅状态(与本地完全一致)
    _oi = oi_line(bars, i)
    tail_bits = [vs] + ([_oi] if _oi else []) + [rng]
    last = (" | ".join([x for x in tail_bits if x]) if reso else rng)
    # 副图段:给了 sub_lines 就一行一个周期(前面自带周期名);没给就自己算一行.
    if sub_lines:
        subs = [x for x in sub_lines if x]
    else:
        _sw = sub_words(bars, i)
        subs = [_sw] if _sw else []
    return "\n".join([first] + subs + [sp, last])


# ---------------- 推送 ----------------
def push(title, body):
    if not WEBHOOK:
        print(f"[未配置 WECOM_WEBHOOK] {title}\n{body}")
        return False
    payload = json.dumps({"msgtype": "markdown",
                          "markdown": {"content": f"**{title}**\n{body}"[:4000]}}).encode()
    # 同一条连发两次(与本地一致)
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
# ---- 新闻筛选口径(用户 2026-10-01 定,与本地一致)----
# 只认①供需端异动 ②政府政策异动;上下游联动与宏观一律不进.
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
    """把默认的负下标转成正下标.

    这里踩过坑:写成 bars[max(0, i-1)],i=-1 时会取到 bars[0],
    等于拿最后一根和最早一根比;而 ap = bars[i-1] if i >= 1 else None
    在 i=-1 时直接变成 None,判断永远走"走平"分支.
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
            s += ",MA9 刚上穿 MA25"
        elif p["MA9"] > p["MA25"] and m9 < m25:
            s += ",MA9 刚下穿 MA25"
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
            z += ",刚金叉"
        elif p["DIF"] >= p["DEA"] and dif < dea:
            z += ",刚死叉"
    return f"MACD {z}"


def _adx_words(bars, i=-1):
    """ADX+DMI 合成一句:涨势/跌势 + 在升温/在降温/走平(与本地同口径)."""
    i = _idx(bars, i)
    ap = bars[i - 1].get("ADX") if i >= 1 else None
    return adx_phrase(bars[i].get("ADX"), ap,
                      bars[i].get("PDI"), bars[i].get("MDI"))


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
    """只陈述供需与政策异动,不搬原文,不做上下游推演.空仓时只看能开仓的品种."""
    try:
        titles = _fetch_news()
    except Exception as e:  # noqa: BLE001
        print(f"  新闻抓取失败 {e}")
        return "  抓取失败"
    if not titles:
        return "  暂无可用报道"
    # 云端不读手机持仓;默认按"能开仓"口径,即两个监控品种里波动够的那个
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
            lines.append(f"  {zh}(可开仓)  无驱动型大事件及政件")
            continue
        j = " ".join(hit)
        tp = [lb for kw, lb in TOPICS if kw in j][:5]
        bull = sum(1 for k in BULLISH if k in j)
        bear = sum(1 for k in BEARISH if k in j)
        tone = ("供应收缩方向,短期偏多" if bull > bear else
                "供应宽松或需求走弱,短期偏空" if bear > bull else
                "供需两向都有说法,方向未定")
        if any(k in j for k in POLICY):
            tone += ";含政件,留意执行力度"
        lines.append(f"  {zh}(可开仓)  {len(hit)} 条,涉及"
                     f"{','.join(tp) if tp else '相关异动'} → {tone}")
    return "\n".join(lines) if lines else "  无驱动型大事件及政件"


# ---- 现货价(生意社基差页,取"现货价格"列)----
SPOT_UNIT = {"SA": "元/吨", "FG": "元/平方米"}
# 玻璃换算系数:1 吨 = 87 平方米。
# 不是教科书的 80(5mm x 2500kg/m3 = 12.5kg/m2 -> 80 m2/吨)——
# 实际交割的板有公差、密度也不同。2026-10-04 用我的钢铁网的沙河行情原文
# 反推:10.75 元/平方米 = 935 元/吨 -> 87.0;10.70 元/平方米 = 930 元/吨 -> 86.9。
SQM_PER_TON = 87.0
_SPOT = {}


def closed_only(bars, minutes):
    """只保留已经收线的 K 线.

    云端拿的是全量,最后一根常常是正在走的那根;本地一直只用已收线的.
    两边口径必须一致,否则同一时刻会给出不同的趋势描述.
    """
    if not bars:
        return bars
    cutoff = now_cn() - timedelta(minutes=minutes)
    return [b for b in bars if b["dt"] <= cutoff] or bars


def _shape(bars):
    """是震荡整理还是趋势推进:三线走平 + 反复缠绕 = 震荡(与本地同口径)."""
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


SHAH_LIST = "https://www.mysteel.com/oilchem/bolizq/"
_SHAH = {}


def shah_glass():
    """沙河浮法玻璃现货价(我的钢铁网每日行情).返回 (元/吨, 元/平方米).

    同花顺 App 里那个 935 用的就是沙河(玻璃的交割基准地),生意社给的是
    全国均价(12.15 元/平方米),折出来永远和 App 差一截。用户 2026-10-04
    拍板改用这个源。取不到返回 (None, None),调用方退回生意社。
    """
    if "v" in _SHAH:
        return _SHAH["v"]
    ton = sqm = None
    try:
        raw = _get(SHAH_LIST, referer="https://www.mysteel.com/", encoding="utf-8")
        url = None
        for a, title in re.findall(
                r'href="([^"]+/a/\d+/[0-9A-F]+\.html)"[^>]*>([^<]{4,90})', raw):
            if "沙河" in title and "浮法玻璃" in title:
                url = a if a.startswith("http") else "https:" + a
                break
        if url:
            page = _get(url, referer=SHAH_LIST, encoding="utf-8")
            txt = re.sub(r"<script.*?</script>", "", page, flags=re.S)
            txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", txt))
            m = re.search(r"合计\s*([0-9]+(?:\.[0-9]+)?)\s*元/吨", txt)
            if m:
                ton = float(m.group(1))
            m2 = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*元/平方米", txt)
            if m2:
                sqm = float(m2.group(1))
    except Exception as e:  # noqa: BLE001
        print("  沙河现货抓取失败:%s" % str(e)[:50])
    _SHAH["v"] = (ton, sqm)
    return ton, sqm


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
        print(f"  现货价抓取失败({zh}): {str(e)[:50]}")
    _SPOT[prod] = val
    return val


# ---- 末尾随机句(用户提供)----
CLOSING_TAGS = (
    "沉没成本不参与重大决策",
    "承兑附加条件视为拒承兑",
    "保持怀疑,独立性高于一切",
    "一切皆有可能,但是依然要怀疑一切",
    "利益之所在,风险之所在",
    "权力只对权力的来源负责",
    "外在言行,皆为内心映射",
    "人只会为自身利益权衡取舍",
    "没投资过你人生的人,就没讨好的资格,更没平等对待的义务",
    "出钱的有话语权,出力的有建议权,而我有决策权",
    "做时间的朋友,",
    "艹,地球online的金币也太难获取了,",
    "开整   , 开整",
    "快开饭了,宝贝",
    "又活过了一天.",
    "牛逼坏了现在,这也能行  !",
    "行吧, 静能生慧",
    "💰稳可周全💰",
    "大肥鱼🐟的生活也并非一帆风顺",
    "势如风,贯其巷;巷有尽,势有衰.",
    "初阳是饵,次阳封喉;无次阳,弃之,周而复始",
    "大道至简",
    "多空争锋,得失又岂尽如人意",
    "嗯,也不是不行",
    "顺势不争价,谋势不谋时.不争价者得其势,不谋时者成其谋.",
    "价是果,势是因,倒着看的人先出局",
    "不预测,只应对",
    "亏得清楚,比赚得糊涂值钱",
    "命运的齿轮开始转动,丝毫不在意你夹在中间",
    "不确定时,空仓也是一种持仓",
    "别慌,让市场先说话",
    "看得懂才下手,看不懂就看着",
    "慢一步进场,快一步离场",
    "计划外的仓位,迟早变成计划外的亏损",
    "风起于青萍,浪成于微澜",
    "兵无常势,水无常形",
    "弓满则折,月满则亏",
    "留一分余地,得十年从容",
    "静水流深,急湍无鱼",
    "举重若轻者,必曾不举",
    "刀在鞘里,才是刀",
    "简单的事重复做,重复的事认真做",
    "小到睡得着的仓位,才叫仓位",
    "浮盈不是钱,平掉的才是",
    "进锐者退速,稳步者行远",
    "弓不引满,箭不轻发",
    "一步一印,胜过百步生风",
    "动急之则急,动缓之缓随",
    "明天还有机会",
    "走走停停也是路",
    "一顿好饭治百病",
    "不在亏损的单子上加仓",
    "一次侥幸,会喂大一辈子的侥幸",
    "靠侥幸赚的,会靠侥幸亏回去",
    "记录比记忆可靠",
    "不复盘的人,亏十次是同一个错",
    "一笔交易只承担一个理由",
    "不与大浪争,只随大浪走",
    "别拿别人的标准要求自己",
    "你不是一只股票",
    "生活大于盘面",
    "止损被扫后它涨了,那是它的路,不是你的",

    "混沌非乱,涌现非自由.",
    "你在读盘,是别人在动;你决定出手,你,便成了盘的一部分.",
    "天象不因卜者而改,然你的预测,必将改变你的行为.",
    "测市与测己,本为一事.",
    "今日微事,皆非微事.",
    "万物多存于\"可识而不可算\"之间.",
    "混沌之秘:细处不可预测,整体却自有章度.",
    "观身侧七人,我亦存于他人\"七\"中之数.",
    "简单的规则,长出无穷的复杂.",
    "Simple rules, endless complexity.",

    "在场,就是你现在做的所有事的意义",
    "你不是被推,是挑推你的手",

    "没鞘的刀,难道是\"菜\"刀.--菜就多练",
    "山外山,楼外楼,天上这会儿有神仙.--看大戏",
    "潮凶势猛,我自不动如山",
    "大浪淘沙,你名叫狗头金吗?",
)
TAGS_KEY = "__tags__"
FW = "  "


def _w(text):
    return sum(2 if ord(c) > 0x2E80 else 1 for c in text)


DASH_FREE_OVER = 40      # 超过这个宽度就不加短横


def _wrap(tag):
    """短句加首尾短横,长句不加.

    短横占 6 格,长句本来就快顶满整行,再套短横就贴到两头,很难看.
    """
    if _w(tag) > DASH_FREE_OVER:
        return tag
    return "-  " + tag + "  -"


def _center(text, width):
    pad = max(0, width - _w(text))
    left = (pad // 2) // 2 * 2
    right = pad - left
    return FW * (left // 2) + text + FW * (right // 2) + (" " if right % 2 else "")


def closing_tag(state):
    """每次只发一句,顺序随机,19 句跑完一轮才重来."""
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
    tag = CLOSING_TAGS[idx]
    line = _wrap(tag)                      # 长句不加短横
    width = max(_w(_wrap(t)) for t in CLOSING_TAGS)
    return _center(line, width)



def compose_brief(state):
    n = now_cn()
    wd = "一二三四五六日"[n.weekday()]
    # 标题不带时间(用户 2026-10-04):收盘总结提前到 14:45 发,
    # 写 15:00 是错的,写 14:45 也没意义 —— 直接不写。
    out = [f"\U0001F4CA **{n:%m-%d} \u5468{wd}**", ""]
    for prod, code in PRODUCTS.items():
        try:
            d4 = prep(closed_only(fetch_bars(code, 240), 240))
            d60 = prep(closed_only(fetch_bars(code, 60), 60))
            if len(d4) < 30:
                continue
            out.append(f"**{product_zh(prod)} {code}**")
            # 今日趋势:标签独占一行,内容另起一行缩进六字
            out.append("  今日趋势")
            segs = [_ma_words(d4), _macd_words(d4),
                    rsi_words(d4, len(d4) - 1,
                              pdi=d4[-1].get("PDI"), mdi=d4[-1].get("MDI")),
                    _adx_words(d4)]
            p60 = _ma_words(d60) if len(d60) > 2 else ""
            if p60:
                segs.append(f"60分钟{p60}")
            # 用户 2026-10-04:4 小时那段和"60分..."那段分两行写 ——
            # 挤一行时末尾的"排列"会被折到下一行去。两行从同一列起,
            # 这样"三"字正下方就是下一行的"6"字。
            body = ";".join([x for x in segs if x])
            tr = body.split(";")
            if len(tr) > 1 and tr[-1].startswith("60分钟"):
                out.append("            " + ";".join(tr[:-1]))
                out.append("            " + tr[-1])
            else:
                out.append("            " + body)
            wk = d4[-10:]
            wo, wc = wk[0]["open"], wk[-1]["close"]
            chg = (wc - wo) / wo * 100 if wo else 0
            tone = "上行" if chg > 0.5 else ("下行" if chg < -0.5 else "横向")
            hi = max(b["high"] for b in wk)
            lo = min(b["low"] for b in wk)
            shape = _shape(d4)
            out.append(f"  本周趋势  本周{tone} {abs(chg):.1f}%,"
                       f"周内 {lo:.0f}~{hi:.0f},{shape}")
            sp_ton = None
            sp_txt = "-(未取到)"
            # 玻璃优先用沙河(交割基准地,与同花顺 App 同口径);
            # 取不到才退回生意社全国均价,那时才需要乘 87 折算。
            if prod == "FG":
                ton, sqm = shah_glass()
                if ton:
                    # A 方案(用户 2026-10-04):只报沙河的吨价,和期货同单位、能和 App 对上
                    sp_ton = ton
                    sp_txt = f"{ton:g} 元/吨(沙河)"
            if sp_ton is None:
                sp = spot_price(prod)
                unit = SPOT_UNIT.get(prod, "元/吨")
                if isinstance(sp, (int, float)):
                    sp_txt = f"{sp:g} {unit}"
                    # 纯碱本来就是 元/吨,与生意社页面口径一致。
                    if unit == "元/平方米":
                        sp_ton = sp * SQM_PER_TON
                        sp_txt += f"(折 {sp_ton:g} 元/吨)"
                    else:
                        sp_ton = float(sp)
            close_px = d4[-1]["close"]
            # 基差 = 现货 - 期货(升水为正)
            basis = f"  基差 {sp_ton - close_px:+.0f}" if sp_ton is not None else ""
            out.append(f"  现货收盘 {sp_txt}  期货收盘 {close_px:.0f}{basis}")
            out.append("")
        except Exception as e:  # noqa: BLE001
            out.append(f"  {prod} 取数失败:{str(e)[:50]}")
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
    # ---- 收盘盘点:交易日 15:00 推一条,休市一律不推 ----
    bstate = state.get(BRIEF_KEY) or {}
    if brief_due(bstate):
        print("  到收盘盘点时间,开始生成")
        body = compose_brief(state)
        push("收盘盘点", body)
        bstate["last"] = now_cn().strftime("%Y-%m-%d")
        state[BRIEF_KEY] = bstate
        changed = True
        print("  盘点状态已并入 signal_state.json")
    elif not is_trading_day():
        print(f"  今日休市({now_cn():%Y-%m-%d}),收盘盘点静默")
    changed = False
    if not is_trading_day():
        print(f"  今日休市({now_cn():%Y-%m-%d}),不扫描信号")
        if changed:
            json.dump(state, open(STATE_FILE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        return 0
    for prod, code in PRODUCTS.items():
        # 共振:一次把三个周期取齐(4小时 / 1小时 / 15分),两个周期推送共用.
        # 4小时和60分本来就要取,只多一个 15 分.
        p3 = {}
        for period in (240, 60, 15):
            try:
                p3[period] = prep(fetch_bars(code, period))
            except Exception as e:  # noqa: BLE001
                p3[period] = None
                print(f"  {code}/{period} 取数失败 {e}")
        try:
            reso = resonance_line(p3.get(240), p3.get(60), p3.get(15))
        except Exception as e:  # noqa: BLE001
            reso = ""
            print(f"  {code} 共振行失败 {e}")
        # 副图分两行,各自标周期(用户 2026-10-04 定):4小时在上,1小时在下.
        try:
            subs = []
            for lab, per in (("4小时", 240), ("1小时", 60)):
                if p3.get(per):
                    w = sub_words(p3[per], -1)
                    if w:
                        subs.append(f"{lab} {w}")
        except Exception as e:  # noqa: BLE001
            subs = []
            print(f"  {code} 副图行失败 {e}")
        # 量比:口径是"这次推送 vs 上次推送",样本记在 signal_state.json 里
        try:
            vol_text = volume_push_word(state, code)
        except Exception as e:  # noqa: BLE001
            vol_text = ""
            print(f"  {code} 量比失败 {e}")
        for period, label in ((240, "4小时"), (60, "60分钟")):
            bars = p3.get(period)
            if bars is None:
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
                     describe(bars, k, reso=reso, sub_lines=subs,
                              vol_text=vol_text) + "\n\n"
                     + tail_phrase(k, bar, worth=worth_acting(bars, k)))
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
