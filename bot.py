"""
bot.py — Kubera consolidated bot (4-file architecture)

All logic from market.py, signals.py, strategies.py, monitor.py,
telegram_handlers.py merged here. Only 4 files remain:
  config.py, tasty.py, database.py, bot.py

Scan schedule (market days only):
  08:00 ET — balance sync from TastyTrade NLV
  09:45 ET — full watchlist scan
  every 30 min (09:30–16:30 ET) — position monitor
"""

import asyncio
import json
import logging
import math
import os
import threading
from datetime import datetime, date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from scipy.stats import norm
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

import config as _cfg
from config import (
    log, ET, TG_TOKEN, TG_CHAT,
    TT_CLIENT_SECRET, TT_REFRESH_TOKEN, TT_ACCOUNT, TT_SANDBOX,
    WATCHLIST, MAX_POSITIONS, MAX_SECTOR_POSITIONS,
    DTE_MIN, DTE_MAX, DTE_SWEET_SPOT, DTE_EXIT,
    PROFIT_TARGET_CREDIT, PROFIT_TARGET_DEBIT, PROFIT_TARGET_CALENDAR,
    LOSS_STOP_MULTIPLIER, DEBIT_HARD_STOP_PCT,
    CAL_DTE_FRONT, CAL_DTE_BACK, CAL_IV_MAX,
    KILL_SWITCH, REAL_BALANCE,
    IVR_HIGH, IVR_MEDIUM_FLOOR, IVR_LOW_CEILING,
    BIAS_MOVE_THRESHOLD, BIAS_VIX_LOOKBACK, BIAS_VIX_MOVE_PCT,
    BIAS_SKEW_RATIO, BIAS_SKEW_NEUTRAL_LOW,
    MIN_OPTION_OI, MAX_SPREAD_PCT,
    ETF_SYMBOLS, STOCK_SYMBOLS, SECTOR_MAP,
    TARGET_DELTA, MIN_POP,
    JADE_MIN_CREDIT_RATIO, IC_CREDIT_RATIO, IC_MAX_IVR,
    MAX_BPR_PCT, MIN_BPR_GATE_PCT, EARNINGS_BLOCK,
    SAFE_HAVEN_SYMBOLS, BLOCKED_SYMBOLS,
    ROLL_POP_FLOOR,
)
import tasty as tt
import database as db


# ── File paths ─────────────────────────────────────────────────────

EARNINGS_CACHE = '/home/trader/kubera/data/earnings_cache.json'
IV_CACHE_PATH  = '/home/trader/kubera/data/iv_cache.json'


# ── Black-Scholes ──────────────────────────────────────────────────

def bs_delta(S, K, T, r, sigma, option_type):
    """Black-Scholes delta. Used to find the 16-delta short strike."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return round(norm.cdf(d1) if option_type == 'call' else norm.cdf(d1) - 1, 4)


def bs_prob_otm(S, K, T, r, sigma, option_type):
    """Black-Scholes probability the option expires OTM (= POP for short option)."""
    if T <= 0 or sigma <= 0:
        return 50.0
    d2 = (math.log(S / K) + (r - 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return round(norm.cdf(-d2) * 100 if option_type == 'call' else norm.cdf(d2) * 100, 1)


# ── Expected Move ──────────────────────────────────────────────────

def expected_move(price, iv_pct, dte):
    """TastyTrade formula: EM = Stock × (IV/100) × √(DTE/365).
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

async def calculate_ivr(symbol, tt_metrics_cache=None):
    """IVR from TastyTrade's implied_volatility_index_rank.
    Falls back to rolling 52-week file cache if TT unavailable."""
    try:
        m = None
        if tt_metrics_cache and symbol in tt_metrics_cache:
            m = tt_metrics_cache[symbol]
        else:
            metrics = await tt.tt_get_metrics_batch([symbol])
            m = metrics.get(symbol)

        if m:
            ivr = m.implied_volatility_index_rank
            if ivr is not None:
                val = round(float(ivr) * 100, 1)
                log.info(f'IVR {symbol}: {val}% (TT tos_ivr)')
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
    """Classify IVR into regime: 'HIGH' | 'MEDIUM' | 'LOW' | None."""
    if ivr is None:
        return None
    if ivr >= IVR_HIGH:
        return 'HIGH'
    if ivr >= IVR_MEDIUM_FLOOR:
        return 'MEDIUM'
    return 'LOW'


# ── VIX ────────────────────────────────────────────────────────────

async def get_vix_data():
    """Fetch VIX/VIX9D/VIX3M via TastyTrade DXLink.
    Returns dict with vix, vix_dir, vix9d, vix3m."""
    try:
        from tastytrade.streamer import DXLinkStreamer
        from tastytrade.dxfeed import Trade, Summary

        syms = ['VIX', 'VIX9D', 'VIX3M']
        trades    = {}
        prevclose = {}

        async with DXLinkStreamer(tt.TT_SESSION) as streamer:
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

        vix_prev = prevclose.get('VIX')
        vix_dir  = 'unknown'
        if vix_prev and vix_prev > 0:
            change_pct = (vix - vix_prev) / vix_prev
            if change_pct >= BIAS_VIX_MOVE_PCT:
                vix_dir = 'rising'
            elif change_pct <= -BIAS_VIX_MOVE_PCT:
                vix_dir = 'falling'
            else:
                vix_dir = 'flat'

        log.info(f'VIX: {vix} (prev={vix_prev}, dir={vix_dir}) VIX9D={vix9d} VIX3M={vix3m}')
        return {'vix': vix, 'vix9d': vix9d, 'vix3m': vix3m, 'vix_dir': vix_dir, 'vix_prev': vix_prev}

    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f'VIX fetch failed: {str(e)[:120]}') from e


async def get_vix():
    ctx = await get_vix_data()
    return ctx['vix']


# ── Directional Bias Detection ─────────────────────────────────────

def get_bias(closes, vix_dir, chain_calls=None, chain_puts=None, iv_frac=None):
    """3-signal bias vote: price action + VIX direction + vol skew.
    Returns ('BULL' | 'BEAR' | 'NEUTRAL', reason_string)."""
    votes   = []
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
            call_ivs = [c['greeks']['mid_iv'] for c in chain_calls
                        if c.get('greeks', {}).get('mid_iv') and c['greeks']['mid_iv'] > 0]
            put_ivs  = [p['greeks']['mid_iv'] for p in chain_puts
                        if p.get('greeks', {}).get('mid_iv') and p['greeks']['mid_iv'] > 0]
            if call_ivs and put_ivs:
                avg_call_iv = sum(call_ivs[:5]) / min(5, len(call_ivs))
                avg_put_iv  = sum(put_ivs[:5])  / min(5, len(put_ivs))
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

async def fetch_earnings_cache(tt_metrics=None):
    """Return {symbol: 'YYYY-MM-DD'} for upcoming earnings."""
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
            tt_metrics = await tt.tt_get_metrics_batch(list(STOCK_SYMBOLS))
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
    if price < 20:
        return 0.10
    elif price < 50:
        return 0.20
    elif price < 100:
        return 0.40
    else:
        return 0.75


def _find_strike(option_type, price, options, greeks_data, iv_frac, T, r, em_dollar):
    """Find the strike nearest to TARGET_DELTA (16 delta) that also clears the expected move."""
    candidates = []
    dyn_min_credit = _min_credit_for_price(price)

    for opt in options:
        strike   = float(opt.get('strike', opt.get('strike_price', 0)))
        opt_type = str(opt.get('option_type', '')).upper()

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

        gd     = greeks_data.get(opt.get('tt_symbol', opt.get('symbol', '')), {})
        bid    = round(float(gd.get('bid', 0)), 2)
        ask    = float(gd.get('ask', 0))
        oi     = gd.get('open_interest')
        mid_iv = gd.get('mid_iv') or iv_frac

        if bid < dyn_min_credit:
            continue
        if oi is not None and oi < MIN_OPTION_OI:
            continue
        if ask > 0 and bid > 0:
            mid = (bid + ask) / 2
            if mid > 0 and (ask - bid) / mid * 100 > MAX_SPREAD_PCT:
                continue

        delta     = bs_delta(price, strike, T, r, float(mid_iv), option_type)
        abs_delta = abs(delta)
        if not (0.10 <= abs_delta <= 0.35):
            continue

        pop        = bs_prob_otm(price, strike, T, r, float(mid_iv), option_type)
        buf_dollar = abs(strike - price)

        candidates.append({
            'strike':     strike,
            'bid':        bid,
            'ask':        ask,
            'delta':      delta,
            'abs_delta':  abs_delta,
            'pop':        pop,
            'iv':         round(float(mid_iv) * 100, 1),
            'oi':         int(oi) if oi is not None else None,
            'buf_dollar': round(buf_dollar, 2),
            'em_cleared': buf_dollar >= em_dollar,
            'tt_symbol':  opt.get('tt_symbol', opt.get('symbol', '')),
            'tt_opt':     opt,
        })

    if not candidates:
        return None

    candidates.sort(key=lambda c: abs(c['abs_delta'] - TARGET_DELTA))
    em_candidates = [c for c in candidates if c['em_cleared']]
    if em_candidates:
        best = em_candidates[0]
        log.info(f'Strike (EM-cleared): {option_type} {best["strike"]} '
                 f'delta={best["delta"]:.3f} pop={best["pop"]:.1f}% bid=${best["bid"]:.2f}')
        return best

    best = candidates[0]
    log.info(f'Strike (delta-nearest fallback): {option_type} {best["strike"]} '
             f'delta={best["delta"]:.3f} pop={best["pop"]:.1f}% bid=${best["bid"]:.2f}')
    return best


# ── Main Data Fetch ────────────────────────────────────────────────

async def get_options_data(symbol, vix_dir='unknown', tt_cache=None,
                           dte_min_override=None, dte_max_override=None):
    """Fetch all data needed to select a strategy and build strikes.
    Returns data dict or None if symbol should be skipped."""
    try:
        # ── 1. Price history ───────────────────────────────────────
        hist = None
        if tt_cache and 'history' in tt_cache:
            hist = tt_cache['history'].get(symbol)
        if hist is None:
            h_dict = await tt.tt_prefetch_history([symbol], days=60)
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
            spots = await tt.tt_get_spot_batch([symbol])
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
            ivr = await calculate_ivr(symbol)

        if ivr is None:
            log.info(f'SKIP {symbol}: IVR unavailable')
            return None

        ivr_regime = classify_ivr(ivr)
        iv_frac    = avg_iv / 100

        log.info(f'{symbol}: price={price} IVR={ivr}% ({ivr_regime}) IV={avg_iv:.1f}%')

        # ── 4. Option chain ─────────────────────────────────────────
        dte_min = dte_min_override if dte_min_override is not None else DTE_MIN
        dte_max = dte_max_override if dte_max_override is not None else DTE_MAX

        _expiry_list = await tt.tt_get_option_instruments(
            symbol, dte_min, dte_max, prefer_nearest=False, ranked=True
        )
        if not _expiry_list:
            log.info(f'SKIP {symbol}: no expiry in DTE range {dte_min}–{dte_max}')
            return None

        target_exp, target_dte, options = min(
            _expiry_list, key=lambda x: abs(x[1] - DTE_SWEET_SPOT)
        )
        log.info(f'{symbol}: expiry {target_exp} ({target_dte} DTE)')

        # ── 5. Greeks fetch ─────────────────────────────────────────
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
            greeks_data = await tt.tt_get_greeks_for_options(candidate_opts)

        def _enrich(sym_map, opt_type_str):
            result = []
            for sym, info in sym_map.items():
                gd = greeks_data.get(sym, {})
                entry = {
                    'strike':        info['strike'],
                    'option_type':   opt_type_str,
                    'bid':           round(float(gd.get('bid', 0)), 2),
                    'ask':           float(gd.get('ask', 0)),
                    'last':          float(gd.get('price', 0)),
                    'open_interest': int(gd['open_interest']) if gd.get('open_interest') is not None else None,
                    'volume':        int(gd['volume']) if gd.get('volume') is not None else None,
                    'greeks':        {'mid_iv': gd.get('mid_iv') or iv_frac},
                    'tt_symbol':     sym,
                    'tt_opt':        info['opt'],
                }
                result.append(entry)
            return result

        chain_calls = sorted(_enrich(call_map, 'call'), key=lambda x: x['strike'])
        chain_puts  = sorted(_enrich(put_map,  'put'),  key=lambda x: x['strike'], reverse=True)

        # ── 6. Expected move ────────────────────────────────────────
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

        # ── 8. Directional bias ─────────────────────────────────────
        bias, bias_reason = get_bias(closes, vix_dir, chain_calls, chain_puts, expiry_iv_frac)

        # ── 9. Earnings check ───────────────────────────────────────
        earnings = []
        try:
            ec = json.load(open(EARNINGS_CACHE)).get('earnings', {})
            if symbol in ec:
                ed        = date.fromisoformat(ec[symbol])
                days_away = (ed - date.today()).days
                if 0 <= days_away <= 30:
                    earnings.append(f'EARNINGS {ec[symbol]} ({days_away}d)')
        except Exception:
            pass

        return {
            'symbol':      symbol,
            'price':       price,
            'move_5d':     round((closes[-1] - closes[-6]) / closes[-6] * 100, 1) if len(closes) >= 6 else 0.0,
            'avg_iv':      avg_iv,
            'ivr':         ivr,
            'ivr_regime':  ivr_regime,
            'bias':        bias,
            'bias_reason': bias_reason,
            'expiry':      target_exp,
            'dte':         target_dte,
            'em_dollar':   em_dollar,
            'em_pct':      em_pct,
            'best_put':    best_put,
            'best_call':   best_call,
            'chain_calls': chain_calls,
            'chain_puts':  chain_puts,
            'earnings':    earnings,
            'sector':      SECTOR_MAP.get(symbol, 'Unknown'),
        }

    except Exception as e:
        log.warning(f'Options data error {symbol}: {str(e)[:80]}', exc_info=True)
        return None


# ── Strategy Decision Tree ─────────────────────────────────────────

def select_strategy(ivr_regime, bias, data):
    """Select strategy from IVR regime + bias.
    Returns (strategy_name, sub_type, reason)."""
    move_5d = data.get('move_5d', 0.0)

    if ivr_regime == 'HIGH':
        if bias == 'BULL':
            return 'jade_lizard', '', (
                f'HIGH IVR ({data["ivr"]:.0f}%) + BULL → Jade Lizard '
                f'(degrades to put credit spread if credit math fails)'
            )
        elif bias == 'BEAR':
            return 'call_credit_spread', '', f'HIGH IVR ({data["ivr"]:.0f}%) + BEAR → call credit spread'
        else:
            return 'iron_condor', '', f'HIGH IVR ({data["ivr"]:.0f}%) + NEUTRAL → iron condor'

    elif ivr_regime == 'MEDIUM':
        if bias == 'BULL':
            return 'put_credit_spread', '', f'MEDIUM IVR ({data["ivr"]:.0f}%) + BULL → put credit spread'
        elif bias == 'BEAR':
            return 'call_credit_spread', '', f'MEDIUM IVR ({data["ivr"]:.0f}%) + BEAR → call credit spread'
        else:
            return 'skip', '', f'MEDIUM IVR ({data["ivr"]:.0f}%) + NEUTRAL → SKIP (insufficient premium)'

    elif ivr_regime == 'LOW':
        if move_5d <= -BIAS_MOVE_THRESHOLD * 100:
            return 'debit_spread', 'call', (
                f'LOW IVR ({data["ivr"]:.0f}%) + stock down {move_5d:.1f}% → call debit spread'
            )
        elif move_5d >= BIAS_MOVE_THRESHOLD * 100:
            return 'debit_spread', 'put', (
                f'LOW IVR ({data["ivr"]:.0f}%) + stock up +{move_5d:.1f}% → put debit spread'
            )
        else:
            return 'calendar_spread', '', (
                f'LOW IVR ({data["ivr"]:.0f}%) + stock flat → calendar spread (IV expansion bet)'
            )
    else:
        return 'skip', '', 'IVR unavailable or unclassified — SKIP'


def check_jade_lizard(best_put, best_call, call_spread_width):
    """Returns (is_valid, total_credit, reason)."""
    if best_put is None or best_call is None:
        return False, 0.0, 'missing put or call strike — cannot build Jade Lizard'

    put_credit          = best_put.get('bid', 0.0)
    call_credit         = best_call.get('bid', 0.0)
    total_credit        = round(put_credit + call_credit, 2)
    required_min_credit = round(call_spread_width * JADE_MIN_CREDIT_RATIO, 2)

    if total_credit >= required_min_credit:
        return True, total_credit, (
            f'Jade Lizard valid: credit ${total_credit:.2f} ≥ '
            f'call spread ${call_spread_width:.0f} × {JADE_MIN_CREDIT_RATIO} = ${required_min_credit:.2f}'
        )
    else:
        return False, total_credit, (
            f'Jade Lizard invalid: credit ${total_credit:.2f} < required ${required_min_credit:.2f} '
            f'→ downgrade to put credit spread'
        )


# ── Entry Validation ──────────────────────────────────────────────

def validate_entry(data, strategy, sub_type, earnings_cache, open_positions):
    """Returns (ok: bool, failures: list[str])."""
    symbol  = data.get('symbol', '')
    sector  = data.get('sector', 'Unknown')
    ivr     = data.get('ivr', 0)
    failures = []

    if any(p.get('symbol') == symbol and p.get('status') == 'open' for p in open_positions):
        failures.append(f'{symbol} already has an open position')
        return False, failures

    if symbol in BLOCKED_SYMBOLS:
        failures.append(f'{symbol} is permanently blocked')
        return False, failures

    if earnings_cache and symbol in earnings_cache:
        try:
            ed = date.fromisoformat(earnings_cache[symbol])
            days_away = (ed - date.today()).days
            if 0 <= days_away <= EARNINGS_BLOCK:
                failures.append(f'Earnings in {days_away}d ({earnings_cache[symbol]}) — blocked')
                return False, failures
        except Exception:
            pass

    best_put  = data.get('best_put')
    best_call = data.get('best_call')

    if strategy in ('put_credit_spread', 'jade_lizard') and best_put is None:
        failures.append(f'no valid put strike at 16 delta — cannot build {strategy}')
        return False, failures

    if strategy == 'call_credit_spread' and best_call is None:
        failures.append('no valid call strike at 16 delta')
        return False, failures

    if strategy == 'iron_condor' and (best_put is None or best_call is None):
        missing = 'put' if best_put is None else 'call'
        failures.append(f'no valid {missing} strike — cannot build iron condor')
        return False, failures

    credit_strategies = ('put_credit_spread', 'call_credit_spread', 'iron_condor', 'jade_lizard')
    if strategy in credit_strategies:
        if strategy in ('put_credit_spread', 'jade_lizard') and best_put:
            pop = best_put.get('pop', 0.0)
            if pop < MIN_POP * 100:
                failures.append(f'put POP {pop:.1f}% < {MIN_POP*100:.0f}% minimum')
                return False, failures

        if strategy == 'call_credit_spread' and best_call:
            pop = best_call.get('pop', 0.0)
            if pop < MIN_POP * 100:
                failures.append(f'call POP {pop:.1f}% < {MIN_POP*100:.0f}% minimum')
                return False, failures

        if strategy == 'iron_condor' and best_put and best_call:
            put_pop  = best_put.get('pop', 0.0)
            call_pop = best_call.get('pop', 0.0)
            if put_pop < MIN_POP * 100:
                failures.append(f'IC put leg POP {put_pop:.1f}% < {MIN_POP*100:.0f}%')
                return False, failures
            if call_pop < MIN_POP * 100:
                failures.append(f'IC call leg POP {call_pop:.1f}% < {MIN_POP*100:.0f}%')
                return False, failures

    if strategy == 'call_credit_spread' and symbol in SAFE_HAVEN_SYMBOLS:
        failures.append(f'{symbol} is a safe-haven — call credits always blocked')
        return False, failures

    if strategy == 'iron_condor' and ivr > IC_MAX_IVR:
        failures.append(f'IC blocked: IVR {ivr:.0f}% > {IC_MAX_IVR:.0f}% (extreme IV implies large move)')
        return False, failures

    sector_count = sum(
        1 for p in open_positions
        if SECTOR_MAP.get(p.get('symbol', ''), 'Unknown') == sector
        and p.get('status') == 'open'
    )
    if sector_count >= MAX_SECTOR_POSITIONS:
        failures.append(f'sector cap: {sector_count}/{MAX_SECTOR_POSITIONS} in {sector}')
        return False, failures

    open_count = sum(1 for p in open_positions if p.get('status') == 'open')
    if open_count >= MAX_POSITIONS:
        failures.append(f'max positions: {open_count}/{MAX_POSITIONS} already open')
        return False, failures

    return True, []


# ── Position Sizing ────────────────────────────────────────────────

def calculate_size(balance, bpr_per_contract):
    """Flat BPR sizing: 5% of NLV per trade. Minimum 1 contract.
    Returns 0 if 1 contract BPR exceeds MIN_BPR_GATE_PCT of balance —
    symbol is too expensive for the account size."""
    if bpr_per_contract <= 0:
        return 1
    max_bpr       = balance * MAX_BPR_PCT
    contracts_raw = int(max_bpr / bpr_per_contract)
    if contracts_raw == 0:
        gate = balance * MIN_BPR_GATE_PCT
        if bpr_per_contract > gate:
            log.info(
                f'BPR gate: 1-contract BPR ${bpr_per_contract:.0f} '
                f'> {MIN_BPR_GATE_PCT*100:.0f}% of balance (${gate:.0f}) — symbol too expensive, skip'
            )
            return 0
        contracts_raw = 1  # affordable at 1 contract (between 5%–15% of balance)
    log.info(
        f'Sizing: ${balance:.0f} × {MAX_BPR_PCT*100:.0f}% = '
        f'${max_bpr:.0f} / ${bpr_per_contract:.0f} per ct = {contracts_raw} ct'
    )
    return contracts_raw


# ── Signal Builder ─────────────────────────────────────────────────

def build_signal(data, strategy, sub_type, balance, call_spread_width=5.0):
    """Assemble the final signal dict. Returns signal dict or None."""
    symbol    = data.get('symbol', '')
    expiry    = data.get('expiry')
    dte       = data.get('dte', 0)
    price     = data.get('price', 0.0)
    ivr       = data.get('ivr', 0.0)
    avg_iv    = data.get('avg_iv', 30.0)
    em_dollar = data.get('em_dollar', 0.0)
    em_pct    = data.get('em_pct', 0.0)
    best_put  = data.get('best_put')
    best_call = data.get('best_call')

    sig = {
        'symbol':       symbol,
        'strategy':     strategy,
        'sub_type':     sub_type,
        'expiry':       str(expiry),
        'dte':          dte,
        'price':        price,
        'avg_iv':       avg_iv,
        'em_dollar':    em_dollar,
        'em_pct':       em_pct,
        'ivr':          ivr,
        'ivr_regime':   data.get('ivr_regime', ''),
        'bias':         data.get('bias', 'NEUTRAL'),
        'bias_reason':  data.get('bias_reason', ''),
        'sector':       data.get('sector', 'Unknown'),
        'dte_exit':     DTE_EXIT,
        'roll_pop_floor': ROLL_POP_FLOOR,
    }

    if strategy in ('put_credit_spread', 'jade_lizard', 'iron_condor'):
        if best_put is None:
            log.warning(f'build_signal: {symbol} {strategy} — no put strike')
            return None

        put_credit   = best_put.get('bid', 0.0)
        put_strike   = best_put.get('strike', 0.0)
        put_pop      = best_put.get('pop', 0.0)
        put_delta    = best_put.get('delta', 0.0)
        put_iv       = best_put.get('iv', avg_iv)
        wing_width   = round(max(call_spread_width, em_dollar * 1.5), 0)

        bpr_per_contract = (wing_width - put_credit) * 100
        contracts        = calculate_size(balance, bpr_per_contract)
        if contracts == 0:
            return None
        max_loss         = round(bpr_per_contract * contracts, 2)
        profit_target    = round(put_credit * PROFIT_TARGET_CREDIT, 2)
        loss_stop        = round(put_credit * LOSS_STOP_MULTIPLIER, 2)

        sig.update({
            'sell_strike':   put_strike,
            'buy_strike':    round(put_strike - wing_width, 2),
            'credit':        put_credit,
            'pop':           put_pop,
            'delta':         put_delta,
            'iv':            put_iv,
            'wing_width':    wing_width,
            'contracts':     contracts,
            'max_loss':      max_loss,
            'profit_target': profit_target,
            'loss_stop':     loss_stop,
        })

        if strategy == 'jade_lizard' and best_call is not None:
            jl_valid, jl_credit, jl_reason = check_jade_lizard(best_put, best_call, call_spread_width)
            if jl_valid:
                call_credit  = best_call.get('bid', 0.0)
                call_strike  = best_call.get('strike', 0.0)
                total_credit = round(put_credit + call_credit, 2)
                bpr_per_contract = (wing_width - total_credit) * 100
                contracts        = calculate_size(balance, bpr_per_contract)
                if contracts == 0:
                    return None
                max_loss         = round(bpr_per_contract * contracts, 2)
                profit_target    = round(total_credit * PROFIT_TARGET_CREDIT, 2)
                loss_stop        = round(total_credit * LOSS_STOP_MULTIPLIER, 2)
                sig.update({
                    'strategy':          'jade_lizard',
                    'call_strike':       call_strike,
                    'call_buy_strike':   round(call_strike + call_spread_width, 2),
                    'call_spread_width': call_spread_width,
                    'credit':            total_credit,
                    'put_credit':        put_credit,
                    'call_credit':       call_credit,
                    'contracts':         contracts,
                    'max_loss':          max_loss,
                    'profit_target':     profit_target,
                    'loss_stop':         loss_stop,
                    'jade_reason':       jl_reason,
                })
                log.info(f'{symbol}: Jade Lizard confirmed — {jl_reason}')
            else:
                sig['strategy'] = 'put_credit_spread'
                log.info(f'{symbol}: Jade Lizard downgraded → put credit spread — {jl_reason}')

        if strategy == 'iron_condor':
            if best_call is None:
                log.warning(f'build_signal: {symbol} iron_condor — no call strike')
                return None
            call_credit  = best_call.get('bid', 0.0)
            call_strike  = best_call.get('strike', 0.0)
            total_credit = round(put_credit + call_credit, 2)
            bpr_per_contract = (wing_width - put_credit) * 100
            contracts        = calculate_size(balance, bpr_per_contract)
            if contracts == 0:
                return None
            max_loss         = round(bpr_per_contract * contracts, 2)
            profit_target    = round(total_credit * PROFIT_TARGET_CREDIT, 2)
            loss_stop        = round(total_credit * LOSS_STOP_MULTIPLIER, 2)
            sig.update({
                'put_strike':      put_strike,
                'put_buy_strike':  round(put_strike  - wing_width, 2),
                'call_strike':     call_strike,
                'call_buy_strike': round(call_strike + wing_width, 2),
                'credit':          total_credit,
                'put_credit':      put_credit,
                'call_credit':     call_credit,
                'put_pop':         put_pop,
                'call_pop':        best_call.get('pop', 0.0),
                'wing_width':      wing_width,
                'contracts':       contracts,
                'max_loss':        max_loss,
                'profit_target':   profit_target,
                'loss_stop':       loss_stop,
            })

    elif strategy == 'call_credit_spread':
        if best_call is None:
            log.warning(f'build_signal: {symbol} call_credit_spread — no call strike')
            return None
        call_credit  = best_call.get('bid', 0.0)
        call_strike  = best_call.get('strike', 0.0)
        call_pop     = best_call.get('pop', 0.0)
        call_delta   = best_call.get('delta', 0.0)
        call_iv      = best_call.get('iv', avg_iv)
        wing_width   = round(max(call_spread_width, em_dollar * 1.5), 0)

        bpr_per_contract = (wing_width - call_credit) * 100
        contracts        = calculate_size(balance, bpr_per_contract)
        if contracts == 0:
            return None
        max_loss         = round(bpr_per_contract * contracts, 2)
        profit_target    = round(call_credit * PROFIT_TARGET_CREDIT, 2)
        loss_stop        = round(call_credit * LOSS_STOP_MULTIPLIER, 2)

        sig.update({
            'sell_strike':   call_strike,
            'buy_strike':    round(call_strike + wing_width, 2),
            'credit':        call_credit,
            'pop':           call_pop,
            'delta':         call_delta,
            'iv':            call_iv,
            'wing_width':    wing_width,
            'contracts':     contracts,
            'max_loss':      max_loss,
            'profit_target': profit_target,
            'loss_stop':     loss_stop,
        })

    elif strategy == 'debit_spread':
        leg = best_call if sub_type == 'call' else best_put
        if leg is None:
            log.warning(f'build_signal: {symbol} debit_spread/{sub_type} — no strike found')
            return None
        est_debit        = round(price * 0.02, 2)
        bpr_per_contract = est_debit * 100
        contracts        = calculate_size(balance, bpr_per_contract)
        if contracts == 0:
            return None
        max_loss         = round(bpr_per_contract * contracts, 2)
        sig.update({
            'anchor_strike':     leg.get('strike', 0.0),
            'sub_type':          sub_type,
            'est_debit':         est_debit,
            'debit_iv':          leg.get('iv', avg_iv),
            'pop':               leg.get('pop', 0.0),
            'contracts':         contracts,
            'max_loss':          max_loss,
            'profit_target_pct': PROFIT_TARGET_DEBIT,
            'loss_stop_pct':     0.50,
        })

    elif strategy == 'calendar_spread':
        est_debit        = round(price * 0.015, 2)
        bpr_per_contract = est_debit * 100
        contracts        = calculate_size(balance, bpr_per_contract)
        if contracts == 0:
            return None
        max_loss         = round(bpr_per_contract * contracts, 2)
        sig.update({
            'est_debit':         est_debit,
            'contracts':         contracts,
            'max_loss':          max_loss,
            'profit_target_pct': PROFIT_TARGET_CALENDAR,
            'loss_stop_pct':     0.50,
        })

    else:
        log.warning(f'build_signal: unknown strategy {strategy}')
        return None

    log.info(
        f'Signal built: {symbol} {sig["strategy"]} expiry={expiry} dte={dte} '
        f'ivr={ivr:.0f}% [{sig["ivr_regime"]}] bias={sig["bias"]} '
        f'contracts={sig.get("contracts",1)} max_loss=${sig.get("max_loss",0):.0f}'
    )
    return sig


# ── Wing Finder ────────────────────────────────────────────────────

def _find_wing(chain, short_strike, direction, price, em_dollar, min_credit_ratio=IC_CREDIT_RATIO):
    """Find best wing (buy) strike. Returns setup dict or None."""
    if price < 20:
        candidates = [2, 1]
    elif price < 50:
        candidates = [3, 2, 5]
    elif price < 100:
        candidates = [5, 4, 3, 7]
    else:
        candidates = [10, 15, 20, 7, 5]

    best_setup = None
    best_score = -1.0

    for wing_width in candidates:
        buy_strike = round(short_strike - wing_width if direction == 'put'
                           else short_strike + wing_width, 2)

        wing_entry = next(
            (o for o in chain if abs(float(o.get('strike', 0)) - buy_strike) < 0.01), None
        )
        if wing_entry is None:
            continue

        wing_oi = wing_entry.get('open_interest')
        if wing_oi is not None and wing_oi < MIN_OPTION_OI:
            continue

        wing_bid = float(wing_entry.get('bid', 0))
        wing_ask = float(wing_entry.get('ask', 0))
        if wing_bid > 0 and wing_ask > 0:
            wing_mid = (wing_bid + wing_ask) / 2
            if wing_mid > 0 and (wing_ask - wing_bid) / wing_mid * 100 > MAX_SPREAD_PCT:
                continue
        wing_mid = round((wing_bid + wing_ask) / 2, 2) if wing_bid > 0 else round(wing_ask * 0.5, 2)

        short_entry = next(
            (o for o in chain if abs(float(o.get('strike', 0)) - short_strike) < 0.01), None
        )
        if short_entry is None:
            continue
        sell_bid = float(short_entry.get('bid', 0))
        sell_ask = float(short_entry.get('ask', 0))
        sell_mid = round((sell_bid + sell_ask) / 2, 2) if sell_bid > 0 else sell_bid

        net_credit = round(max(sell_mid - wing_mid, 0.01), 2)
        if net_credit <= 0:
            continue

        credit_ratio = round(net_credit / wing_width, 3)
        max_loss     = round((wing_width - net_credit) * 100, 2)
        if max_loss <= 0:
            continue

        score = net_credit * (credit_ratio ** 2)
        if score > best_score:
            best_score = score
            best_setup = {
                'wing_width':   wing_width,
                'buy_strike':   buy_strike,
                'net_credit':   net_credit,
                'credit_ratio': credit_ratio,
                'max_loss':     max_loss,
                'sell_mid':     sell_mid,
                'wing_mid':     wing_mid,
            }

    if best_setup is None:
        return None

    if best_setup['credit_ratio'] < min_credit_ratio:
        log.info(
            f'Wing: credit ratio {best_setup["credit_ratio"]:.3f} < {min_credit_ratio:.3f} '
            f'(1/3 rule) — best available used'
        )
    if em_dollar > 0 and best_setup['wing_width'] < em_dollar:
        log.info(f'Wing ${best_setup["wing_width"]:.0f} < EM ${em_dollar:.2f} — wing narrower than EM')

    return best_setup


# ── Order Builders ─────────────────────────────────────────────────

def build_put_credit_spread(data, signal):
    best_put  = data.get('best_put')
    chain     = data.get('chain_puts', [])
    price     = data.get('price', 0.0)
    em_dollar = data.get('em_dollar', 0.0)

    if best_put is None:
        log.warning(f'{data.get("symbol")}: build_put_credit_spread — no put strike')
        return None

    wing = _find_wing(chain, best_put['strike'], 'put', price, em_dollar)
    if wing is None:
        log.info(f'{data.get("symbol")}: build_put_credit_spread — no valid wing found')
        return None

    contracts     = signal.get('contracts', 1)
    net_credit    = wing['net_credit']
    max_loss      = round(wing['max_loss'] * contracts, 2)
    profit_target = round(net_credit * PROFIT_TARGET_CREDIT, 2)
    loss_stop     = round(net_credit * LOSS_STOP_MULTIPLIER, 2)

    return {
        'strategy':       'put_credit_spread',
        'symbol':         data['symbol'],
        'expiry':         str(data['expiry']),
        'dte':            data['dte'],
        'price':          price,
        'sell_strike':    best_put['strike'],
        'buy_strike':     wing['buy_strike'],
        'wing_width':     wing['wing_width'],
        'credit':         net_credit,
        'credit_ratio':   wing['credit_ratio'],
        'contracts':      contracts,
        'max_loss':       max_loss,
        'pop':            best_put.get('pop', 0.0),
        'delta':          best_put.get('delta', 0.0),
        'iv':             best_put.get('iv', data.get('avg_iv', 30.0)),
        'em_dollar':      em_dollar,
        'em_pct':         data.get('em_pct', 0.0),
        'ivr':            data.get('ivr', 0.0),
        'ivr_regime':     data.get('ivr_regime', ''),
        'bias':           data.get('bias', 'NEUTRAL'),
        'profit_target':  profit_target,
        'loss_stop':      loss_stop,
        'dte_exit':       DTE_EXIT,
        'roll_pop_floor': ROLL_POP_FLOOR,
    }


def build_call_credit_spread(data, signal):
    best_call = data.get('best_call')
    chain     = data.get('chain_calls', [])
    price     = data.get('price', 0.0)
    em_dollar = data.get('em_dollar', 0.0)

    if best_call is None:
        log.warning(f'{data.get("symbol")}: build_call_credit_spread — no call strike')
        return None

    wing = _find_wing(chain, best_call['strike'], 'call', price, em_dollar)
    if wing is None:
        log.info(f'{data.get("symbol")}: build_call_credit_spread — no valid wing found')
        return None

    contracts     = signal.get('contracts', 1)
    net_credit    = wing['net_credit']
    max_loss      = round(wing['max_loss'] * contracts, 2)
    profit_target = round(net_credit * PROFIT_TARGET_CREDIT, 2)
    loss_stop     = round(net_credit * LOSS_STOP_MULTIPLIER, 2)

    return {
        'strategy':       'call_credit_spread',
        'symbol':         data['symbol'],
        'expiry':         str(data['expiry']),
        'dte':            data['dte'],
        'price':          price,
        'sell_strike':    best_call['strike'],
        'buy_strike':     wing['buy_strike'],
        'wing_width':     wing['wing_width'],
        'credit':         net_credit,
        'credit_ratio':   wing['credit_ratio'],
        'contracts':      contracts,
        'max_loss':       max_loss,
        'pop':            best_call.get('pop', 0.0),
        'delta':          best_call.get('delta', 0.0),
        'iv':             best_call.get('iv', data.get('avg_iv', 30.0)),
        'em_dollar':      em_dollar,
        'em_pct':         data.get('em_pct', 0.0),
        'ivr':            data.get('ivr', 0.0),
        'ivr_regime':     data.get('ivr_regime', ''),
        'bias':           data.get('bias', 'NEUTRAL'),
        'profit_target':  profit_target,
        'loss_stop':      loss_stop,
        'dte_exit':       DTE_EXIT,
        'roll_pop_floor': ROLL_POP_FLOOR,
    }


def build_iron_condor(data, signal):
    best_put    = data.get('best_put')
    best_call   = data.get('best_call')
    chain_puts  = data.get('chain_puts', [])
    chain_calls = data.get('chain_calls', [])
    price       = data.get('price', 0.0)
    em_dollar   = data.get('em_dollar', 0.0)

    if best_put is None or best_call is None:
        log.warning(f'{data.get("symbol")}: build_iron_condor — missing leg(s)')
        return None

    put_wing  = _find_wing(chain_puts,  best_put['strike'],  'put',  price, em_dollar)
    call_wing = _find_wing(chain_calls, best_call['strike'], 'call', price, em_dollar)

    if put_wing is None or call_wing is None:
        missing = 'put' if put_wing is None else 'call'
        log.info(f'{data.get("symbol")}: build_iron_condor — no {missing} wing found')
        return None

    total_credit   = round(put_wing['net_credit'] + call_wing['net_credit'], 2)
    wing_max       = max(put_wing['wing_width'], call_wing['wing_width'])
    max_loss_1ct   = round((wing_max - max(put_wing['net_credit'], call_wing['net_credit'])) * 100, 2)
    contracts      = signal.get('contracts', 1)
    max_loss       = round(max_loss_1ct * contracts, 2)
    profit_target  = round(total_credit * PROFIT_TARGET_CREDIT, 2)
    loss_stop      = round(total_credit * LOSS_STOP_MULTIPLIER, 2)
    total_width    = put_wing['wing_width'] + call_wing['wing_width']
    combined_ratio = round(total_credit / total_width, 3) if total_width > 0 else 0.0

    if combined_ratio < IC_CREDIT_RATIO:
        log.info(f'{data.get("symbol")}: IC credit ratio {combined_ratio:.3f} < {IC_CREDIT_RATIO:.3f}')

    return {
        'strategy':         'iron_condor',
        'symbol':           data['symbol'],
        'expiry':           str(data['expiry']),
        'dte':              data['dte'],
        'price':            price,
        'put_sell_strike':  best_put['strike'],
        'put_buy_strike':   put_wing['buy_strike'],
        'put_wing_width':   put_wing['wing_width'],
        'put_credit':       put_wing['net_credit'],
        'put_pop':          best_put.get('pop', 0.0),
        'put_delta':        best_put.get('delta', 0.0),
        'call_sell_strike': best_call['strike'],
        'call_buy_strike':  call_wing['buy_strike'],
        'call_wing_width':  call_wing['wing_width'],
        'call_credit':      call_wing['net_credit'],
        'call_pop':         best_call.get('pop', 0.0),
        'call_delta':       best_call.get('delta', 0.0),
        'credit':           total_credit,
        'combined_ratio':   combined_ratio,
        'contracts':        contracts,
        'max_loss':         max_loss,
        'iv':               data.get('avg_iv', 30.0),
        'em_dollar':        em_dollar,
        'em_pct':           data.get('em_pct', 0.0),
        'ivr':              data.get('ivr', 0.0),
        'ivr_regime':       data.get('ivr_regime', ''),
        'bias':             data.get('bias', 'NEUTRAL'),
        'profit_target':    profit_target,
        'loss_stop':        loss_stop,
        'dte_exit':         DTE_EXIT,
        'roll_pop_floor':   ROLL_POP_FLOOR,
    }


def build_jade_lizard(data, signal):
    best_put    = data.get('best_put')
    best_call   = data.get('best_call')
    chain_puts  = data.get('chain_puts', [])
    chain_calls = data.get('chain_calls', [])
    price       = data.get('price', 0.0)
    em_dollar   = data.get('em_dollar', 0.0)
    symbol      = data.get('symbol', '')

    if best_put is None:
        log.warning(f'{symbol}: build_jade_lizard — no put strike')
        return None

    put_wing = _find_wing(chain_puts, best_put['strike'], 'put', price, em_dollar)
    if put_wing is None:
        log.info(f'{symbol}: build_jade_lizard — no put wing, falling back to put credit spread')
        return build_put_credit_spread(data, signal)

    if best_call is None:
        log.info(f'{symbol}: build_jade_lizard — no call strike, falling back')
        return build_put_credit_spread(data, signal)

    call_wing = _find_wing(chain_calls, best_call['strike'], 'call', price, em_dollar)
    if call_wing is None:
        log.info(f'{symbol}: build_jade_lizard — no call wing, falling back')
        return build_put_credit_spread(data, signal)

    put_credit        = put_wing['net_credit']
    call_credit       = call_wing['net_credit']
    total_credit      = round(put_credit + call_credit, 2)
    call_spread_width = call_wing['wing_width']

    if total_credit >= call_spread_width * JADE_MIN_CREDIT_RATIO:
        jade_reason = (f'Jade Lizard valid: total credit ${total_credit:.2f} ≥ '
                       f'call spread ${call_spread_width:.0f} → zero upside risk')
        log.info(f'{symbol}: {jade_reason}')
    else:
        log.info(f'{symbol}: Jade Lizard degraded → put credit spread '
                 f'(credit ${total_credit:.2f} < spread ${call_spread_width:.0f})')
        return build_put_credit_spread(data, signal)

    contracts     = signal.get('contracts', 1)
    max_loss_1ct  = round((put_wing['wing_width'] - put_credit) * 100, 2)
    max_loss      = round(max_loss_1ct * contracts, 2)
    profit_target = round(total_credit * PROFIT_TARGET_CREDIT, 2)
    loss_stop     = round(total_credit * LOSS_STOP_MULTIPLIER, 2)

    return {
        'strategy':         'jade_lizard',
        'symbol':           symbol,
        'expiry':           str(data['expiry']),
        'dte':              data['dte'],
        'price':            price,
        'put_sell_strike':  best_put['strike'],
        'put_buy_strike':   put_wing['buy_strike'],
        'put_wing_width':   put_wing['wing_width'],
        'put_credit':       put_credit,
        'put_pop':          best_put.get('pop', 0.0),
        'put_delta':        best_put.get('delta', 0.0),
        'call_sell_strike': best_call['strike'],
        'call_buy_strike':  call_wing['buy_strike'],
        'call_wing_width':  call_spread_width,
        'call_credit':      call_credit,
        'call_pop':         best_call.get('pop', 0.0),
        'call_delta':       best_call.get('delta', 0.0),
        'credit':           total_credit,
        'jade_reason':      jade_reason,
        'contracts':        contracts,
        'max_loss':         max_loss,
        'iv':               data.get('avg_iv', 30.0),
        'em_dollar':        em_dollar,
        'em_pct':           data.get('em_pct', 0.0),
        'ivr':              data.get('ivr', 0.0),
        'ivr_regime':       data.get('ivr_regime', ''),
        'bias':             data.get('bias', 'NEUTRAL'),
        'profit_target':    profit_target,
        'loss_stop':        loss_stop,
        'dte_exit':         DTE_EXIT,
        'roll_pop_floor':   ROLL_POP_FLOOR,
    }


def build_debit_spread(data, signal):
    sub_type = signal.get('sub_type', 'call')
    price    = data.get('price', 0.0)
    expiry   = data.get('expiry')
    dte      = data.get('dte', 0)
    avg_iv   = data.get('avg_iv', 30.0)
    symbol   = data.get('symbol', '')
    T        = dte / 365 if dte > 0 else 0.1
    r        = 0.05

    TARGET_DEBIT_DELTA = 0.40

    if sub_type == 'call':
        chain    = data.get('chain_calls', [])
        opt_type = 'call'
    else:
        chain    = data.get('chain_puts', [])
        opt_type = 'put'

    if not chain:
        log.info(f'{symbol}: build_debit_spread/{sub_type} — no chain data')
        return None

    buy_candidates = []
    for opt in chain:
        strike = float(opt.get('strike', 0))
        gd     = opt.get('greeks', {})
        iv     = float(gd.get('mid_iv', 0) or avg_iv / 100)
        delta  = bs_delta(price, strike, T, r, iv, opt_type)
        abs_d  = abs(delta)
        if not (0.30 <= abs_d <= 0.55):
            continue
        bid = float(opt.get('bid', 0))
        ask = float(opt.get('ask', 0))
        if ask <= 0:
            continue
        oi = opt.get('open_interest')
        if oi is not None and oi < MIN_OPTION_OI:
            continue
        buy_candidates.append({
            'strike':    strike,
            'ask':       ask,
            'bid':       bid,
            'abs_delta': abs_d,
            'delta':     delta,
            'iv':        round(iv * 100, 1),
            'pop':       bs_prob_otm(price, strike, T, r, iv, opt_type),
        })

    if not buy_candidates:
        log.info(f'{symbol}: build_debit_spread/{sub_type} — no 40-delta buy candidates')
        return None

    buy_candidates.sort(key=lambda c: abs(c['abs_delta'] - TARGET_DEBIT_DELTA))
    buy_leg = buy_candidates[0]

    if price < 20:
        wing_width = 2
    elif price < 50:
        wing_width = 3
    elif price < 100:
        wing_width = 5
    else:
        wing_width = 10

    sell_target = round(buy_leg['strike'] + wing_width if sub_type == 'call'
                        else buy_leg['strike'] - wing_width, 2)
    sell_leg    = next((o for o in chain if abs(float(o.get('strike', 0)) - sell_target) < 0.01), None)

    buy_ask   = buy_leg['ask']
    sell_bid  = float(sell_leg.get('bid', 0)) if sell_leg else 0.0
    net_debit = round(buy_ask - sell_bid, 2)

    if net_debit <= 0:
        log.info(f'{symbol}: build_debit_spread — zero or negative net debit')
        return None

    contracts  = signal.get('contracts', 1)
    max_loss   = round(net_debit * 100 * contracts, 2)
    max_profit = round((wing_width - net_debit) * 100 * contracts, 2)

    return {
        'strategy':          'debit_spread',
        'sub_type':          sub_type,
        'symbol':            symbol,
        'expiry':            str(expiry),
        'dte':               dte,
        'price':             price,
        'buy_strike':        buy_leg['strike'],
        'sell_strike':       sell_target,
        'wing_width':        wing_width,
        'debit':             net_debit,
        'contracts':         contracts,
        'max_loss':          max_loss,
        'max_profit':        max_profit,
        'pop':               buy_leg.get('pop', 0.0),
        'delta':             buy_leg.get('delta', 0.0),
        'iv':                buy_leg.get('iv', avg_iv),
        'em_dollar':         data.get('em_dollar', 0.0),
        'em_pct':            data.get('em_pct', 0.0),
        'ivr':               data.get('ivr', 0.0),
        'ivr_regime':        data.get('ivr_regime', ''),
        'bias':              data.get('bias', 'NEUTRAL'),
        'profit_target_pct': PROFIT_TARGET_DEBIT,
        'loss_stop_pct':     0.50,
        'dte_exit':          DTE_EXIT,
    }


def build_calendar_spread(data, signal):
    price  = data.get('price', 0.0)
    avg_iv = data.get('avg_iv', 30.0)
    symbol = data.get('symbol', '')

    if price < 20:
        interval = 0.50
    elif price < 100:
        interval = 1.0
    else:
        interval = 5.0

    atm_strike = round(round(price / interval) * interval, 2)
    contracts  = signal.get('contracts', 1)
    est_debit  = round(price * 0.015, 2)
    max_loss   = round(est_debit * 100 * contracts, 2)

    log.info(f'{symbol}: calendar spread — ATM={atm_strike} front≈{CAL_DTE_FRONT} back≈{CAL_DTE_BACK}')

    return {
        'strategy':          'calendar_spread',
        'symbol':            symbol,
        'price':             price,
        'atm_strike':        atm_strike,
        'front_dte_target':  CAL_DTE_FRONT,
        'back_dte_target':   CAL_DTE_BACK,
        'contracts':         contracts,
        'est_debit':         est_debit,
        'max_loss':          max_loss,
        'iv':                avg_iv,
        'ivr':               data.get('ivr', 0.0),
        'ivr_regime':        data.get('ivr_regime', ''),
        'bias':              data.get('bias', 'NEUTRAL'),
        'profit_target_pct': PROFIT_TARGET_CALENDAR,
        'loss_stop_pct':     0.50,
        'dte_exit':          DTE_EXIT,
        'needs_two_expiries': True,
    }


def build_order(data, signal):
    """Dispatch to correct builder by strategy."""
    strategy = signal.get('strategy', '')
    if strategy == 'put_credit_spread':
        return build_put_credit_spread(data, signal)
    elif strategy == 'call_credit_spread':
        return build_call_credit_spread(data, signal)
    elif strategy == 'iron_condor':
        return build_iron_condor(data, signal)
    elif strategy == 'jade_lizard':
        return build_jade_lizard(data, signal)
    elif strategy == 'debit_spread':
        return build_debit_spread(data, signal)
    elif strategy == 'calendar_spread':
        return build_calendar_spread(data, signal)
    else:
        log.warning(f'build_order: unknown strategy "{strategy}"')
        return None



# ── Monitor — Value Fetchers ────────────────────────────────────────

async def _get_credit_spread_value(trade) -> float | None:
    """Current mark for a 2-leg credit spread. Returns net debit to close or None."""
    try:
        symbol      = trade['symbol']
        sell_strike = float(trade['sell_strike'] or 0)
        buy_strike  = float(trade['buy_strike']  or 0)
        expiry_str  = trade['expiry']
        strategy    = trade['strategy']
        opt_type    = 'put' if 'put' in strategy else 'call'

        val = await tt.tt_get_spread_value(symbol, expiry_str, sell_strike, buy_strike, opt_type)
        if val is None:
            return None
        sell_mid = val['sell'].get('mid') or val['sell'].get('price', 0)
        buy_mid  = val['buy'].get('mid')  or val['buy'].get('price', 0)
        if sell_mid is None or buy_mid is None:
            return None
        return round(float(sell_mid) - float(buy_mid), 4)
    except Exception as e:
        log.warning(f'Credit spread value error {trade.get("symbol")}: {str(e)[:60]}')
        return None


async def _get_ic_value(trade) -> float | None:
    """Current total mark for a 4-leg iron condor. Returns total debit to close or None."""
    from tastytrade.instruments import get_option_chain
    from datetime import date as _date
    try:
        symbol     = trade['symbol']
        expiry_str = trade['expiry']
        expiry     = _date.fromisoformat(expiry_str)

        chain = await get_option_chain(tt.TT_SESSION, symbol)
        if expiry not in chain:
            available = sorted(chain.keys())
            expiry    = min(available, key=lambda d: abs((d - expiry).days))
        options = chain[expiry]
        puts    = [o for o in options if o.option_type.value == 'P']
        calls   = [o for o in options if o.option_type.value == 'C']

        def _find(opts, strike):
            return next((o for o in opts if abs(float(o.strike_price) - strike) < 0.01), None)

        sp_opt = _find(puts,  float(trade['sell_strike_put'] or 0))
        bp_opt = _find(puts,  float(trade['buy_strike_put']  or 0))
        sc_opt = _find(calls, float(trade['sell_strike']     or 0))
        bc_opt = _find(calls, float(trade['buy_strike']      or 0))

        all_opts = [o for o in [sp_opt, bp_opt, sc_opt, bc_opt] if o]
        if len(all_opts) < 4:
            return None

        greeks = await tt.tt_get_greeks_for_options(all_opts)

        def _mid(opt):
            if opt is None:
                return 0.0
            d   = greeks.get(opt.symbol, {})
            bid = d.get('bid', 0)
            ask = d.get('ask', 0)
            p   = d.get('price', 0)
            return (bid + ask) / 2 if ask > 0 else p

        debit = (_mid(sp_opt) - _mid(bp_opt)) + (_mid(sc_opt) - _mid(bc_opt))
        return round(max(debit, 0), 4)
    except Exception as e:
        log.warning(f'IC value error {trade.get("symbol")}: {str(e)[:60]}')
        return None


async def _get_jade_lizard_value(trade) -> float | None:
    """Current total mark for a 3-leg Jade Lizard. Returns net debit to close or None."""
    from tastytrade.instruments import get_option_chain
    from datetime import date as _date
    try:
        symbol     = trade['symbol']
        expiry_str = trade['expiry']
        expiry     = _date.fromisoformat(expiry_str)

        chain = await get_option_chain(tt.TT_SESSION, symbol)
        if expiry not in chain:
            available = sorted(chain.keys())
            expiry    = min(available, key=lambda d: abs((d - expiry).days))
        options = chain[expiry]
        puts    = [o for o in options if o.option_type.value == 'P']
        calls   = [o for o in options if o.option_type.value == 'C']

        def _find(opts, strike):
            return next((o for o in opts if abs(float(o.strike_price) - strike) < 0.01), None)

        sp_opt = _find(puts,  float(trade['sell_strike_put'] or 0))
        sc_opt = _find(calls, float(trade['sell_call']       or 0))
        bc_opt = _find(calls, float(trade['buy_strike']      or 0))

        all_opts = [o for o in [sp_opt, sc_opt, bc_opt] if o]
        if len(all_opts) < 3:
            return None

        greeks = await tt.tt_get_greeks_for_options(all_opts)

        def _mid(opt):
            if opt is None:
                return 0.0
            d   = greeks.get(opt.symbol, {})
            bid = d.get('bid', 0)
            ask = d.get('ask', 0)
            p   = d.get('price', 0)
            return (bid + ask) / 2 if ask > 0 else p

        debit = _mid(sp_opt) + _mid(sc_opt) - _mid(bc_opt)
        return round(max(debit, 0), 4)
    except Exception as e:
        log.warning(f'Jade Lizard value error {trade.get("symbol")}: {str(e)[:60]}')
        return None


async def _get_debit_spread_value(trade) -> float | None:
    """Current spread mark for a debit spread. Returns credit to close or None."""
    try:
        symbol      = trade['symbol']
        sell_strike = float(trade['sell_strike'] or 0)
        buy_strike  = float(trade['buy_strike']  or 0)
        expiry_str  = trade['expiry']
        opt_type    = 'call' if 'call' in str(trade.get('direction', '')).lower() else 'put'

        val = await tt.tt_get_spread_value(symbol, expiry_str, sell_strike, buy_strike, opt_type)
        if val is None:
            return None
        sell_mid = val['sell'].get('mid') or val['sell'].get('price', 0)
        buy_mid  = val['buy'].get('mid')  or val['buy'].get('price', 0)
        if sell_mid is None or buy_mid is None:
            return None
        return round(float(buy_mid) - float(sell_mid), 4)
    except Exception as e:
        log.warning(f'Debit spread value error {trade.get("symbol")}: {str(e)[:60]}')
        return None


async def _get_calendar_spread_value(trade) -> float | None:
    """Current value for a calendar spread. Returns credit to close or None."""
    from tastytrade.instruments import get_option_chain
    from datetime import date as _date
    try:
        symbol    = trade['symbol']
        near_str  = trade['near_expiry']
        far_str   = trade['far_expiry']
        strike    = float(trade['sell_strike'] or 0)
        opt_type  = 'C' if 'call' in str(trade.get('direction', '')).lower() else 'P'

        if not near_str or not far_str:
            return None

        near_expiry = _date.fromisoformat(near_str)
        far_expiry  = _date.fromisoformat(far_str)

        chain = await get_option_chain(tt.TT_SESSION, symbol)

        def _resolve(exp):
            if exp not in chain:
                avail = sorted(chain.keys())
                exp   = min(avail, key=lambda d: abs((d - exp).days))
            return exp

        near_expiry = _resolve(near_expiry)
        far_expiry  = _resolve(far_expiry)

        def _find_at(expiry, strike_target):
            opts = [o for o in chain[expiry] if o.option_type.value == opt_type]
            return next((o for o in opts if abs(float(o.strike_price) - strike_target) < 0.01), None)

        near_opt = _find_at(near_expiry, strike)
        far_opt  = _find_at(far_expiry,  strike)
        if not near_opt or not far_opt:
            return None

        greeks = await tt.tt_get_greeks_for_options([near_opt, far_opt])

        def _mid(opt):
            d   = greeks.get(opt.symbol, {})
            bid = d.get('bid', 0)
            ask = d.get('ask', 0)
            p   = d.get('price', 0)
            return (bid + ask) / 2 if ask > 0 else p

        val = _mid(far_opt) - _mid(near_opt)
        return round(max(val, 0), 4)
    except Exception as e:
        log.warning(f'Calendar spread value error {trade.get("symbol")}: {str(e)[:60]}')
        return None


async def _get_spot(symbol: str) -> float | None:
    try:
        spots = await tt.tt_get_spot_batch([symbol])
        return spots.get(symbol)
    except Exception:
        return None


# ── Monitor — Close Execution ───────────────────────────────────────

async def _close_trade(trade, reason: str, pnl: float,
                       hit_target: int = 0, was_stopped: int = 0,
                       exit_spot=None, current_value=None,
                       dry_run: bool = False) -> bool:
    """Route to the correct TT close function. Updates DB on success."""
    strategy    = trade['strategy']
    symbol      = trade['symbol']
    contracts   = int(trade['contracts'] or 1)
    expiry      = date.fromisoformat(trade['expiry']) if trade.get('expiry') else None
    credit      = float(trade['credit_debit'] or 0)
    dte_at_close = (expiry - date.today()).days if expiry else None

    result = {'error': 'strategy not matched', 'status': 'FAILED'}

    try:
        if strategy in ('put_credit_spread', 'call_credit_spread'):
            opt_type    = 'put' if 'put' in strategy else 'call'
            sell_strike = float(trade['sell_strike'] or 0)
            buy_strike  = float(trade['buy_strike']  or 0)
            result = await tt.tt_close_position(
                symbol=symbol, expiry=expiry,
                sell_strike=sell_strike, buy_strike=buy_strike,
                option_type=opt_type, debit=round(current_value or 0, 2),
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'iron_condor':
            result = await tt.tt_close_iron_condor(
                symbol=symbol, expiry=expiry,
                sell_put=float(trade['sell_strike_put'] or 0),
                buy_put=float(trade['buy_strike_put']   or 0),
                sell_call=float(trade['sell_strike']    or 0),
                buy_call=float(trade['buy_strike']      or 0),
                debit=round(current_value or 0, 2),
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'jade_lizard':
            result = await tt.tt_close_jade_lizard(
                symbol=symbol, expiry=expiry,
                sell_put=float(trade['sell_strike_put'] or 0),
                sell_call=float(trade['sell_call']      or 0),
                buy_call=float(trade['buy_strike']      or 0),
                debit=round(current_value or 0, 2),
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'debit_spread':
            result = await tt.tt_close_debit_spread(
                symbol=symbol, expiry=expiry,
                buy_strike=float(trade['buy_strike']   or 0),
                sell_strike=float(trade['sell_strike'] or 0),
                option_type=str(trade.get('direction', 'call')).lower(),
                credit=round(current_value or 0, 2),
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'calendar_spread':
            near_expiry = date.fromisoformat(trade['near_expiry']) if trade.get('near_expiry') else expiry
            far_expiry  = date.fromisoformat(trade['far_expiry'])  if trade.get('far_expiry')  else expiry
            result = await tt.tt_close_calendar_spread(
                symbol=symbol,
                near_expiry=near_expiry, far_expiry=far_expiry,
                strike=float(trade['sell_strike'] or 0),
                option_type=str(trade.get('direction', 'call')).lower(),
                credit=round(current_value or 0, 2),
                contracts=contracts, dry_run=dry_run,
            )

        else:
            log.warning(f'Unknown strategy for close: {strategy} ({symbol})')
            return False

    except Exception as e:
        log.error(f'Close execution error {symbol} {strategy}: {e}')
        return False

    if result.get('error') or result.get('status') == 'FAILED':
        log.error(f'Close order failed: {symbol} {strategy} — {result.get("error")}')
        return False

    cv = float(current_value or 0)
    cr = float(credit)
    is_debit = 'debit' in strategy or 'calendar' in strategy
    if is_debit:
        gross_pnl = round((cv - abs(cr)) * 100 * contracts, 2)
    else:
        gross_pnl = round((abs(cr) - cv) * 100 * contracts, 2)

    db.close_trade_db(
        trade_id=trade['id'],
        reason=reason,
        pnl=gross_pnl,
        hit_target=hit_target,
        was_stopped=was_stopped,
        exit_spot=exit_spot,
        exit_value=current_value,
        dte_at_close=dte_at_close,
    )
    log.info(f'Closed: {symbol} {strategy} id={trade["id"]} reason={reason} '
             f'pnl=${gross_pnl:.2f} cv={cv:.4f} credit={cr:.4f}')
    return True


# ── Monitor — Main Loop ─────────────────────────────────────────────

async def _monitor_one(trade, vix=None, dry_run: bool = False) -> dict:
    """Check and manage a single open trade."""
    result = {
        'id':            trade['id'],
        'symbol':        trade['symbol'],
        'strategy':      trade['strategy'],
        'action':        'hold',
        'reason':        '',
        'value':         None,
        'pnl_approx':    None,
        'pop':           None,
        'dte_remaining': None,
        'alert':         False,
        'credit_debit':  float(trade.get('credit_debit') or 0),
        'contracts':     int(trade.get('contracts') or 1),
    }

    symbol    = trade['symbol']
    strategy  = trade['strategy']
    credit    = float(trade['credit_debit']  or 0)
    contracts = int(trade['contracts']       or 1)
    is_debit  = 'debit' in strategy or 'calendar' in strategy
    is_calendar = strategy == 'calendar_spread'

    today  = date.today()
    expiry = None
    if is_calendar and trade.get('near_expiry'):
        expiry = date.fromisoformat(trade['near_expiry'])
    elif trade.get('expiry'):
        expiry = date.fromisoformat(trade['expiry'])

    dte_remaining = (expiry - today).days if expiry else None
    result['dte_remaining'] = dte_remaining

    current_value = None
    try:
        if strategy in ('put_credit_spread', 'call_credit_spread'):
            current_value = await _get_credit_spread_value(trade)
        elif strategy == 'iron_condor':
            current_value = await _get_ic_value(trade)
        elif strategy == 'jade_lizard':
            current_value = await _get_jade_lizard_value(trade)
        elif strategy == 'debit_spread':
            current_value = await _get_debit_spread_value(trade)
        elif strategy == 'calendar_spread':
            current_value = await _get_calendar_spread_value(trade)
    except Exception as e:
        log.warning(f'Value fetch failed for {symbol} id={trade["id"]}: {str(e)[:80]}')

    result['value'] = current_value

    if current_value is not None:
        db.update_trade_last_spread(trade['id'], current_value)
        cv = float(current_value)
        if is_debit:
            pnl_approx = round((cv - abs(credit)) * 100 * contracts, 2)
        else:
            pnl_approx = round((abs(credit) - cv) * 100 * contracts, 2)
        result['pnl_approx'] = pnl_approx

    spot = None
    try:
        spot = await _get_spot(symbol)
        if spot:
            db.update_trade_last_spot(trade['id'], spot)
    except Exception:
        spot = trade.get('last_spot_price') or float(trade.get('spot_price') or 0) or None

    result['spot']  = spot
    result['trade'] = dict(trade)

    # DTE EXIT (21 DTE)
    dte_exit = int(DTE_EXIT)
    if dte_remaining is not None and dte_remaining <= dte_exit:
        if current_value is not None:
            closed = await _close_trade(
                trade, reason='dte_exit', pnl=result.get('pnl_approx', 0),
                exit_spot=spot, current_value=current_value, dry_run=dry_run,
            )
            if closed:
                result['action'] = 'closed'
                result['reason'] = f'DTE exit ({dte_remaining} DTE ≤ {dte_exit})'
                return result
        else:
            result['action'] = 'alert'
            result['reason'] = f'DTE {dte_remaining} ≤ {dte_exit} — value unavailable, manual action needed'
            result['alert']  = True
            return result

    if current_value is None:
        result['reason'] = 'value unavailable — hold'
        return result

    cv = float(current_value)
    cr = abs(float(credit))

    if not is_debit:
        profit_target = round(cr * (1 - PROFIT_TARGET_CREDIT), 4)
        hard_stop     = round(cr * LOSS_STOP_MULTIPLIER, 4)

        if cv <= profit_target:
            closed = await _close_trade(
                trade, reason='profit_target', pnl=result['pnl_approx'] or 0,
                hit_target=1, exit_spot=spot, current_value=cv, dry_run=dry_run,
            )
            if closed:
                result['action'] = 'closed'
                result['reason'] = f'50% profit target (credit={cr:.2f} → value={cv:.2f})'
                return result

        elif cv >= hard_stop:
            closed = await _close_trade(
                trade, reason='hard_stop', pnl=result['pnl_approx'] or 0,
                was_stopped=1, exit_spot=spot, current_value=cv, dry_run=dry_run,
            )
            if closed:
                result['action'] = 'closed'
                result['reason'] = f'2× hard stop (credit={cr:.2f} → value={cv:.2f})'
                return result

        # POP alert
        try:
            if spot and spot > 0 and trade.get('expiry') and dte_remaining:
                T  = dte_remaining / 365
                r  = 0.05
                iv = float(trade.get('entry_iv') or 0)
                if iv > 0:
                    if strategy == 'iron_condor':
                        sp  = float(trade['sell_strike_put'] or 0)
                        sc  = float(trade['sell_strike']     or 0)
                        pop = min(bs_prob_otm(spot, sp, T, r, iv / 100, 'put'),
                                  bs_prob_otm(spot, sc, T, r, iv / 100, 'call'))
                    elif strategy == 'jade_lizard':
                        sp  = float(trade['sell_strike_put'] or 0)
                        pop = bs_prob_otm(spot, sp, T, r, iv / 100, 'put')
                    else:
                        sell_strike = float(trade['sell_strike'] or 0)
                        opt_type    = 'put' if 'put' in strategy else 'call'
                        pop = bs_prob_otm(spot, sell_strike, T, r, iv / 100, opt_type)
                    result['pop'] = round(pop, 1)
                    if pop < ROLL_POP_FLOOR * 100:
                        result['action'] = 'alert'
                        result['reason'] = (f'POP alert: {pop:.1f}% < {ROLL_POP_FLOOR*100:.0f}% '
                                            f'(credit={cr:.2f} value={cv:.2f} dte={dte_remaining})')
                        result['alert'] = True
        except Exception as e:
            log.debug(f'POP check error {symbol}: {str(e)[:50]}')

    else:
        debit_paid    = abs(cr)
        dte_exit_cal  = 5 if is_calendar else dte_exit

        if is_calendar:
            profit_target = round(debit_paid * (1 + PROFIT_TARGET_CALENDAR), 4)
        else:
            profit_target = round(debit_paid * (1 + PROFIT_TARGET_DEBIT), 4)
        hard_stop = round(debit_paid * (1 - DEBIT_HARD_STOP_PCT), 4)

        if cv >= profit_target:
            closed = await _close_trade(
                trade, reason='profit_target', pnl=result['pnl_approx'] or 0,
                hit_target=1, exit_spot=spot, current_value=cv, dry_run=dry_run,
            )
            if closed:
                pct = (PROFIT_TARGET_CALENDAR if is_calendar else PROFIT_TARGET_DEBIT) * 100
                result['action'] = 'closed'
                result['reason'] = f'{pct:.0f}% debit profit target (paid={debit_paid:.2f} → value={cv:.2f})'
                return result

        elif cv <= hard_stop:
            closed = await _close_trade(
                trade, reason='hard_stop', pnl=result['pnl_approx'] or 0,
                was_stopped=1, exit_spot=spot, current_value=cv, dry_run=dry_run,
            )
            if closed:
                result['action'] = 'closed'
                result['reason'] = f'50% debit hard stop (paid={debit_paid:.2f} → value={cv:.2f})'
                return result

        if is_calendar and dte_remaining is not None and dte_remaining <= dte_exit_cal:
            closed = await _close_trade(
                trade, reason='calendar_dte_exit', pnl=result['pnl_approx'] or 0,
                exit_spot=spot, current_value=cv, dry_run=dry_run,
            )
            if closed:
                result['action'] = 'closed'
                result['reason'] = f'Calendar near-month DTE exit ({dte_remaining} ≤ {dte_exit_cal})'
                return result

    return result


async def monitor_positions(send_telegram=None, dry_run: bool = False):
    """Main monitor entry point. Called every 30 min by APScheduler."""
    now = datetime.now(ET)
    is_weekday  = now.weekday() < 5
    market_hour = (9 <= now.hour < 16) or (now.hour == 16 and now.minute == 0)

    if not is_weekday:
        log.debug('Monitor: weekend — skipping')
        return []

    trades = db.get_open_trades()
    if not trades:
        log.info('Monitor: no open positions')
        return []

    log.info(f'Monitor: checking {len(trades)} open trade(s) '
             f'[market_hours={market_hour} dry_run={dry_run}]')

    vix = None
    try:
        vix = await tt.tt_get_vix()
    except Exception as e:
        log.warning(f'Monitor VIX fetch failed: {str(e)[:50]}')

    results = []
    for t in trades:
        trade = dict(t)
        if trade.get('manual_mgmt'):
            log.info(f'Monitor: skip id={trade["id"]} {trade["symbol"]} — manual_mgmt flag set')
            continue

        effective_dry_run = dry_run or (not market_hour)
        result = await _monitor_one(trade, vix=vix, dry_run=effective_dry_run)
        results.append(result)

        if result['action'] in ('closed', 'alert') and send_telegram:
            symbol   = result['symbol']
            strategy = result['strategy']
            reason   = result['reason']
            value    = result.get('value')
            pnl_a    = result.get('pnl_approx')
            pop      = result.get('pop')
            dte      = result.get('dte_remaining')

            if result['action'] == 'closed':
                emoji = '✅' if 'profit' in reason else '🛑'
                msg = (
                    f"{emoji} CLOSED: *{symbol}* `{strategy}`\n"
                    f"Reason: {reason}\n"
                    f"Value: ${value:.4f} | P&L ≈ ${pnl_a:+.2f}"
                    + (f" | DTE: {dte}" if dte is not None else "")
                    + ('\n_(dry_run — not executed on TT)_' if effective_dry_run else '')
                )
            else:
                msg = (
                    f"⚠️ ALERT: *{symbol}* `{strategy}`\n"
                    f"{reason}"
                    + (f" | POP: {pop:.1f}%" if pop else "")
                    + (f" | DTE: {dte}" if dte is not None else "")
                    + (f" | Value: ${value:.4f}" if value is not None else "")
                    + (f" | P&L ≈ ${pnl_a:+.2f}" if pnl_a is not None else "")
                )

            try:
                await send_telegram(msg)
            except Exception as e:
                log.warning(f'Monitor TG send failed: {str(e)[:50]}')

    closed_count = sum(1 for r in results if r['action'] == 'closed')
    alert_count  = sum(1 for r in results if r['action'] == 'alert')
    log.info(f'Monitor complete: {closed_count} closed, {alert_count} alerts, '
             f'{len(results) - closed_count - alert_count} holds')
    return results


# ── Telegram ────────────────────────────────────────────────────────

_tg_bot: Bot = None


async def tg(message: str):
    """Send a Telegram message. Silently swallows failures."""
    global _tg_bot
    try:
        if _tg_bot is None:
            _tg_bot = Bot(token=TG_TOKEN)
        await _tg_bot.send_message(chat_id=TG_CHAT, text=message, parse_mode='Markdown')
    except Exception as e:
        log.warning(f'TG send failed: {str(e)[:60]}')


async def tg_html(message: str):
    """Send a Telegram HTML message. Silently swallows failures."""
    global _tg_bot
    try:
        if _tg_bot is None:
            _tg_bot = Bot(token=TG_TOKEN)
        await _tg_bot.send_message(chat_id=TG_CHAT, text=message, parse_mode='HTML')
    except Exception as e:
        log.warning(f'TG send failed: {str(e)[:60]}')


def _authed(update: Update) -> bool:
    return str(update.effective_chat.id) == str(TG_CHAT)


async def _deny(update: Update):
    await update.message.reply_text('Unauthorized.')


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    await update.message.reply_text(
        '*Kubera online.* Type /help for command list.',
        parse_mode='Markdown'
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    await update.message.reply_text(
        'KUBERA COMMANDS\n\n'
        'TRADING\n'
        '/scan      — trigger manual scan\n'
        '/monitor   — run monitor cycle now\n\n'
        'POSITIONS\n'
        '/positions — open positions + P&L\n'
        '/history   — closed trade summary\n\n'
        'ACCOUNT\n'
        '/status    — bot state + guardrails\n'
        '/balance   — fetch live TT NLV\n'
        '/pause     — halt new entries\n'
        '/resume    — re-enable entries\n'
        '/mode      — show or set mode (paper/live)\n\n'
        'EXIT RULES\n'
        '25% profit  → auto close\n'
        '50% loss    → hard stop\n'
        '21 DTE      → time exit\n'
        '2 con. losses → 24h pause\n'
        '10% weekly drawdown → weekly pause'
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)

    balance = db.get_current_balance()
    mode    = db.get_state('mode') or 'paper'
    paused  = db.get_state('paused') == 'true'
    weekly  = db.get_state('weekly_pause') == 'true'
    monthly = db.get_state('monthly_pause') == 'true'
    losses  = int(db.get_state('consecutive_losses') or 0)
    wins    = int(db.get_state('consecutive_wins')   or 0)
    peak    = db.get_state('balance_peak') or '—'

    can_trade, guard_reason = db.check_guardrails()
    open_count  = db.get_open_trade_count()
    deployed    = db.get_deployed_capital()
    deployed_pct = round(deployed / balance * 100, 1) if balance > 0 else 0

    status_icon = '🟢' if can_trade else '🔴'
    msg = (
        f'{status_icon} *Kubera Status*\n\n'
        f'Mode: `{mode}` | Balance: `${balance:,.2f}`\n'
        f'Peak: `${peak}` | Kill Switch: `${KILL_SWITCH:,.0f}`\n'
        f'Positions: `{open_count}/{MAX_POSITIONS}` | Deployed: `{deployed_pct}%`\n\n'
        f'Paused: `{paused}` | Weekly: `{weekly}` | Monthly: `{monthly}`\n'
        f'Streak: `+{wins}W / {losses}L`\n\n'
    )
    if not can_trade:
        msg += f'⛔ *{guard_reason}*'
    else:
        msg += '✅ Ready to trade'

    await update.message.reply_text(msg, parse_mode='Markdown')


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)

    trades = db.get_open_trades()
    if not trades:
        await update.message.reply_text('No open positions.')
        return

    _strat_label = {
        'iron_condor':        'Iron Condor',
        'put_credit_spread':  'Put Credit Spread',
        'call_credit_spread': 'Call Credit Spread',
        'jade_lizard':        'Jade Lizard',
        'debit_spread':       'Debit Spread',
        'calendar_spread':    'Calendar Spread',
    }

    today = date.today()
    lines = [f'<b>Open Positions ({len(trades)})</b>']

    for t in trades:
        t = dict(t)
        strategy = t.get('strategy', '?')
        symbol   = t.get('symbol', '?')
        strat    = _strat_label.get(strategy, strategy)
        cts      = int(t.get('contracts') or 1)

        # DTE
        expiry_s = t.get('expiry') or t.get('near_expiry') or t.get('far_expiry', '?')
        try:
            dte = (date.fromisoformat(expiry_s) - today).days
        except Exception:
            dte = '?'

        # Credit / debit at entry
        cr       = float(t.get('credit_debit') or 0)
        is_debit = cr < 0
        entry    = abs(cr)

        # Current value and P&L
        cur_val  = t.get('last_spread_value')
        if cur_val is not None:
            cur_val = float(cur_val)
            if is_debit:
                pnl_per = cur_val - entry          # debit: profit = value rose
            else:
                pnl_per = entry - cur_val          # credit: profit = value fell
            pnl_usd = round(pnl_per * 100 * cts, 2)
            pnl_pct = round(pnl_per / entry * 100, 1) if entry else 0
            pnl_sign = '+' if pnl_usd >= 0 else ''
            pnl_str  = f'{pnl_sign}${pnl_usd:.2f} ({pnl_sign}{pnl_pct:.1f}%)'
            val_str  = f'${cur_val:.2f}'
        else:
            pnl_str = 'n/a'
            val_str = 'n/a'

        # Spot price (from last monitor cycle)
        spot = t.get('last_spot_price') or t.get('spot_price')
        spot_str = f'${float(spot):.2f}' if spot else 'n/a'

        # Strike structure + distance to short strike
        sell_put  = t.get('sell_strike_put')
        buy_put   = t.get('buy_strike_put')
        sell_call = t.get('sell_call') or t.get('sell_strike')
        buy_call  = t.get('buy_strike')

        if strategy == 'iron_condor' and sell_put and sell_call:
            strikes_str = f'P {buy_put:.0f}/{sell_put:.0f} | C {sell_call:.0f}/{buy_call:.0f}'
            if spot:
                s = float(spot)
                dist_put  = round(s - float(sell_put),  2)
                dist_call = round(float(sell_call) - s, 2)
                dist_str  = f'dist: {dist_put:+.2f} to put short, {dist_call:+.2f} to call short'
            else:
                dist_str = ''
        elif strategy in ('call_credit_spread',) and sell_call:
            strikes_str = f'C {sell_call:.0f}/{buy_call:.0f}'
            if spot:
                dist_str = f'dist: {round(float(sell_call) - float(spot), 2):+.2f} to short strike'
            else:
                dist_str = ''
        elif strategy in ('put_credit_spread', 'jade_lizard') and t.get('sell_strike'):
            sp = float(t['sell_strike'])
            bp = float(t.get('buy_strike') or 0)
            strikes_str = f'P {bp:.0f}/{sp:.0f}'
            if sell_call:
                strikes_str += f' | C {sell_call:.0f}'
            if spot:
                dist_str = f'dist: {round(float(spot) - sp, 2):+.2f} to short strike'
            else:
                dist_str = ''
        elif strategy == 'calendar_spread':
            atm = t.get('sell_strike') or t.get('buy_strike')
            strikes_str = f'ATM {atm:.0f}' if atm else ''
            dist_str = ''
        else:
            strikes_str = ''
            dist_str = ''

        # TP / SL — show close value and resulting dollar P&L at each level
        tp = t.get('target_value')
        sl = t.get('stop_value')
        if tp and sl and entry:
            tp_profit = round((entry - float(tp)) * 100 * cts, 2) if not is_debit else round((float(tp) - entry) * 100 * cts, 2)
            sl_loss   = round((entry - float(sl)) * 100 * cts, 2) if not is_debit else round((float(sl) - entry) * 100 * cts, 2)
            tp_sl = f'TP: +${tp_profit:.0f} | SL: ${sl_loss:.0f}'
        else:
            tp_sl = ''

        # Assemble block (HTML — no markdown italic issues)
        block  = f'\n<b>#{t["id"]} {symbol} — {strat} x{cts}</b>\n'
        block += f'Spot: {spot_str} | DTE: {dte} | IVR: {t.get("ivr","?")} | Bias: {t.get("bias","?")}\n'
        if strikes_str:
            block += f'Strikes: {strikes_str}\n'
        if dist_str:
            block += f'{dist_str}\n'
        cr_label = 'Debit' if is_debit else 'Credit'
        block += f'{cr_label}: ${entry:.2f} | Val: {val_str} | P&L: {pnl_str}\n'
        if tp_sl:
            block += tp_sl

        lines.append(block)

    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    await update.message.reply_text('Fetching balance from TastyTrade...')
    try:
        await sync_balance()
        balance = db.get_current_balance()
        await update.message.reply_text(f'💰 Balance: `${balance:,.2f}`', parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f'Balance fetch failed: {str(e)[:80]}')


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    await update.message.reply_text('🔍 Triggering manual scan...')
    try:
        await run_scan()
    except Exception as e:
        await update.message.reply_text(f'Scan error: {str(e)[:80]}')


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    db.set_state('paused', 'true')
    await update.message.reply_text('⏸ *Paused* — new entries halted. Monitor continues.', parse_mode='Markdown')


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    db.set_state('paused', 'false')
    can_trade, reason = db.check_guardrails()
    if can_trade:
        await update.message.reply_text('▶️ *Resumed* — new entries enabled.', parse_mode='Markdown')
    else:
        await update.message.reply_text(
            f'▶️ Pause cleared, but still blocked:\n`{reason}`', parse_mode='Markdown'
        )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    args = context.args
    if not args:
        current = db.get_state('mode') or 'paper'
        await update.message.reply_text(f'Current mode: `{current}`\nUsage: /mode paper|live', parse_mode='Markdown')
        return
    new_mode = args[0].lower().strip()
    if new_mode not in ('paper', 'live'):
        await update.message.reply_text('Invalid mode. Use: /mode paper or /mode live')
        return
    db.set_state('mode', new_mode)
    icon = '📝' if new_mode == 'paper' else '💹'
    await update.message.reply_text(
        f'{icon} Mode set to `{new_mode}`.', parse_mode='Markdown'
    )


def _fmt_scan_trade(t: dict) -> str:
    """Format one newly-placed trade for the post-scan new-trades block (HTML)."""
    from datetime import date as _date
    symbol    = t.get('symbol', '?')
    strategy  = t.get('strategy', '')
    contracts = int(t.get('contracts') or 1)
    cr        = float(t.get('credit_debit') or 0)   # negative = debit
    max_loss  = float(t.get('max_loss') or 0)
    spot      = float(t.get('spot_price') or 0)
    mode_s    = t.get('mode') or 'paper'

    strat_labels = {
        'iron_condor':       'Iron Condor',
        'put_credit_spread': 'Put Spread',
        'call_credit_spread':'Call Spread',
        'jade_lizard':       'Jade Lizard',
        'debit_spread':      'Debit Spread',
        'calendar_spread':   'Calendar',
    }
    strat_label = strat_labels.get(strategy, strategy)

    def _d(s):
        try:
            return _date.fromisoformat(s).strftime('%-d %b')
        except Exception:
            return s or '?'

    is_debit = cr < 0
    cr_label = f'Debit ${abs(cr):.2f}' if is_debit else f'Credit ${cr:.2f}'
    loss_label = f'Max loss ${abs(max_loss):.0f}' if max_loss else ''

    if strategy == 'calendar_spread':
        near_s = _d(t.get('near_expiry') or '')
        far_s  = _d(t.get('far_expiry')  or '')
        spot_s = f'${spot:.0f}' if spot else ''
        detail = f'ATM {spot_s}  ·  {near_s} → {far_s}'
    elif strategy == 'iron_condor':
        sp = t.get('sell_strike_put') or 0
        bp = t.get('buy_strike_put')  or 0
        sc = t.get('sell_call')       or 0
        bc = t.get('buy_strike')      or 0
        detail = (f'{bp:.0f}P / <b>{sp:.0f}P</b>  ·  '
                  f'<b>{sc:.0f}C</b> / {bc:.0f}C')
    elif strategy == 'jade_lizard':
        sp = t.get('sell_strike_put') or t.get('sell_strike') or 0
        sc = t.get('sell_call') or 0
        bc = t.get('buy_strike') or 0
        detail = f'Put <b>{sp:.0f}P</b>  ·  Calls <b>{sc:.0f}C</b>/{bc:.0f}C'
    else:
        ss  = t.get('sell_strike') or t.get('sell_call') or 0
        bs  = t.get('buy_strike') or 0
        exp = _d(t.get('expiry') or '')
        opt = 'C' if 'call' in strategy else 'P'
        detail = f'<b>{ss:.0f}{opt}</b> / {bs:.0f}{opt}  ·  {exp}'

    mode_tag = '  <i>[paper]</i>' if mode_s == 'paper' else ''
    return (f'<b>{symbol}</b>  {strat_label} ×{contracts}{mode_tag}\n'
            f'  {detail}\n'
            f'  {cr_label}  ·  {loss_label}')


def _fmt_legs(trade: dict, strategy: str) -> str:
    """Format strike legs showing short/long direction explicitly."""
    sp   = trade.get('sell_strike_put')
    bp   = trade.get('buy_strike_put')
    sc   = trade.get('sell_strike') or trade.get('sell_call')
    bc   = trade.get('buy_strike')
    near = trade.get('near_expiry', '')
    far  = trade.get('far_expiry',  '')

    def _s(v): return f'{float(v):.2f}'.rstrip('0').rstrip('.')

    if strategy == 'iron_condor':
        parts = []
        if bp and sp: parts.append(f'Short {_s(sp)}P / Long {_s(bp)}P')
        if sc and bc: parts.append(f'Short {_s(sc)}C / Long {_s(bc)}C')
        return '  |  '.join(parts)
    elif strategy == 'put_credit_spread':
        if sp and bp: return f'Short {_s(sp)}P / Long {_s(bp)}P'
        if sp:        return f'Short {_s(sp)}P'
    elif strategy == 'call_credit_spread':
        if sc and bc: return f'Short {_s(sc)}C / Long {_s(bc)}C'
        if sc:        return f'Short {_s(sc)}C'
    elif strategy == 'calendar_spread':
        strike = sc or bc
        try:
            near_s = date.fromisoformat(near).strftime('%b %d') if near else '?'
            far_s  = date.fromisoformat(far).strftime('%b %d')  if far  else '?'
        except Exception:
            near_s, far_s = near or '?', far or '?'
        if strike:
            return f'{_s(strike)}C  |  short {near_s} / long {far_s}'
        return f'short {near_s} / long {far_s}'
    elif strategy == 'debit_spread':
        direction = str(trade.get('direction', '')).lower()
        opt = 'C' if 'call' in direction else 'P'
        if bc and sc: return f'Long {_s(bc)}{opt} / Short {_s(sc)}{opt}'
    elif strategy == 'jade_lizard':
        parts = []
        if sp:        parts.append(f'Short {_s(sp)}P')
        if sc and bc: parts.append(f'Short {_s(sc)}C / Long {_s(bc)}C')
        return '  |  '.join(parts)
    return ''


def _fmt_position_block(r: dict) -> str:
    """Build a full status block for one position from _monitor_one result."""
    trade       = r.get('trade', {})
    symbol      = r['symbol']
    strategy    = r['strategy']
    action      = r['action']
    is_debit    = 'debit' in strategy or 'calendar' in strategy
    is_calendar = 'calendar' in strategy

    strat_label = {
        'iron_condor':        'Iron Condor',
        'put_credit_spread':  'Put Credit Spread',
        'call_credit_spread': 'Call Credit Spread',
        'calendar_spread':    'Calendar Spread',
        'debit_spread':       'Debit Spread',
        'jade_lizard':        'Jade Lizard',
    }.get(strategy, strategy.replace('_', ' ').title())

    action_icon = {'closed': '✅', 'alert': '⚠️', 'hold': '🔵'}.get(action, '•')

    # ── Expiry / DTE ──────────────────────────────────────
    dte_now  = r.get('dte_remaining')
    dte_open = trade.get('dte_at_open')
    expiry_s = trade.get('near_expiry') or trade.get('expiry') or ''
    try:
        exp_fmt = date.fromisoformat(expiry_s).strftime('%b %d') if expiry_s else '?'
    except Exception:
        exp_fmt = expiry_s or '?'
    dte_str = f'DTE {dte_now}' if dte_now is not None else 'DTE ?'
    if dte_open:
        dte_str += f' (entered {dte_open})'

    # ── Entry economics ────────────────────────────────────
    credit    = abs(float(trade.get('credit_debit') or 0))
    contracts = int(trade.get('contracts') or 1)
    capital   = float(trade.get('capital_used') or (credit * 100 * contracts))
    entry_lbl = 'paid' if is_debit else 'cr'

    # ── Current value / P&L ───────────────────────────────
    value = r.get('value')
    pnl   = r.get('pnl_approx')
    val_s = f'${value:.2f}' if value is not None else 'no data'
    if pnl is not None:
        pnl_pct = (pnl / capital * 100) if capital else 0
        sign    = '+' if pnl >= 0 else ''
        pnl_s   = f'{sign}${pnl:.0f} ({sign}{pnl_pct:.1f}%)'
    else:
        pnl_s = 'N/A'

    # ── Proximity-to-stop warning ─────────────────────────
    warn_stop = ''
    if value is not None and credit > 0:
        if not is_debit:
            sl_check = credit * LOSS_STOP_MULTIPLIER
            pct = (value - credit) / (sl_check - credit) if sl_check > credit else 0
        else:
            sl_check = credit * (1 - DEBIT_HARD_STOP_PCT)
            pct = (credit - value) / (credit - sl_check) if credit > sl_check else 0
        if pct >= 0.75:
            warn_stop = '  ⚠️'

    # ── Spot + distance to short strikes ──────────────────
    spot_now   = r.get('spot')
    spot_entry = float(trade.get('spot_price') or 0)
    if spot_entry and spot_now and abs(spot_now - spot_entry) > 0.01:
        spot_s = f'${spot_entry:.2f} → ${spot_now:.2f}'
    elif spot_now:
        spot_s = f'${spot_now:.2f}'
    elif spot_entry:
        spot_s = f'${spot_entry:.2f} (entry)'
    else:
        spot_s = '?'

    dist_str = ''
    if spot_now:
        s      = float(spot_now)
        sc_val = float(trade.get('sell_strike') or trade.get('sell_call') or 0)
        sp_val = float(trade.get('sell_strike_put') or 0)
        thresh = s * 0.03  # warn when within 3% of spot

        if strategy == 'iron_condor' and sp_val and sc_val:
            d_put  = s - sp_val
            d_call = sc_val - s
            f_put  = ' ⚠️' if d_put  < thresh else ''
            f_call = ' ⚠️' if d_call < thresh else ''
            dist_str = f'+${d_put:.2f} to put{f_put}  |  +${d_call:.2f} to call{f_call}'
        elif strategy == 'call_credit_spread' and sc_val:
            d_call = sc_val - s
            flag   = ' ⚠️' if d_call < thresh else ''
            dist_str = f'+${d_call:.2f} to short call{flag}'
        elif strategy in ('put_credit_spread', 'jade_lizard') and sp_val:
            d_put = s - sp_val
            flag  = ' ⚠️' if d_put < thresh else ''
            dist_str = f'+${d_put:.2f} to short put{flag}'

    # ── TP / SL — computed fresh from entry credit ────────
    if is_debit:
        pt_pct  = PROFIT_TARGET_CALENDAR if is_calendar else PROFIT_TARGET_DEBIT
        tp_val  = round(credit * (1 + pt_pct), 2)
        sl_val  = round(credit * (1 - DEBIT_HARD_STOP_PCT), 2)
        tp_pnl  = round((tp_val - credit) * 100 * contracts)
        sl_pnl  = round((credit - sl_val) * 100 * contracts)
        tp_line = f'TP >${tp_val:.2f} (+${tp_pnl})'
        sl_line = f'SL <${sl_val:.2f} (-${sl_pnl})'
    else:
        tp_val  = round(credit * (1 - PROFIT_TARGET_CREDIT), 2)
        sl_val  = round(credit * LOSS_STOP_MULTIPLIER, 2)
        tp_pnl  = round((credit - tp_val) * 100 * contracts)
        sl_pnl  = round((sl_val - credit) * 100 * contracts)
        tp_line = f'TP <${tp_val:.2f} (+${tp_pnl})'
        sl_line = f'SL >${sl_val:.2f} (-${sl_pnl})'

    # ── Entry context ─────────────────────────────────────
    ivr  = trade.get('ivr')
    iv   = trade.get('avg_iv') or trade.get('entry_iv')
    pop  = r.get('pop')
    bias = trade.get('bias') or ''
    opened_at = (trade.get('opened_at') or '')[:10]

    legs = _fmt_legs(trade, strategy)

    lines = [f'{action_icon} *{symbol}* — {strat_label}']
    lines.append(f'Exp: {exp_fmt}  |  {dte_str}')
    if legs:
        lines.append(legs)
    spot_line = f'Spot: {spot_s}'
    if dist_str:
        spot_line += f'  ·  {dist_str}'
    lines.append(spot_line)
    lines.append(f'{entry_lbl} ${credit:.2f} → {val_s}  |  P&L: {pnl_s}{warn_stop}')
    lines.append(f'{tp_line}  |  {sl_line}')

    ctx = []
    if ivr  is not None: ctx.append(f'IVR {ivr:.0f}%')
    if iv   is not None: ctx.append(f'IV {iv:.1f}%')
    if pop  is not None: ctx.append(f'POP {pop:.0f}%')
    if bias:             ctx.append(f'Bias {bias}')
    if ctx:
        lines.append('At entry: ' + ' | '.join(ctx))
    if opened_at:
        lines.append(f'Opened: {opened_at}')

    if action in ('closed', 'alert') and r.get('reason'):
        lines.append(f'↳ {r["reason"]}')

    return '\n'.join(lines)


async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    await update.message.reply_text('🔄 Running monitor cycle...')
    try:
        results = await run_monitor()
        now         = datetime.now(ET)
        market_open = (9 <= now.hour < 16) or (now.hour == 16 and now.minute == 0)
        mode        = db.get_state('mode') or 'paper'

        if not results:
            await update.message.reply_text('✅ Monitor complete — no open positions')
            return

        if mode != 'live':
            mode_note = '_(paper mode — no live closes)_'
        elif not market_open:
            mode_note = '_(market closed — closes in dry-run)_'
        else:
            mode_note = ''

        closed_n = sum(1 for r in results if r['action'] == 'closed')
        alerts_n = sum(1 for r in results if r['action'] == 'alert')
        header   = (f'📊 *Monitor — {len(results)} positions | '
                    f'{closed_n} closed | {alerts_n} alerts*')
        if mode_note:
            header += f'\n{mode_note}'

        # Send header then one message per position (avoids 4096-char TG limit)
        await update.message.reply_text(header, parse_mode='Markdown')
        for r in results:
            try:
                block = _fmt_position_block(r)
                await update.message.reply_text(block, parse_mode='Markdown')
            except Exception as _e:
                await update.message.reply_text(
                    f'⚠️ {r["symbol"]} — display error: {str(_e)[:80]}')
    except Exception as e:
        await update.message.reply_text(f'Monitor error: {str(e)[:120]}')


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)

    summary = db.get_performance_summary()
    if summary is None:
        await update.message.reply_text('No closed trades yet.')
        return

    by_strat = summary.get('by_strategy', {})
    strat_lines = []
    for strat_name, s in sorted(by_strat.items(), key=lambda x: -abs(x[1]['pnl'])):
        strat_lines.append(f'  {strat_name}: {s["count"]}t {s["win_rate"]:.0f}% ${s["pnl"]:+.2f}')

    msg = (
        f'📊 *Performance Summary*\n\n'
        f'Total trades: `{summary["total"]}`\n'
        f'Win rate: `{summary["win_rate"]:.1f}%` ({summary["winners"]}W / {summary["losers"]}L)\n'
        f'Total P&L: `${summary["total_pnl"]:+.2f}`\n'
        f'Avg P&L: `${summary["avg_pnl"]:+.2f}`\n'
        f'This week: `${summary["week_pnl"]:+.2f}` | This month: `${summary["month_pnl"]:+.2f}`\n\n'
    )
    if summary.get('best_trade'):
        b = summary['best_trade']
        msg += f'Best: `{b["symbol"]} {b["strategy"]} ${b["pnl"]:+.2f}`\n'
    if summary.get('worst_trade'):
        w = summary['worst_trade']
        msg += f'Worst: `{w["symbol"]} {w["strategy"]} ${w["pnl"]:+.2f}`\n'
    if strat_lines:
        msg += '\nBy strategy:\n' + '\n'.join(strat_lines)

    await update.message.reply_text(msg, parse_mode='Markdown')


# ── Kill switch ─────────────────────────────────────────────────────

async def _check_kill_switch():
    balance = db.get_current_balance()
    if balance <= float(KILL_SWITCH):
        if db.get_kill_alert_pending():
            await tg(
                f'🚨 *KILL SWITCH* — Balance ${balance:,.2f} ≤ ${KILL_SWITCH:,.2f}\n'
                f'All new entries halted. Manual review required.'
            )
            db.ack_kill_alert()


# ── Balance sync ────────────────────────────────────────────────────

async def sync_balance():
    """Fetch TT NLV and store as authoritative live balance."""
    try:
        nlv = await tt.tt_get_balance()
        if nlv and nlv > 0:
            db.set_state('tt_live_balance', str(nlv))
            log.info(f'Balance sync: TT NLV = ${nlv:,.2f}')
            peak_str = db.get_state('balance_peak')
            peak = float(peak_str) if peak_str else 0.0
            if nlv > peak:
                db.set_state('balance_peak', str(nlv))
                log.info(f'New balance peak: ${nlv:,.2f}')
            await _check_kill_switch()
        else:
            log.warning('Balance sync: TT returned no valid NLV')
    except Exception as e:
        log.error(f'Balance sync failed: {e}')


# ── Order placement ─────────────────────────────────────────────────

async def _place_order(order: dict, dry_run: bool = True) -> dict | None:
    """Route order dict to the correct TT placement function."""
    strategy  = order.get('strategy', '')
    symbol    = order.get('symbol', '')
    contracts = int(order.get('contracts', 1))
    expiry_s  = order.get('expiry')
    expiry    = date.fromisoformat(expiry_s) if expiry_s else None

    try:
        if strategy in ('put_credit_spread', 'call_credit_spread'):
            opt_type = 'put' if 'put' in strategy else 'call'
            return await tt.tt_place_credit_spread(
                symbol=symbol, expiry=expiry,
                sell_strike=order['sell_strike'], buy_strike=order['buy_strike'],
                option_type=opt_type, credit=order['credit'],
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'iron_condor':
            return await tt.tt_place_iron_condor(
                symbol=symbol, expiry=expiry,
                sell_put=order.get('put_sell_strike')  or order.get('sell_put',  0),
                buy_put=order.get('put_buy_strike')    or order.get('buy_put',   0),
                sell_call=order.get('call_sell_strike') or order.get('sell_call', 0),
                buy_call=order.get('call_buy_strike')   or order.get('buy_call',  0),
                total_credit=order['credit'],
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'jade_lizard':
            return await tt.tt_place_jade_lizard(
                symbol=symbol, expiry=expiry,
                sell_put=order.get('put_sell_strike')   or order.get('sell_put',   0),
                sell_call=order.get('call_sell_strike') or order.get('sell_call',  0),
                buy_call=order.get('call_buy_strike')   or order.get('buy_call',   0),
                total_credit=order['credit'],
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'debit_spread':
            opt_type = str(order.get('sub_type', 'call')).lower()
            return await tt.tt_place_debit_spread(
                symbol=symbol, expiry=expiry,
                buy_strike=order['buy_strike'], sell_strike=order['sell_strike'],
                option_type=opt_type, debit=order['debit'],
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'calendar_spread':
            near_expiry = date.fromisoformat(order['near_expiry']) if order.get('near_expiry') else expiry
            far_expiry  = date.fromisoformat(order['far_expiry'])  if order.get('far_expiry')  else expiry
            opt_type    = str(order.get('sub_type', 'call')).lower()
            return await tt.tt_place_calendar_spread(
                symbol=symbol,
                near_expiry=near_expiry, far_expiry=far_expiry,
                strike=order.get('atm_strike') or order.get('strike', 0),
                option_type=opt_type, debit=order.get('est_debit') or order.get('debit', 0),
                contracts=contracts, dry_run=dry_run,
            )

        else:
            log.error(f'Unknown strategy for placement: {strategy} ({symbol})')
            return None

    except Exception as e:
        log.error(f'Order placement exception {symbol} {strategy}: {e}')
        return None


def _build_record_kwargs(order: dict, signal: dict, data: dict) -> dict:
    strategy = order.get('strategy', '')
    s = dict(signal)
    s['sell_strike']     = (order.get('sell_strike') or order.get('call_sell_strike')
                            or order.get('sell_put') or order.get('put_sell_strike'))
    s['buy_strike']      = (order.get('buy_strike') or order.get('call_buy_strike')
                            or order.get('buy_call') or order.get('put_buy_strike'))
    s['sell_strike_put'] = order.get('put_sell_strike') or order.get('sell_put')
    s['buy_strike_put']  = order.get('put_buy_strike')  or order.get('buy_put')
    s['sell_call']       = order.get('call_sell_strike') or order.get('sell_call')
    s['near_expiry']     = order.get('near_expiry')
    s['far_expiry']      = order.get('far_expiry')
    s['expiry']          = order.get('expiry')
    s['dte']             = order.get('dte')
    s['contracts']       = order.get('contracts')
    s['max_loss']        = order.get('max_loss')
    s['profit_target']   = order.get('profit_target')
    s['loss_stop']       = order.get('loss_stop')
    s['roll_pop_floor']  = order.get('roll_pop_floor', 33.0)
    s['strategy']        = strategy
    s['sub_type']        = order.get('sub_type', signal.get('sub_type'))

    # Calendar spread: build_calendar_spread uses 'atm_strike' not 'sell_strike',
    # and sub_type is '' from select_strategy. Fix both so monitor can price the spread.
    if strategy == 'calendar_spread':
        if not s.get('sell_strike'):
            s['sell_strike'] = order.get('atm_strike')
        if not s.get('sub_type'):
            s['sub_type'] = 'call'  # matches _place_order default

    is_debit = 'debit' in strategy or 'calendar' in strategy
    if is_debit:
        s['credit_debit'] = -abs(float(order.get('est_debit') or order.get('debit') or 0))
    else:
        s['credit_debit'] = abs(float(order.get('credit') or 0))

    bp = data.get('best_put')
    bc = data.get('best_call')
    ref = bp or bc
    if ref:
        s['entry_delta'] = ref.get('delta')
        s['entry_iv']    = ref.get('iv')
        s['entry_theta'] = ref.get('theta')
        s['entry_vega']  = ref.get('vega')
        s['entry_gamma'] = ref.get('gamma')

    return {'signal': s, 'data': dict(data)}


async def _execute_signal(order: dict, signal: dict, data: dict,
                           dry_run: bool = True) -> bool:
    """Place order, poll fill, record in DB, set OCO."""
    symbol    = order.get('symbol', '')
    strategy  = order.get('strategy', '')
    contracts = int(order.get('contracts', 1))
    mode      = db.get_state('mode') or 'paper'

    log.info(f'Executing: {symbol} {strategy} x{contracts} | dry_run={dry_run}')

    result = await _place_order(order, dry_run=dry_run)
    if not result or result.get('status') == 'FAILED' or result.get('error'):
        err = result.get('error', 'unknown') if result else 'placement returned None'
        log.error(f'Order failed: {symbol} {strategy} — {err}')
        err_safe = err.replace('`', "'")  # strip backticks — break TG markdown
        await tg(f'❌ Order failed: *{symbol}* `{strategy}`\n{err_safe}')
        return False

    order_id = result.get('order_id')
    status   = result.get('status', 'unknown')
    log.info(f'Order placed: {symbol} id={order_id} status={status}')

    kwargs = _build_record_kwargs(order, signal, data)
    try:
        trade_id = db.record_trade(kwargs['signal'], kwargs['data'], mode=mode)
    except Exception as e:
        log.error(f'DB record failed {symbol}: {e}')
        trade_id = None

    is_debit  = 'debit' in strategy or 'calendar' in strategy
    cr        = float(order.get('credit') or order.get('est_debit') or order.get('debit') or 0)
    credit_str = f'{"debit" if is_debit else "credit"}=${cr:.2f}'

    await tg(
        f'📋 *{symbol}* `{strategy}` x{contracts}\n'
        f'{credit_str} | expiry={order.get("expiry")} | id={trade_id}\n'
        f'Order: {order_id} status={status}'
        + ('\n_(dry_run — not live)_' if dry_run else '')
    )

    if not dry_run and order_id:
        fill = await tt.tt_poll_order_fill(order_id, intervals=[15, 15, 30, 60])
        if fill.get('filled'):
            fill_price = fill.get('fill_price')
            log.info(f'Filled: {symbol} order={order_id} fill_price={fill_price}')
            await tg(f'✅ *{symbol}* filled @ {fill_price or "N/A"} (order {order_id})')
        elif fill.get('terminal'):
            await tg(f'⚠️ *{symbol}* order {order_id} terminal: {fill.get("status")} — NOT opened')
            if trade_id:
                db.mark_externally_closed(trade_id)
            return False
        else:
            await tg(f'⏳ *{symbol}* order {order_id} not yet filled after {fill.get("waited_s")}s — monitoring')

    if not dry_run and not is_debit and order_id and trade_id:
        try:
            credit_val = cr
            tp_price   = round(credit_val * (1 - PROFIT_TARGET_CREDIT), 2)
            sl_price   = round(credit_val * LOSS_STOP_MULTIPLIER, 2)
            is_ic      = (strategy == 'iron_condor')
            oco_result = await tt.tt_place_oco_close(
                symbol=symbol,
                expiry=date.fromisoformat(order['expiry']),
                sell_strike=order.get('sell_strike', 0) or order.get('sell_put', 0),
                buy_strike=order.get('buy_strike', 0)   or order.get('buy_call', 0),
                option_type='put' if 'put' in strategy else 'call',
                tp_price=tp_price, sl_price=sl_price,
                contracts=contracts,
                is_ic=is_ic,
                sell_put=order.get('sell_put')   if is_ic else None,
                buy_put=order.get('buy_put')     if is_ic else None,
                sell_call=order.get('sell_call') if is_ic else None,
                buy_call=order.get('buy_call')   if is_ic else None,
            )
            if oco_result.get('oco_id'):
                db.set_oco_order_id(trade_id, oco_result['oco_id'])
                log.info(f'OCO set: {symbol} oco_id={oco_result["oco_id"]} tp={tp_price} sl={sl_price}')
        except Exception as e:
            log.warning(f'OCO setup failed {symbol}: {str(e)[:80]}')

    return True


# ── Scan ────────────────────────────────────────────────────────────

async def run_scan():
    """Full watchlist scan. Called at 09:45 ET on market days."""
    now = datetime.now(ET)
    log.info(f'=== SCAN START {now.strftime("%Y-%m-%d %H:%M ET")} ===')

    can_trade, reason = db.check_guardrails()
    if not can_trade:
        log.info(f'SCAN BLOCKED: {reason}')
        await tg(f'🚫 Scan blocked: {reason}')
        return

    open_count = db.get_open_trade_count()
    if open_count >= MAX_POSITIONS:
        log.info(f'SCAN: max positions reached ({open_count}/{MAX_POSITIONS})')
        await tg(f'ℹ️ Scan: max positions ({open_count}/{MAX_POSITIONS}) — no new entries')
        return

    mode    = db.get_state('mode') or 'paper'
    dry_run = (mode != 'live')

    await tg(f'🔍 *Kubera scan* {now.strftime("%Y-%m-%d %H:%M ET")} | '
             f'mode={mode} | positions={open_count}/{MAX_POSITIONS}')

    balance = db.get_current_balance()
    log.info(f'Balance: ${balance:,.2f} | mode={mode}')

    # VIX
    vix_data = None
    vix_dir  = 'unknown'
    try:
        vix_data = await get_vix_data()
        vix_dir  = vix_data.get('vix_dir', 'unknown')
        log.info(f'VIX: {vix_data.get("vix")} dir={vix_dir}')
    except Exception as e:
        log.warning(f'VIX fetch failed: {str(e)[:80]}')
        await tg(f'⚠️ VIX unavailable — scan continuing with vix_dir=unknown')

    # Batch prefetch
    await tg('📡 Prefetching market data...')
    symbols = list(WATCHLIST)
    spots   = {}
    metrics = {}
    history = {}
    try:
        spots = await tt.tt_get_spot_batch(symbols)
        log.info(f'Spot prefetch: {len(spots)}/{len(symbols)} symbols')
    except Exception as e:
        log.warning(f'Spot prefetch failed: {str(e)[:60]}')
    try:
        metrics = await tt.tt_get_metrics_batch(symbols)
        log.info(f'Metrics prefetch: {len(metrics)}/{len(symbols)} symbols')
    except Exception as e:
        log.warning(f'Metrics prefetch failed: {str(e)[:60]}')
    try:
        history = await tt.tt_prefetch_history(symbols, days=60)
        log.info(f'History prefetch: {len(history)}/{len(symbols)} symbols')
    except Exception as e:
        log.warning(f'History prefetch failed: {str(e)[:60]}')

    tt_cache = {'spots': spots, 'metrics': metrics, 'history': history}

    earnings_cache = {}
    try:
        earnings_cache = await fetch_earnings_cache(tt_metrics=metrics)
        log.info(f'Earnings: {len(earnings_cache)} upcoming announcements cached')
    except Exception as e:
        log.warning(f'Earnings cache failed: {str(e)[:60]}')

    open_trades   = db.get_open_trades()
    sector_counts: dict[str, int] = {}
    for t in open_trades:
        s = t['sector'] or 'Unknown'
        sector_counts[s] = sector_counts.get(s, 0) + 1

    traded_today = db.symbols_today()

    # Snapshot max trade ID before scan — used to find newly placed trades at the end
    with db.get_conn() as _c:
        _row = _c.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone()
        pre_scan_id = int(_row[0])

    placed   = 0
    skipped  = 0
    errors   = 0
    open_now = open_count

    for symbol in symbols:
        if open_now >= MAX_POSITIONS:
            log.info(f'Scan: max positions reached mid-scan ({open_now})')
            break

        if symbol in traded_today:
            skipped += 1
            continue

        try:
            data = await get_options_data(symbol, vix_dir=vix_dir, tt_cache=tt_cache)
            if data is None:
                skipped += 1
                continue

            strategy, sub_type, strat_reason = select_strategy(
                data['ivr_regime'], data['bias'], data
            )
            if strategy == 'skip' or strategy is None:
                log.info(f'SKIP {symbol}: {strat_reason}')
                skipped += 1
                continue

            open_pos_list = [dict(t) for t in open_trades]
            ok, val_failures = validate_entry(data, strategy, sub_type, earnings_cache, open_pos_list)
            if not ok:
                log.info(f'SKIP {symbol} ({strategy}): {val_failures}')
                skipped += 1
                continue

            sector = data.get('sector', 'Unknown')
            if sector_counts.get(sector, 0) >= MAX_SECTOR_POSITIONS:
                log.info(f'SKIP {symbol}: sector gate ({sector} has {sector_counts[sector]} positions)')
                skipped += 1
                continue

            call_spread_width = 5.0
            if strategy == 'jade_lizard':
                bc = data.get('best_call')
                if bc:
                    bc_strike = bc['strike']
                    calls = sorted([c for c in data.get('chain_calls', [])
                                    if c['strike'] > bc_strike], key=lambda x: x['strike'])
                    if calls:
                        call_spread_width = round(calls[0]['strike'] - bc_strike, 0)

            signal = build_signal(data, strategy, sub_type, balance, call_spread_width)
            if signal is None:
                skipped += 1
                continue

            if strategy == 'calendar_spread':
                far_list = await tt.tt_get_option_instruments(
                    symbol, CAL_DTE_BACK - 10, CAL_DTE_BACK + 15,
                    prefer_nearest=False, ranked=True
                )
                if not far_list:
                    log.info(f'SKIP {symbol}: no far expiry for calendar (DTE {CAL_DTE_BACK})')
                    skipped += 1
                    continue
                signal['_cal_far_expiry_list'] = far_list

            order = build_order(data, signal)
            if order is None or order.get('error'):
                log.info(f'SKIP {symbol}: build_order failed — {order.get("error") if order else "None"}')
                skipped += 1
                continue

            # Calendar spread: populate near/far expiry from data + far_list into order.
            # build_calendar_spread() omits expiry fields; _place_order() needs them.
            if strategy == 'calendar_spread':
                near_str = str(data.get('expiry', ''))
                far_list_resolved = signal.get('_cal_far_expiry_list', [])
                if not far_list_resolved:
                    log.info(f'SKIP {symbol}: calendar far_list empty after build_order')
                    skipped += 1
                    continue
                far_str = far_list_resolved[0][0]   # (expiry_str, dte, options) tuple
                order['near_expiry'] = near_str
                order['far_expiry']  = far_str
                order['expiry']      = near_str
                order['dte']         = data.get('dte', 0)

            # Re-check capital ceiling immediately before execution —
            # earlier placements in this same scan cycle may have consumed headroom.
            can_trade_now, guard_now = db.check_guardrails()
            if not can_trade_now:
                log.info(f'SKIP {symbol}: capital ceiling hit mid-scan — {guard_now}')
                skipped += 1
                continue

            success = await _execute_signal(order, signal, data, dry_run=dry_run)
            if success:
                placed   += 1
                open_now += 1
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
                traded_today.add(symbol)
            else:
                errors += 1

        except Exception as e:
            log.error(f'Scan error {symbol}: {e}', exc_info=True)
            errors += 1

    summary = (
        f'✅ *Scan complete* — {placed} placed | {skipped} skipped | {errors} errors\n'
        f'Balance: ${balance:,.2f} | Positions: {open_now}/{MAX_POSITIONS}'
    )
    await tg(summary)
    log.info(f'=== SCAN END: {placed} placed, {skipped} skipped, {errors} errors ===')

    # Send new trades block — one HTML message listing every trade opened this scan
    if placed > 0:
        try:
            with db.get_conn() as _c:
                new_rows = _c.execute(
                    "SELECT * FROM trades WHERE id > ? AND status='open' ORDER BY id",
                    (pre_scan_id,)
                ).fetchall()
            if new_rows:
                lines = [_fmt_scan_trade(dict(r)) for r in new_rows]
                block = f'📋 <b>Trades opened ({len(new_rows)}):</b>\n\n' + '\n\n'.join(lines)
                await tg_html(block)
        except Exception as e:
            log.warning(f'New trades block failed: {e}')


# ── Monitor wrapper ─────────────────────────────────────────────────

async def run_monitor():
    mode    = db.get_state('mode') or 'paper'
    dry_run = (mode != 'live')
    try:
        results = await monitor_positions(send_telegram=tg, dry_run=dry_run)
        return results or []
    except Exception as e:
        log.error(f'Monitor exception: {e}', exc_info=True)
        await tg(f'❌ Monitor error: {str(e)[:80]}')
        return []


# ── Application ─────────────────────────────────────────────────────

async def post_init(application):
    """Called after Telegram Application starts. Connects TT, inits DB, starts scheduler."""
    from telegram import BotCommand
    global _tg_bot
    _tg_bot = application.bot

    await application.bot.set_my_commands([
        BotCommand('scan',      'Trigger manual scan'),
        BotCommand('monitor',   'Run monitor cycle now'),
        BotCommand('positions', 'Open positions + P&L'),
        BotCommand('history',   'Closed trade summary'),
        BotCommand('status',    'Bot state + guardrails'),
        BotCommand('balance',   'Fetch live TT NLV'),
        BotCommand('pause',     'Halt new entries'),
        BotCommand('resume',    'Re-enable entries'),
        BotCommand('mode',      'Show or set mode (paper/live)'),
        BotCommand('help',      'Command list'),
    ])

    db.init_db()
    log.info('DB initialised')

    ok = await tt.tt_connect(
        provider_secret=TT_CLIENT_SECRET,
        refresh_token=TT_REFRESH_TOKEN,
        account_number=TT_ACCOUNT,
        is_test=TT_SANDBOX,
    )
    if not ok:
        log.error('TT connection failed at startup')
        await tg('❌ Kubera: TastyTrade connection failed at startup')
    else:
        log.info(f'TT connected: account={TT_ACCOUNT} sandbox={TT_SANDBOX}')
        await tg(f'🟢 *Kubera online* | mode={db.get_state("mode") or "paper"} | '
                 f'account={TT_ACCOUNT} | sandbox={TT_SANDBOX}')

    scheduler = AsyncIOScheduler(timezone='America/New_York')
    scheduler.add_job(sync_balance, 'cron', day_of_week='mon-fri', hour=8,  minute=0,    id='sync_balance')
    scheduler.add_job(run_scan,     'cron', day_of_week='mon-fri', hour=9,  minute=45,   id='run_scan')
    scheduler.add_job(run_monitor,  'cron', day_of_week='mon-fri', hour='9-16', minute='0,30', id='run_monitor')
    scheduler.start()
    log.info('APScheduler started: sync=08:00, scan=09:45, monitor=*/30min')


def main():
    app = (
        Application.builder()
        .token(TG_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler('start',     cmd_start))
    app.add_handler(CommandHandler('help',      cmd_help))
    app.add_handler(CommandHandler('status',    cmd_status))
    app.add_handler(CommandHandler('positions', cmd_positions))
    app.add_handler(CommandHandler('balance',   cmd_balance))
    app.add_handler(CommandHandler('scan',      cmd_scan))
    app.add_handler(CommandHandler('pause',     cmd_pause))
    app.add_handler(CommandHandler('resume',    cmd_resume))
    app.add_handler(CommandHandler('mode',      cmd_mode))
    app.add_handler(CommandHandler('monitor',   cmd_monitor))
    app.add_handler(CommandHandler('history',   cmd_history))

    log.info('Kubera starting...')
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()

