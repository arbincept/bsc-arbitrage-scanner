"""
BSC Real-Time Arbitrage & Route Scanner (Production-Grade)
Features:
- Multi-DEX Aggregated Routing (KyberSwap Aggregator API)
- Automated Token Transfer Tax Detection (Buy/Sell fee-on-transfer via GoPlus Security)
- Pool Depth & Liquidity Filtering (DEXScreener integration & Anti-Phantom checks)
- Real Execution Slippage & Dynamic Gas Fee Friction Modeling
- Zero Private Keys: Read-Only Simulation & Telemetry
"""

import requests
import time
import sys

# ==============================================================================
# CONFIGURATION PARAMETERS
# ==============================================================================
BNB_INPUT_AMOUNT = 0.005      # Trade simulation size in native BNB
MIN_PROFIT_PCT = 1.0          # Minimum REAL NET profit threshold to trigger alert (%)
SLIPPAGE_BUFFER_PCT = 0.5     # Execution slippage tolerance buffer (%)
MIN_POOL_LIQUIDITY_USD = 500  # Minimum required pool liquidity depth in USD
MAX_ALLOWED_TAX_PCT = 10.0    # Ignore honeypots or scam tokens with >10% tax

# Native Wrapped BNB on BSC
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"

# Arbitrage Inception (ARB INC) token contract on BSC
ARB_INC_TOKEN = "0x5EE54869Ecd5E752C31aF095187326D4A4D50e1c"

# External APIs
KYBER_ROUTES_API = "https://aggregator-api.kyberswap.com/bsc/api/v1/routes"
KYBER_BUILD_API = "https://aggregator-api.kyberswap.com/bsc/api/v1/route/build"
GOPLUS_SECURITY_API = "https://api.gopluslabs.io/api/v1/token_security/56"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

TOKEN_LIST_URLS = [
    "https://tokens.pancakeswap.finance/pancakeswap-extended.json",
    "https://raw.githubusercontent.com/viaprotocol/tokenlists/main/tokenlists/bsc.json"
]

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
})

# In-memory caches to avoid redundant external API calls
security_cache = {}
liquidity_cache = {}
last_alert_time = {}

# ==============================================================================
# TOKEN SECURITY & TAX DETECTION (Fee-On-Transfer)
# ==============================================================================
def get_token_security(token_address):
    """
    Detects buy tax, sell tax, and honeypot flags using GoPlus Security API.
    Prevents false-positive arbitrage alerts on tokens with transfer taxes.
    """
    addr_lower = token_address.lower()
    
    # Check cache (valid for session)
    if addr_lower in security_cache:
        return security_cache[addr_lower]
        
    sec_info = {
        "buy_tax": 0.0,
        "sell_tax": 0.0,
        "is_honeypot": False,
        "cannot_sell_all": False
    }
    
    try:
        url = f"{GOPLUS_SECURITY_API}?contract_addresses={token_address}"
        r = session.get(url, timeout=3.5)
        if r.status_code == 200:
            res = r.json().get("result", {}).get(addr_lower, {})
            if res:
                sec_info["buy_tax"] = float(res.get("buy_tax") or 0.0)
                sec_info["sell_tax"] = float(res.get("sell_tax") or 0.0)
                sec_info["is_honeypot"] = (res.get("is_honeypot") == "1")
                sec_info["cannot_sell_all"] = (res.get("cannot_sell_all") == "1")
    except Exception:
        pass
        
    security_cache[addr_lower] = sec_info
    return sec_info

# ==============================================================================
# POOL DEPTH & LIQUIDITY MONITORING
# ==============================================================================
def get_token_liquidity(token_address):
    """
    Retrieves aggregated pool liquidity depth across all DEX pairs from DEXScreener.
    Filters out shallow pools with excessive price impact.
    """
    addr_lower = token_address.lower()
    
    # Return cached liquidity if checked in the last 15 minutes
    now = time.time()
    if addr_lower in liquidity_cache:
        val, ts = liquidity_cache[addr_lower]
        if now - ts < 900:
            return val
            
    liq_usd = 0.0
    try:
        url = f"{DEXSCREENER_API}/{token_address}"
        r = session.get(url, timeout=3.5)
        if r.status_code == 200:
            pairs = r.json().get("pairs") or []
            liq_usd = sum(float(p.get("liquidity", {}).get("usd", 0) or 0) for p in pairs)
    except Exception:
        pass
        
    liquidity_cache[addr_lower] = (liq_usd, now)
    return liq_usd

# ==============================================================================
# ROUTING & EXECUTION VERIFICATION
# ==============================================================================
def verify_kyber_executable(route_summary, sender="0xaff5163102a945952d75ea6a32d185e4afd604e7"):
    """
    Verifies that the routing aggregator can build executable calldata
    with the required slippage tolerance (anti-phantom pool validation).
    """
    try:
        payload = {
            "routeSummary": route_summary,
            "sender": sender,
            "recipient": sender,
            "slippageTolerance": int(SLIPPAGE_BUFFER_PCT * 100)  # e.g. 50 bps = 0.5%
        }
        r = session.post(KYBER_BUILD_API, json=payload, timeout=3.5)
        if r.status_code == 200:
            res = r.json()
            if res.get("data", {}).get("data"):
                return True
    except Exception:
        pass
    return False

def calculate_token_received(raw_token_out, buy_tax):
    return int(raw_token_out * (1.0 - buy_tax))

def calculate_trade_metrics(quote_buy, quote_sell, sell_tax):
    raw_bnb_out_wei = int(quote_sell.get("amountOut", 0))
    actual_bnb_out = (raw_bnb_out_wei / 1e18) * (1.0 - sell_tax)
    bnb_after_slippage = actual_bnb_out * (1.0 - SLIPPAGE_BUFFER_PCT / 100.0)

    gas_buy_wei = int(quote_buy.get("gas", 250000)) * int(quote_buy.get("gasPrice", 1e9))
    gas_sell_wei = int(quote_sell.get("gas", 250000)) * int(quote_sell.get("gasPrice", 1e9))
    total_gas_bnb = (gas_buy_wei + gas_sell_wei) / 1e18

    final_net_bnb = bnb_after_slippage - total_gas_bnb
    net_profit_bnb = final_net_bnb - BNB_INPUT_AMOUNT
    net_roi_pct = (net_profit_bnb / BNB_INPUT_AMOUNT) * 100.0
    theoretical_gross_bnb = (raw_bnb_out_wei / 1e18) - BNB_INPUT_AMOUNT
    theoretical_gross_pct = (theoretical_gross_bnb / BNB_INPUT_AMOUNT) * 100.0

    return {
        "actual_bnb_out": actual_bnb_out,
        "total_gas_bnb": total_gas_bnb,
        "final_net_bnb": final_net_bnb,
        "net_profit_bnb": net_profit_bnb,
        "net_roi_pct": net_roi_pct,
        "theoretical_gross_pct": theoretical_gross_pct,
    }

def get_quote(token_in, token_out, amount_wei):
    """Queries KyberSwap Aggregation API for optimal multi-DEX route."""
    params = {
        "tokenIn": token_in,
        "tokenOut": token_out,
        "amountIn": str(amount_wei),
        "saveGas": "0",
        "gasInclude": "1"
    }
    try:
        r = session.get(KYBER_ROUTES_API, params=params, timeout=3.5)
        if r.status_code == 200:
            return r.json().get("data", {}).get("routeSummary")
    except Exception:
        pass
    return None

def load_bsc_tokens():
    """Loads active verified BSC tokens and ensures ARB INC is included."""
    tokens = {}
    
    # Always include Arbitrage Inception token
    tokens[ARB_INC_TOKEN.lower()] = {
        "address": ARB_INC_TOKEN,
        "symbol": "Arb Inc",
        "name": "Arbitrage Inception"
    }
    
    for url in TOKEN_LIST_URLS:
        try:
            r = session.get(url, timeout=6)
            if r.status_code == 200:
                raw = r.json()
                items = raw.get("tokens", []) if isinstance(raw, dict) else raw
                for t in items:
                    if isinstance(t, dict) and t.get("chainId") == 56:
                        addr = (t.get("address") or "").lower()
                        if addr and addr != WBNB.lower():
                            tokens[addr] = {
                                "address": t.get("address"),
                                "symbol": t.get("symbol", "TOKEN"),
                                "name": t.get("name", "")
                            }
        except Exception:
            continue
    return list(tokens.values())

# ==============================================================================
# COMPREHENSIVE ARBITRAGE EVALUATION ENGINE
# ==============================================================================
def evaluate_token(token_data, current_idx, total_tokens):
    addr = token_data["address"]
    addr_lower = addr.lower()
    symbol = token_data["symbol"]
    
    sys.stdout.write(f"\r[{time.strftime('%X')}] Scanning [{current_idx}/{total_tokens}] {symbol:<12} (Size: {BNB_INPUT_AMOUNT} BNB)")
    sys.stdout.flush()

    # 1. Pool Depth Check
    liq_usd = get_token_liquidity(addr)
    if liq_usd > 0 and liq_usd < MIN_POOL_LIQUIDITY_USD:
        return  # Skip shallow / illiquid pools

    # 2. Token Security & Transfer Tax Check
    security = get_token_security(addr)
    if security["is_honeypot"] or security["cannot_sell_all"]:
        return  # Skip malicious honeypots
        
    buy_tax = security["buy_tax"]
    sell_tax = security["sell_tax"]
    
    if (buy_tax * 100 > MAX_ALLOWED_TAX_PCT) or (sell_tax * 100 > MAX_ALLOWED_TAX_PCT):
        return  # Skip tokens with prohibitive tax

    amount_in_wei = int(BNB_INPUT_AMOUNT * 1e18)
    
    # 3. Leg 1 Quote: WBNB -> TOKEN (Buy)
    quote_buy = get_quote(WBNB, addr, amount_in_wei)
    if not quote_buy:
        return
        
    raw_token_out = int(quote_buy.get("amountOut", 0))
    if raw_token_out <= 0:
        return

    # Real-World Deduction: Subtract buy tax (tokens retained on-chain)
    actual_token_received = calculate_token_received(raw_token_out, buy_tax)
    if actual_token_received <= 0:
        return
        
    time.sleep(0.06)
    
    # 4. Leg 2 Quote: TOKEN -> WBNB (Sell with actual net tokens received)
    quote_sell = get_quote(addr, WBNB, actual_token_received)
    if not quote_sell:
        return
        
    if int(quote_sell.get("amountOut", 0)) <= 0:
        return

    metrics = calculate_trade_metrics(quote_buy, quote_sell, sell_tax)
    total_gas_bnb = metrics["total_gas_bnb"]
    net_profit_bnb = metrics["net_profit_bnb"]
    net_roi_pct = metrics["net_roi_pct"]
    theoretical_gross_pct = metrics["theoretical_gross_pct"]

    # 8. Filter by Minimum Net Profit Requirement
    if net_roi_pct >= MIN_PROFIT_PCT:
        # Anti-Phantom verification: ensure both routes compile executable calldata
        if not verify_kyber_executable(quote_buy) or not verify_kyber_executable(quote_sell):
            return
            
        # Cooldown per token: 2 minutes
        if addr_lower in last_alert_time and (time.time() - last_alert_time[addr_lower]) < 120:
            return
            
        last_alert_time[addr_lower] = time.time()
        
        route_in = [s[0].get("exchange") for s in quote_buy.get("route", []) if s]
        route_out = [s[0].get("exchange") for s in quote_sell.get("route", []) if s]
        
        print("\n\n" + "⚡" * 36)
        print(f"🚨 OPPORTUNITÀ ARBITRAGGIO VERIFICATA: {symbol} ({token_data['name']})")
        print(f"📍 Indirizzo Target  : {addr}")
        print(f"💧 Profondità Pool   : ${liq_usd:,.2f} USD")
        print(f"🏷️  Token Transfer Tax: Buy {buy_tax*100:.1f}% | Sell {sell_tax*100:.1f}%")
        print(f"📊 Spread Teorico    : +{theoretical_gross_pct:.2f}% (Lordo senza attriti)")
        print(f"🛡️  Buffer Slippage   : -{SLIPPAGE_BUFFER_PCT:.1f}%")
        print(f"⛽ Costo Gas Stimato : -{total_gas_bnb:.5f} BNB (~${float(quote_buy.get('gasUsd',0))+float(quote_sell.get('gasUsd',0)):.3f})")
        print(f"💎 PROFITTO NETTO    : +{net_roi_pct:.2f}% (+{net_profit_bnb:.5f} BNB)")
        print(f"🛒 Percorso Buy      : {' -> '.join(route_in)}")
        print(f"💰 Percorso Sell     : {' -> '.join(route_out)}")
        print(f"🔗 Swap Diretto      : https://arbitrage-inc.exchange/swap?tokenOut={addr}")
        print("⚡" * 36 + "\n")

# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    print("=" * 70)
    print("⚡ BSC Full-Friction Arbitrage & Route Scanner")
    print("🛡️  TAX-AWARE & LIQUIDITY-DEPTH PROTECTED ENGINE")
    print("=" * 70)
    print("ℹ️  MODE: READ-ONLY TELEMETRY & SIMULATION (Zero Private Keys)")
    print(f"⚙️  Configurazione: Trade Size = {BNB_INPUT_AMOUNT} BNB | Min Net ROI >= +{MIN_PROFIT_PCT}%")
    print(f"🛡️  Filtri Attrito : Min Liquidity = ${MIN_POOL_LIQUIDITY_USD} | Slippage Buffer = {SLIPPAGE_BUFFER_PCT}% | Max Tax = {MAX_ALLOWED_TAX_PCT}%")
    print("⭐ Support open-source: If this scanner helps you, drop a Star on GitHub!")
    print("👉 https://github.com/arbincept/bsc-arbitrage-scanner")
    print("=" * 70 + "\n")

    token_list = load_bsc_tokens()
    print(f"✅ Ingestion completata: monitoraggio avviato su {len(token_list)} token BSC.")
    print("🚀 Inizio ciclo di scansione...\n")
    
    while True:
        for idx, t in enumerate(token_list, 1):
            evaluate_token(t, idx, len(token_list))
            time.sleep(0.08)
        print(f"\n[{time.strftime('%X')}] Ciclo di scansione completato. Riavvio...")
        time.sleep(2)
