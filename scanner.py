import json, os, sys, time
from collections import Counter
import requests

CFG = {
    "min_age_min": 10,
    "max_age_hours": 48,
    "min_liquidity": 10_000,
    "min_mcap": 30_000,
    "max_mcap": 10_000_000,
    "min_vol_h1": 10_000,
    "min_vol_liq_ratio": 0.3,
    "min_liq_mcap_ratio": 0.03,
    "min_buys_h1": 30,
    "min_buy_ratio": 0.52,
    "min_change_h1": 0,
    "max_change_h1": 500,
    "min_change_m5": -8,
    "max_top10_pct": 50,
    "min_score": 50,
    "poll_seconds": 120,
}
SEEN_FILE = "seen.json"
DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1"
HDR = {"User-Agent": "Mozilla/5.0 memescanner"}


def get(url):
    try:
        r = requests.get(url, headers=HDR, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[warn] {url[:60]}... {e}")
        return None


def discover():
    addrs = set()
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"):
        for t in get(DEX + path) or []:
            if t.get("chainId") == "solana" and t.get("tokenAddress"):
                addrs.add(t["tokenAddress"])
    return list(addrs)


def best_pairs(addrs):
    out = {}
    for i in range(0, len(addrs), 30):
        data = get(f"{DEX}/latest/dex/tokens/{','.join(addrs[i:i+30])}")
        for p in (data or {}).get("pairs") or []:
            if p.get("chainId") != "solana":
                continue
            mint = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if mint not in out or liq > ((out[mint].get("liquidity") or {}).get("usd") or 0):
                out[mint] = p
    return out


def market_check(p):
    """Returns (failed_reason or None, score, stats)."""
    c = CFG
    liq = (p.get("liquidity") or {}).get("usd") or 0
    mcap = p.get("marketCap") or p.get("fdv") or 0
    vol1 = (p.get("volume") or {}).get("h1") or 0
    tx = (p.get("txns") or {}).get("h1") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    ch = p.get("priceChange") or {}
    ch1, ch5 = ch.get("h1") or 0, ch.get("m5") or 0
    created = p.get("pairCreatedAt")
    if not created or not mcap or not liq:
        return "no data", 0, {}
    age = (time.time() * 1000 - created) / 60000
    ratio = buys / (buys + sells) if (buys + sells) else 0
    vol_liq = vol1 / liq

    checks = [
        ("too new", age >= c["min_age_min"]),
        ("too old", age <= c["max_age_hours"] * 60),
        ("low liquidity", liq >= c["min_liquidity"]),
        ("mcap too low", mcap >= c["min_mcap"]),
        ("mcap too high", mcap <= c["max_mcap"]),
        ("low volume", vol1 >= c["min_vol_h1"]),
        ("low vol/liq", vol_liq >= c["min_vol_liq_ratio"]),
        ("thin liq vs mcap", liq / mcap >= c["min_liq_mcap_ratio"]),
        ("few buys", buys >= c["min_buys_h1"]),
        ("low buy ratio", ratio >= c["min_buy_ratio"]),
        ("1h change low", ch1 >= c["min_change_h1"]),
        ("1h change high", ch1 <= c["max_change_h1"]),
        ("dumping 5m", ch5 >= c["min_change_m5"]),
    ]
    stats = dict(age=age, liq=liq, mcap=mcap, vol1=vol1, buys=buys,
                 sells=sells, ratio=ratio, ch1=ch1, ch5=ch5)
    for name, passed in checks:
        if not passed:
            return name, 0, stats

    score = (
        min(vol_liq / 3, 1) * 25
        + (min((ratio - 0.5) / 0.3, 1) * 20 if ratio > 0.5 else 0)
        + min(buys / 300, 1) * 20
        + min(max(ch1, 0) / 100, 1) * 15
        + min((liq / mcap) / 0.15, 1) * 20
    )
    return None, round(score), stats


def safety_check(mint):
    rep = get(f"{RUG}/tokens/{mint}/report")
    if not rep:
        return None, ["RugCheck unavailable"]
    notes, bad = [], False
    if rep.get("mintAuthority"):
        bad = True; notes.append("mint authority NOT revoked")
    if rep.get("freezeAuthority"):
        bad = True; notes.append("freeze authority NOT revoked")
    top10 = sum(h.get("pct", 0) for h in (rep.get("topHolders") or [])[:10])
    if top10 > CFG["max_top10_pct"]:
        bad = True; notes.append(f"top10 holders {top10:.0f}%")
    for r in rep.get("risks") or []:
        if str(r.get("level", "")).lower() == "danger":
            bad = True; notes.append(r.get("name", "danger risk"))
    if not bad:
        notes.append(f"top10 {top10:.0f}%, authorities revoked")
    return (not bad), notes


def send(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[telegram] missing token or chat id\n", text)
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15, json={
            "chat_id": chat, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True})
        print("[telegram]", r.status_code, r.text[:120])
    except Exception as e:
        print("[warn] telegram:", e)


def fmt(n):
    return f"${n/1e6:.2f}M" if n >= 1e6 else f"${n/1e3:.1f}K"


def load_seen():
    try:
        return set(json.load(open(SEEN_FILE)))
    except Exception:
        return set()


def scan():
    seen = load_seen()
    addrs = [a for a in discover() if a not in seen]
    pairs = best_pairs(addrs)
    print(f"[scan] {len(addrs)} new candidates, {len(pairs)} Solana pairs")
    rejects, alerts = Counter(), 0

    for mint, p in pairs.items():
        sym = p["baseToken"]["symbol"]
        reason, score, s = market_check(p)
        if reason:
            rejects[reason] += 1
            continue
        if score < CFG["min_score"]:
            rejects["low score"] += 1
            continue
        safe, notes = safety_check(mint)
        if safe is None:
            rejects["rugcheck down"] += 1
            continue
        seen.add(mint)
        if not safe:
            rejects["unsafe"] += 1
            print(f"[skip] {sym}: {'; '.join(notes)}")
            continue
        alerts += 1
        send(
            f"🚨 <b>{sym}</b>  |  Score {score}/100\n"
            f"MC {fmt(s['mcap'])} · Liq {fmt(s['liq'])} · Age {s['age']:.0f}m\n"
            f"1h: {s['ch1']:+.0f}%  5m: {s['ch5']:+.0f}%  Vol {fmt(s['vol1'])}\n"
            f"Buys/Sells: {s['buys']}/{s['sells']} ({s['ratio']*100:.0f}% buys)\n"
            f"🛡 {'; '.join(notes)}\n\n"
            f"<code>{mint}</code>\n"
            f"<a href=\"{p['url']}\">DexScreener</a> · "
            f"<a href=\"https://rugcheck.xyz/tokens/{mint}\">RugCheck</a>\n"
            f"⚠️ Not financial advice. DYOR."
        )

    print(f"[done] alerts sent: {alerts}")
    print("[rejects]", dict(rejects.most_common()))
    json.dump(list(seen)[-5000:], open(SEEN_FILE, "w"))


if __name__ == "__main__":
    if "--ping" in sys.argv:
        send("✅ Scanner is running")
    if "--once" in sys.argv:
        scan()
    else:
        while True:
            scan()
            time.sleep(CFG["poll_seconds"])
