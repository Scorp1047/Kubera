"""
market.py — Kubera market data layer

What this file does (TastyTrade methodology only):
  - Fetch IVR from TastyTrade (primary entry filter)
  - Classify IVR: HIGH / MEDIUM / LOW
  - Detect directional bias from 3 signals (price action, VIX direction, vol skew)
  - Fetch VIX + direction
  - Compute expected move: Stock × (IV/100) × √(DTE/365)
  - Find the strike nearest to 16 delta in the live chain
  - Compute POP (Black-Scholes)
  - Fetch earnings dates
  - Return a clean data dict for signals.py to consume

What this file does NOT do:
  - RSI, MACD, ADX, EMA, Bollinger Bands, Keltner Channel, Ichimoku
  - OI wall analysis, PCR, unusual flow detection
  - Linear regression regime detection
  - Volume-price divergence (Alpha 3/4)
  - Realized vol / implied vol ratio (Alpha 1/2)
  - Support/resistance levels
  - Grok/Claude calls (no AI in Kubera)
"""

import os
import math
import json
import threading
import asyncio
import traceback as _tb
from datetime import datetime, date, timedelta
from scipy.stats import norm

import config as _cfg
from config import (
    log, ET,
    DTE_MIN, DTE_MAX, DTE_SWEET_SPOT,
    TARGET_DELTA, MIN_POP,
    IVR_HIGH, IVR_MEDIUM_FLOOR, IVR_LOW_CEILING,
    BIAS_MOVE_THRESHOLD, BIAS_VIX_LOOKBACK, BIAS_VIX_MOVE_PCT,
    BIAS_SKEW_RATIO, BIAS_SKEW_NEUTRAL_LOW,
    MIN_OPTION_OI, MAX_SPREAD_PCT,
    ETF_SYMBOLS, STOCK_SYMBOLS, SECTOR_MAP,
)

EARNINGS_CACHE = '/home/trader/kubera/data/earnings_cache.json'
IV_CACHE_PATH  = '/home/trader/kubera/data/iv_cache.json'


# ── Async bridge (set by bot.py post_init) ─────────────────────────
# Allows sync code running in thread-pool workers to call TT async functions.

_TASTY_LOOP = None

def set_tasty_loop(loop):
    global _TASTY_LOOP
    _TASTY_LOOP = loop

def _run_tt(coro, timeout=20):
    """Run a TastyTrade coroutine from a sync/thread-pool context."""
    if _TASTY_LOOP is None:
        raise RuntimeError('TastyTrade event loop not set — bot not fully initialised')
    future = asyncio.run_coroutine_threadsafe(coro, _TASTY_LOOP)
    return future.result(timeout=timeout)


# ── Black-Scholes ──────────────────────────────────────────────────

def bs_delta(S, K, T, r, sigma, option_type):
    """Black-Scholes delta. Used to find the 16-delta short strike."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return round(norm.cdf(d1) if option_type == 'call' else norm.cdf(d1) - 1, 4)

def bs_prob_otm(S, K, T, r, sigma, option_type):
    """Black-Scholes probability the option expires OTM (= POP for short option).
    TastyTrade: 16 delta ≈ 84% POP (1 SD OTM). 30 delta ≈ 70% POP."""
    if T <= 0 or sigma <= 0:
        return 50.0
    d2 = (math.log(S / K) + (r - 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return round(norm.cdf(-d2) * 100 if option_type == 'call' else norm.cdf(d2) * 100, 1)


# ── Expected Move ──────────────────────────────────────────────────

def expected_move(price, iv_pct, dte):
    """TastyTrade formula: EM = Stock × (IV/100) × √(DTE/365).
    Source: tastylive.com/definitions/calculating-expected-move
    Represents 1 standard deviation (68% probability).
    Short strikes are placed at or beyond this distance from spot.
    Returns (em_dollar, em_pct)."""
    if iv_pct <= 0 or dte <= 0:
        return 0.0, 0.0
    em_pct    = round(iv_pct / 100 * math.sqrt(dte / 365) * 100, 1)
    em_dollar = round(price * em_pct / 100, 2)
    return em_dollar, em_pct


# ── IV Cache ───────────────────────────────────────────────────────

_iv_cache_lock = threading.Lock()

def _load_iv_cache():
    with _iv_cache_lock:
        try:
            return json.load(open(IV_CACHE_PATH))
        except Exception:
            return {}

def _save_iv_cache(cache):
    with _iv_cache_lock:
        try:
            json.dump(cache, open(IV_CACHE_PATH, 'w'), indent=2)
        except Exception:
            pass


# ── IVR ────────────────────────────────────────────────────────────

def calculate_ivr(symbol, tt_metrics_cache=None):
    """IVR from TastyTrade's implied_volatility_index_rank.
    Matches exactly what the TT platform displays.
    Falls back to rolling 52-week file cache if TT unavailable."""
    try:
        import tasty as tasty_mod

        m = None
        if tt_metrics_cache and symbol in tt_metrics_cache:
            m = tt_metrics_cache[symbol]
        else:
            metrics = _run_tt(tasty_mod.tt_get_metrics_batch([symbol]))
            m = metrics.get(symbol)

        if m:
            ivr = m.implied_volatility_index_rank
            if ivr is not None:
                val = round(float(ivr) * 100, 1)
                log.info(f'IVR {symbol}: {val}% (TT tos_ivr)')
                # Maintain rolling IV cache with TT's 30d IV
                try:
                    today = date.today().isoformat()
                    if m.implied_volatility_30_day:
                        current_iv = float(m.implied_volatility_30_day)
                    elif m.implied_volatility_index:
                        raw = float(m.implied_volatility_index)
                        current_iv = raw if raw > 2 else raw * 100
                    else:
                        current_iv = None
                    if current_iv:
                        cache = _load_iv_cache()
                        sym_data = cache.get(symbol, {})
                        sym_data[today] = round(current_iv, 1)
                        cache[symbol] = sym_data
                        _save_iv_cache(cache)
                except Exception:
                    pass
                return val
    except Exception as e:
        log.warning(f'TT IVR error {symbol}: {str(e)[:50]}')

    # Fallback: rolling 52-week file cache
    try:
        cache = _load_iv_cache()
        sym_data = cache.get(symbol, {})
        if len(sym_data) >= 30:
            ivs = list(sym_data.values())
            curr = ivs[-1]
            lo, hi = min(ivs), max(ivs)
            if hi == lo:
                return 50.0
            return round(((curr - lo) / (hi - lo)) * 100, 1)
    except Exception:
        pass
    return None


def classify_ivr(ivr):
    """Classify IVR into regime.
    Returns 'HIGH' | 'MEDIUM' | 'LOW' | None (if IVR unavailable).
    HIGH (≥50): sell premium aggressively — IC, Jade Lizard, credit spread all valid.
    MEDIUM (25–49): directional credit spreads only; no IC.
    LOW (<25): buy premium — debit spreads or calendar."""
    if ivr is None:
        return None
    if ivr >= IVR_HIGH:
        return 'HIGH'
    if ivr >= IVR_MEDIUM_FLOOR:
        return 'MEDIUM'
    return 'LOW'


# ── VIX ────────────────────────────────────────────────────────────

async def get_vix_data():
    """Fetch VIX (30-day), VIX9D (9-day), VIX3M (93-day) via TastyTrade DXLink.
    Returns dict with vix, vix_dir ('rising'|'falling'|'flat'), vix9d, vix3m.
    vix_dir is used as directional bias signal 2.
    Raises RuntimeError on failure — never trades on missing VIX data."""
    try:
        import tasty as tasty_mod
        from tastytrade.streamer import DXLinkStreamer
        from tastytrade.dxfeed import Trade, Summary

        syms = ['VIX', 'VIX9D', 'VIX3M']
        trades    = {}
        prevclose = {}

        async with DXLinkStreamer(tasty_mod.TT_SESSION) as streamer:
            await streamer.subscribe(Trade,   syms)
            await streamer.subscribe(Summary, syms)
            await asyncio.sleep(8)

            while True:
                t = streamer.get_event_nowait(Trade)
                if t is None:
                    break
                val = float(t.price or 0)
                if val > 0:
                    trades[t.event_symbol] = round(val, 2)

            while True:
                s = streamer.get_event_nowait(Summary)
                if s is None:
                    break
                prev = getattr(s, 'prev_day_close_price', None)
                val  = float(prev or 0)
                if val > 0:
                    prevclose[s.event_symbol] = round(val, 2)

        if 'VIX' not in trades:
            raise RuntimeError('VIX data unavailable — TT DXLink returned no Trade event for VIX')

        vix   = trades['VIX']
        vix9d = trades.get('VIX9D', vix)
        vix3m = trades.get('VIX3M', vix)

        # VIX direction: live vs yesterday's close (bias signal 2)
        vix_prev = prevclose.get('VIX')
        vix_dir  = 'unknown'
        if vix_prev and vix_prev > 0:
            change_pct = (vix - vix_prev) / vix_prev
            if change_pct >= BIAS_VIX_MOVE_PCT:
                vix_dir = 'rising'   # fear building → bearish signal
            elif change_pct <= -BIAS_VIX_MOVE_PCT:
                vix_dir = 'falling'  # fear receding → bullish signal
            else:
                vix_dir = 'flat'

        log.info(f'VIX: {vix} (prev={vix_prev}, dir={vix_dir}) VIX9D={vix9d} VIX3M={vix3m}')
        return {
            'vix':      vix,
            'vix9d':    vix9d,
            'vix3m':    vix3m,
            'vix_dir':  vix_dir,
            'vix_prev': vix_prev,
        }

    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f'VIX fetch failed: {str(e)[:120]}') from e


async def get_vix():
    """Spot VIX only — delegates to get_vix_data()."""
    ctx = await get_vix_data()
    return ctx['vix']


# ── Directional Bias Detection ─────────────────────────────────────

def get_bias(closes, vix_dir, chain_calls=None, chain_puts=None, iv_frac=None):
    """Determine directional bias from 3 TastyTrade signals.
    Returns ('BULL' | 'BEAR' | 'NEUTRAL', reason_string).

    Signal 1 — Recent price action (5-day return):
      Source: TastyTrade "selling puts in beat-up stocks" study.
      Stock down 5%+ in a week → mean reversion setup → BULLISH (sell puts).
      Stock up 5%+ in a week → overbought, fade → BEARISH.

    Signal 2 — VIX direction (passed from bot.py scan start):
      VIX falling = fear receding = bullish pressure.
      VIX rising  = fear building = bearish pressure.

    Signal 3 — Volatility skew (put IV vs call IV):
      TastyTrade: puts always trade richer than equidistant calls (skew).
      When put_iv / call_iv significantly > 1 → market paying up for downside hedge → BULLISH to sell.
      When call_iv unusually rich → unusual demand for upside → BEARISH.

    Two of three signals agree → that bias is confirmed.
    All three mixed or absent → NEUTRAL."""

    votes = []
    reasons = []

    # Signal 1: 5-day price move
    if len(closes) >= 6:
        move_5d = (closes[-1] - closes[-6]) / closes[-6]
        if move_5d <= -BIAS_MOVE_THRESHOLD:
            votes.append('BULL')
            reasons.append(f'5d move {move_5d*100:.1f}% (beat-up → mean revert)')
        elif move_5d >= BIAS_MOVE_THRESHOLD:
            votes.append('BEAR')
            reasons.append(f'5d move +{move_5d*100:.1f}% (extended → fade)')
        else:
            votes.append('NEUTRAL')
            reasons.append(f'5d move {move_5d*100:.1f}% (flat)')
    else:
        votes.append('NEUTRAL')
        reasons.append('5d move: insufficient history')

    # Signal 2: VIX direction
    if vix_dir == 'falling':
        votes.append('BULL')
        reasons.append('VIX falling (fear receding)')
    elif vix_dir == 'rising':
        votes.append('BEAR')
        reasons.append('VIX rising (fear building)')
    else:
        votes.append('NEUTRAL')
        reasons.append(f'VIX {vix_dir}')

    # Signal 3: Vol skew (put IV vs call IV)
    if chain_calls and chain_puts and iv_frac:
        try:
            # ATM-ish options: within 5% of spot
            # Use mid_iv from greeks if available
            call_ivs = [c['greeks']['mid_iv'] for c in chain_calls
                        if c.get('greeks', {}).get('mid_iv') and c['greeks']['mid_iv'] > 0]
            put_ivs  = [p['greeks']['mid_iv'] for p in chain_puts
                        if p.get('greeks', {}).get('mid_iv') and p['greeks']['mid_iv'] > 0]
            if call_ivs and put_ivs:
                avg_call_iv = sum(call_ivs[:5]) / min(5, len(call_ivs))   # first 5 OTM calls
                avg_put_iv  = sum(put_ivs[:5])  / min(5, len(put_ivs))    # first 5 OTM puts
                skew_ratio  = avg_put_iv / avg_call_iv if avg_call_iv > 0 else 1.0
                if skew_ratio >= BIAS_SKEW_RATIO:
                    votes.append('BULL')
                    reasons.append(f'skew {skew_ratio:.2f} (puts rich → sell puts)')
                elif skew_ratio <= BIAS_SKEW_NEUTRAL_LOW:
                    votes.append('BEAR')
                    reasons.append(f'skew {skew_ratio:.2f} (calls rich → unusual demand)')
                else:
                    votes.append('NEUTRAL')
                    reasons.append(f'skew {skew_ratio:.2f} (balanced)')
            else:
                votes.append('NEUTRAL')
                reasons.append('skew: no IV data')
        except Exception:
            votes.append('NEUTRAL')
            reasons.append('skew: calc error')
    else:
        votes.append('NEUTRAL')
        reasons.append('skew: no chain data')

    # Tally: 2 of 3 agree → bias confirmed
    bull_votes = votes.count('BULL')
    bear_votes = votes.count('BEAR')

    if bull_votes >= 2:
        bias = 'BULL'
    elif bear_votes >= 2:
        bias = 'BEAR'
    else:
        bias = 'NEUTRAL'

    reason = f'[{bias}] {" | ".join(reasons)} ({bull_votes}B/{bear_votes}b/{votes.count("NEUTRAL")}N)'
    log.info(f'Bias: {reason}')
    return bias, reason


# ── Earnings Cache ─────────────────────────────────────────────────

def fetch_earnings_cache(tt_metrics=None):
    """Return {symbol: 'YYYY-MM-DD'} for upcoming earnings.
    If tt_metrics dict provided, refreshes from TT and writes file.
    Otherwise reads today's file cache; fetches fresh only if stale."""
    today_str = date.today().isoformat()

    try:
        cached = json.load(open(EARNINGS_CACHE))
        if cached.get('date') == today_str and tt_metrics is None:
            return cached.get('earnings', {})
    except Exception:
        pass

    earnings = {}

    if tt_metrics is None:
        try:
            import tasty as tasty_mod
            tt_metrics = _run_tt(tasty_mod.tt_get_metrics_batch(list(STOCK_SYMBOLS)))
        except Exception as e:
            log.warning(f'Earnings TT fetch error: {str(e)[:50]}')
            return {}

    for sym, m in tt_metrics.items():
        if sym not in STOCK_SYMBOLS:
            continue
        try:
            if m.earnings and m.earnings.expected_report_date:
                ed = m.earnings.expected_report_date
                if ed >= date.today():
                    earnings[sym] = str(ed)
        except Exception as e:
            log.warning(f'Earnings parse {sym}: {str(e)[:40]}')

    try:
        json.dump({'date': today_str, 'earnings': earnings},
                  open(EARNINGS_CACHE, 'w'), indent=2)
        log.info(f'Earnings cached from TT: {len(earnings)} symbols')
    except Exception as e:
        log.warning(f'Earnings cache write error: {str(e)[:50]}')

    return earnings


# ── Option Chain — Find Target Strike ──────────────────────────────

def _min_credit_for_price(price):
    """Minimum meaningful credit by stock price tier.
    Below this, fill risk and commissions eat the edge."""
    if price < 20:
        return 0.10
    elif price < 50:
        return 0.20
    elif price < 100:
        return 0.40
    else:
        return 0.75


def _find_strike(option_type, price, options, greeks_data, iv_frac, T, r, em_dollar):
    """Find the strike nearest to TARGET_DELTA (16 delta) that also clears
    the expected move distance. Returns the best candidate dict or None.

    Selection priority:
      1. Strike beyond the expected move AND nearest to 16 delta
      2. Strike nearest to 16 delta (regardless of EM, if no EM candidate)

    No ROR gate — TastyTrade uses delta + POP as the primary filter,
    not a return-on-risk calculation."""
    candidates = []
    dyn_min_credit = _min_credit_for_price(price)

    for opt in options:
        strike    = float(opt.get('strike', opt.get('strike_price', 0)))
        opt_type  = str(opt.get('option_type', '')).upper()

        # Filter side
        if option_type == 'put':
            if 'P' not in opt_type and 'PUT' not in opt_type:
                continue
            if strike >= price:
                continue
        else:
            if 'C' not in opt_type and 'CALL' not in opt_type:
                continue
            if strike <= price:
                continue

        gd      = greeks_data.get(opt.get('tt_symbol', opt.get('symbol', '')), {})
        bid     = round(float(gd.get('bid', 0)), 2)
        ask     = float(gd.get('ask', 0))
        oi      = gd.get('open_interest')
        mid_iv  = gd.get('mid_iv') or iv_frac

        # Credit floor
        if bid < dyn_min_credit:
            continue

        # OI gate
        if oi is not None and oi < MIN_OPTION_OI:
            continue

        # Bid-ask spread quality gate
        if ask > 0 and bid > 0:
            mid = (bid + ask) / 2
            if mid > 0 and (ask - bid) / mid * 100 > MAX_SPREAD_PCT:
                continue

        # Delta
        delta = bs_delta(price, strike, T, r, float(mid_iv), option_type)
        abs_delta = abs(delta)

        # Accept range 0.10–0.35 to have candidates; we'll pick nearest to 0.16
        if not (0.10 <= abs_delta <= 0.35):
            continue

        pop = bs_prob_otm(price, strike, T, r, float(mid_iv), option_type)
        buf_dollar = abs(strike - price)

        candidates.append({
            'strike':    strike,
            'bid':       bid,
            'ask':       ask,
            'delta':     delta,
            'abs_delta': abs_delta,
            'pop':       pop,
            'iv':        round(float(mid_iv) * 100, 1),
            'oi':        int(oi) if oi is not None else None,
            'buf_dollar': round(buf_dollar, 2),
            'em_cleared': buf_dollar >= em_dollar,
            'tt_symbol': opt.get('tt_symbol', opt.get('symbol', '')),
            'tt_opt':    opt,
        })

    if not candidates:
        return None

    # Sort by closeness to TARGET_DELTA
    candidates.sort(key=lambda c: abs(c['abs_delta'] - TARGET_DELTA))

    # Prefer EM-cleared, nearest to 16 delta
    em_candidates = [c for c in candidates if c['em_cleared']]
    if em_candidates:
        best = em_candidates[0]
        log.info(f'Strike found (EM-cleared): {option_type} {best["strike"]} '
                 f'delta={best["delta"]:.3f} pop={best["pop"]:.1f}% bid=${best["bid"]:.2f}')
        return best

    # Fallback: nearest 16-delta regardless of EM distance
    best = candidates[0]
    log.info(f'Strike found (delta-nearest fallback): {option_type} {best["strike"]} '
             f'delta={best["delta"]:.3f} pop={best["pop"]:.1f}% bid=${best["bid"]:.2f}')
    return best


# ── Main Data Fetch ────────────────────────────────────────────────

def get_options_data(symbol, vix_dir='unknown', tt_cache=None,
                     dte_min_override=None, dte_max_override=None):
    """Fetch all data needed to select a strategy and build strikes for a symbol.

    Steps:
      1. Price history (5-day move for bias signal 1)
      2. Spot price
      3. IVR + 30d IV → IVR classification (HIGH / MEDIUM / LOW)
      4. Option chain at ~45 DTE expiry
      5. Delta/POP for each candidate strike
      6. Expected move calculation
      7. Vol skew (put IV / call IV) for bias signal 3
      8. Directional bias (3 signals)
      9. Earnings check

    Returns data dict for signals.py. Returns None if symbol should be skipped."""
    try:
        import tasty as tasty_mod

        # ── 1. Price history → 5-day move ──────────────────────────
        hist = None
        if tt_cache and 'history' in tt_cache:
            hist = tt_cache['history'].get(symbol)
        if hist is None:
            h_dict = _run_tt(tasty_mod.tt_prefetch_history([symbol], days=60), timeout=30)
            hist   = h_dict.get(symbol)
        if hist is None or hist.empty or len(hist) < 6:
            log.info(f'SKIP {symbol}: no price history from TT')
            return None

        closes = list(hist['Close'].astype(float))

        # ── 2. Spot price ──────────────────────────────────────────
        price = round(float(closes[-1]), 2)
        if tt_cache and 'spots' in tt_cache and symbol in tt_cache['spots']:
            price = tt_cache['spots'][symbol]
        else:
            spots = _run_tt(tasty_mod.tt_get_spot_batch([symbol]))
            if spots.get(symbol):
                price = spots[symbol]

        # ── 3. IVR + 30d IV ────────────────────────────────────────
        ivr    = None
        avg_iv = 30.0
        m      = None

        if tt_cache and 'metrics' in tt_cache and symbol in tt_cache['metrics']:
            m = tt_cache['metrics'][symbol]
            if m.implied_volatility_index_rank is not None:
                ivr = round(float(m.implied_volatility_index_rank) * 100, 1)
            if m.implied_volatility_30_day:
                avg_iv = round(float(m.implied_volatility_30_day), 1)
            elif m.implied_volatility_index:
                avg_iv = round(float(m.implied_volatility_index) * 100, 1)
        if ivr is None:
            ivr = calculate_ivr(symbol)

        if ivr is None:
            log.info(f'SKIP {symbol}: IVR unavailable')
            return None

        ivr_regime = classify_ivr(ivr)
        iv_frac    = avg_iv / 100

        log.info(f'{symbol}: price={price} IVR={ivr}% ({ivr_regime}) IV={avg_iv:.1f}%')

        # ── 4. Option chain ─────────────────────────────────────────
        dte_min = dte_min_override if dte_min_override is not None else DTE_MIN
        dte_max = dte_max_override if dte_max_override is not None else DTE_MAX

        _expiry_list = _run_tt(
            tasty_mod.tt_get_option_instruments(symbol, dte_min, dte_max,
                                                prefer_nearest=False, ranked=True)
        )
        if not _expiry_list:
            log.info(f'SKIP {symbol}: no expiry in DTE range {dte_min}–{dte_max}')
            return None

        # Pick expiry nearest to DTE_SWEET_SPOT (45 DTE)
        target_exp, target_dte, options = min(
            _expiry_list,
            key=lambda x: abs(x[1] - DTE_SWEET_SPOT)
        )

        log.info(f'{symbol}: expiry {target_exp} ({target_dte} DTE, sweet spot {DTE_SWEET_SPOT})')

        # ── 5. Greeks fetch ─────────────────────────────────────────
        # Filter to 5–45% OTM range for efficiency
        candidate_opts = []
        call_map = {}
        put_map  = {}
        for opt in options:
            strike  = float(opt.strike_price)
            otm_pct = abs(strike - price) / price
            if 0.05 <= otm_pct <= 0.45:
                candidate_opts.append(opt)
                if opt.option_type.value == 'C':
                    call_map[opt.symbol] = {'strike': strike, 'opt': opt,
                                            'strike_price': strike, 'symbol': opt.symbol,
                                            'option_type': 'C', 'tt_symbol': opt.symbol, 'tt_opt': opt}
                else:
                    put_map[opt.symbol]  = {'strike': strike, 'opt': opt,
                                            'strike_price': strike, 'symbol': opt.symbol,
                                            'option_type': 'P', 'tt_symbol': opt.symbol, 'tt_opt': opt}

        greeks_data = {}
        if candidate_opts:
            greeks_data = _run_tt(tasty_mod.tt_get_greeks_for_options(candidate_opts), timeout=30)

        # Build chain lists with greeks attached
        def _enrich(sym_map, opt_type_str):
            result = []
            for sym, info in sym_map.items():
                gd = greeks_data.get(sym, {})
                entry = {
                    'strike':      info['strike'],
                    'option_type': opt_type_str,
                    'bid':         round(float(gd.get('bid', 0)), 2),
                    'ask':         float(gd.get('ask', 0)),
                    'last':        float(gd.get('price', 0)),
                    'open_interest': int(gd['open_interest']) if gd.get('open_interest') is not None else None,
                    'volume':      int(gd['volume']) if gd.get('volume') is not None else None,
                    'greeks':      {'mid_iv': gd.get('mid_iv') or iv_frac},
                    'tt_symbol':   sym,
                    'tt_opt':      info['opt'],
                }
                result.append(entry)
            return result

        chain_calls = sorted(_enrich(call_map, 'call'), key=lambda x: x['strike'])
        chain_puts  = sorted(_enrich(put_map,  'put'),  key=lambda x: x['strike'], reverse=True)

        # ── 6. Expected move ────────────────────────────────────────
        # Use expiry-specific IV if available from TT metrics
        expiry_iv_frac = iv_frac
        if m is not None and m.option_expiration_implied_volatilities:
            for _oeiv in m.option_expiration_implied_volatilities:
                if _oeiv.expiration_date == target_exp and _oeiv.implied_volatility is not None:
                    expiry_iv_frac = float(_oeiv.implied_volatility)
                    break

        em_dollar, em_pct = expected_move(price, expiry_iv_frac * 100, target_dte)
        log.info(f'Expected move {symbol}: ±{em_pct:.1f}% (${em_dollar:.2f}) [{target_exp} {target_dte}DTE]')

        # ── 7. Find best strikes at 16 delta ───────────────────────
        T = target_dte / 365
        r = 0.05

        best_put  = _find_strike('put',  price, chain_puts,  greeks_data, expiry_iv_frac, T, r, em_dollar)
        best_call = _find_strike('call', price, chain_calls, greeks_data, expiry_iv_frac, T, r, em_dollar)

        # ── 8. Directional bias (3-signal) ──────────────────────────
        bias, bias_reason = get_bias(closes, vix_dir, chain_calls, chain_puts, expiry_iv_frac)

        # ── 9. Earnings check ───────────────────────────────────────
        earnings = []
        try:
            ec = json.load(open(EARNINGS_CACHE)).get('earnings', {})
            if symbol in ec:
                ed = date.fromisoformat(ec[symbol])
                days_away = (ed - date.today()).days
                if 0 <= days_away <= 30:
                    earnings.append(f'EARNINGS {ec[symbol]} ({days_away}d)')
        except Exception:
            pass

        return {
            'symbol':       symbol,
            'price':        price,
            'move_5d':      round((closes[-1] - closes[-6]) / closes[-6] * 100, 1) if len(closes) >= 6 else 0.0,
            'avg_iv':       avg_iv,
            'ivr':          ivr,
            'ivr_regime':   ivr_regime,       # 'HIGH' | 'MEDIUM' | 'LOW'
            'bias':         bias,             # 'BULL' | 'BEAR' | 'NEUTRAL'
            'bias_reason':  bias_reason,
            'expiry':       target_exp,
            'dte':          target_dte,
            'em_dollar':    em_dollar,
            'em_pct':       em_pct,
            'best_put':     best_put,
            'best_call':    best_call,
            'chain_calls':  chain_calls,
            'chain_puts':   chain_puts,
            'earnings':     earnings,
            'sector':       SECTOR_MAP.get(symbol, 'Unknown'),
        }

    except Exception as e:
        log.warning(f'Options data error {symbol}: {str(e)[:80]}', exc_info=True)
        return None
