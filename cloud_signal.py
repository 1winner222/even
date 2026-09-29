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
import sys
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
    req = urllib.request.Request(WEBHOOK, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        ok = '"errcode":0' in r.read().decode("utf-8", "replace")
        print(f"  推送 {title} -> {'成功' if ok else '失败'}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"  推送失败: {e}")
        return False


def is_flat(bars):
    w = bars[-6:]
    return (max(b["high"] for b in w) - min(b["low"] for b in w)) < FLAT_GAP


def main():
    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        state = {}
    changed = False
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
                push(f"【{code} {label}】{k}",
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
