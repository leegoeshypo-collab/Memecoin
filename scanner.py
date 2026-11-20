import json, os, sys, time
import requests

CFG = {
    "min_age_min": 0,
    "max_age_hours": 72,
    "min_liquidity": 3_000,
    "min_mcap": 5_000,
    "max_mcap": 50_000_000,
    "min_vol_h1": 1_000,
    "min_vol_liq_ratio": 0.05,
    "min_liq_mcap_ratio": 0.01,
    "min_buys_h1": 5,
    "min_buy_ratio": 0.4,
    "min_change_h1": -100,
    "max_change_h1": 100000,
    "min_change_m5": -100,
    "max_top10_pct": 90,
    "min_score": 0,
    "poll_seconds": 120,
}

SEEN_FILE = "seen.json"
DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1"
HDR = {"User-Agent": "Mozilla/5.0 memescanner"}


def get(url, **kw):
    try:
        r = requests.get(url, headers=HDR, timeout=15, **kw)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[warn] {url[:70]}... {e}")
        return None


def discover():
    addrs = set()
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"):
        data = get(DEX + path)
        for t in data or []:
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
        return False, 0, {}
    age_min = (time.time() * 1000 - created) / 60000
    ratio = buys / (buys + sells) if (buys + sells) else 0
    vol_liq = vol1 / liq

    ok = (
        c["min_age_min"] <= age_min <= c["max_age_hours"] * 60
        and liq >= c["min_liquidity"]
        and c["min_mcap"] <= mcap <= c["max_mcap"]
        and vol1 >= c["min_vol_h1"]
        and vol_liq >= c["min_vol_liq_ratio"]
        and liq / mcap >= c["min_liq_mcap_ratio"]
        and buys >= c["min_buys_h1"]
        and ratio >= c["min_buy_ratio"]
        and c["min_change_h1"] <= ch1 <= c["max_change_h1"]
        and ch5 >= c["min_change_m5"]
    )

    score = (
        min(vol_liq / 3, 1) * 25
        + (min((ratio - 0.5) / 0.3, 1) * 20 if ratio > 0.5 else 0)
        + min(buys / 300, 1) * 20
        + min(max(ch1, 0) / 100, 1) * 15
        + min((liq / mcap) / 0.15, 1) * 20
    )
    stats = dict(age_min=age_min, liq=liq, mcap=mcap, vol1=vol1, buys=buys,
                 sells=sells, ratio=ratio, ch1=ch1, ch5=ch5, vol_liq=vol_liq)
    return ok, round(score), stats


def safety_check(mint):
    rep = get(f"{RUG}/tokens/{mint}/report")
    if not rep:
        return False, ["RugCheck unavailable"]
    notes, bad = [], False
    if rep.get("mintAuthority"):
        bad = True; notes.append("mint authority NOT revoked")
    if rep.get("freezeAuthority"):
        bad = True; notes.append("freeze authority NOT revoked")
    holders = rep.get("topHolders") or []
    top10 = sum(h.get("pct", 0) for h in holders[:10])
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
        print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15, json={
            "chat_id": chat, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True})
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
    print(f"[scan] {len(addrs)} new candidates")
    for mint, p in best_pairs(addrs).items():
        ok, score, s = market_check(p)
        if not ok or score < CFG["min_score"]:
            continue
        safe, notes = safety_check(mint)
        seen.add(mint)
        if not safe:
            print(f"[skip] {p['baseToken']['symbol']}: {notes}")
            continue
        name = p["baseToken"]["symbol"]
        send(
            f"🚨 <b>{name}</b>  |  Score {score}/100\n"
            f"MC {fmt(s['mcap'])} · Liq {fmt(s['liq'])} · Age {s['age_min']:.0f}m\n"
            f"1h: {s['ch1']:+.0f}%  5m: {s['ch5']:+.0f}%  Vol {fmt(s['vol1'])}\n"
            f"Buys/Sells: {s['buys']}/{s['sells']} ({s['ratio']*100:.0f}% buys)\n"
            f"🛡 {'; '.join(notes)}\n\n"
            f"<code>{mint}</code>\n"
            f"<a href=\"{p['url']}\">DexScreener</a> · "
            f"<a href=\"https://rugcheck.xyz/tokens/{mint}\">RugCheck</a>\n"
            f"⚠️ Not financial advice. DYOR."
        )
    json.dump(list(seen)[-5000:], open(SEEN_FILE, "w"))


if __name__ == "__main__":
    if "--once" in sys.argv:
        scan()
    else:
        while True:
            scan()
            time.sleep(CFG["poll_seconds"])
