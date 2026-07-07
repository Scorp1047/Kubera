"""
signals.py — Kubera strategy selector and entry validator

What this file does:
  1. select_strategy()  — decision tree: IVR regime + bias → strategy name
  2. check_jade_lizard() — TT rule: credit ≥ call spread width → zero upside risk upgrade
  3. validate_entry()   — earnings block, POP floor, strike exists, safe-haven, sector cap
  4. calculate_size()   — flat BPR sizing: 5% of NLV per trade
  5. build_signal()     — assemble final signal dict for bot.py

What this file does NOT do:
  - RSI, MACD, ADX, EMA, Bollinger, Keltner — none
  - Multi-gate grading (A+/A/B+/B) — no grade system
  - Vol ratio / RV/IV / Alpha 1-4 — none
  - Grok / Claude AI calls — none
  - Per-trade position sizing by grade — one flat rule for all trades
"""

from datetime import date
from config import (
    log,
    IVR_HIGH, IVR_MEDIUM_FLOOR,
    MIN_POP, JADE_MIN_CREDIT_RATIO, IC_MAX_IVR,
    MAX_BPR_PCT, MAX_POSITIONS, MAX_SECTOR_POSITIONS,
    EARNINGS_BLOCK, SAFE_HAVEN_SYMBOLS, BLOCKED_SYMBOLS,
    SECTOR_MAP, PROFIT_TARGET_CREDIT, PROFIT_TARGET_DEBIT,
    PROFIT_TARGET_CALENDAR, DTE_EXIT,
    LOSS_STOP_MULTIPLIER, ROLL_POP_FLOOR,
)


# ── Strategy Decision Tree ─────────────────────────────────────────

def select_strategy(ivr_regime, bias, data):
    """Select the optimal strategy based on IVR regime and directional bias.

    Returns (strategy_name, sub_type, reason) or ('skip', '', reason).

    strategy_name: 'put_credit_spread' | 'call_credit_spread' | 'iron_condor'
                   | 'jade_lizard' | 'debit_spread' | 'calendar_spread' | 'skip'
    sub_type:      'call' or 'put' for debit_spread; '' otherwise
    reason:        human-readable explanation

    Decision tree (TastyTrade methodology):

    HIGH IVR (≥50):
      BULL  → Jade Lizard (if credit math works) else put credit spread
              Rationale: bullish + premium is expensive → sell puts (skew rich) + call spread
      BEAR  → call credit spread
              Rationale: bearish + premium expensive → sell calls
      NEUTRAL → iron condor
              Rationale: neutral + premium expensive → sell both sides

    MEDIUM IVR (25–49):
      BULL  → put credit spread (standard size)
              Rationale: directional conviction + moderate IV → directional credit
      BEAR  → call credit spread (standard size)
      NEUTRAL → SKIP
              Rationale: TT says insufficient premium at medium IV without directional view

    LOW IVR (<25):
      Move > threshold  → debit spread (call if stock oversold/dropped, put if overbought/risen)
              Rationale: price at extreme + IV cheap → buy directional with defined risk
      Stock quiet → calendar spread
              Rationale: bet on IV expansion from an abnormally quiet underlying
      Otherwise → SKIP
              Rationale: no premium to sell, no extreme to fade
    """
    symbol    = data.get('symbol', '')
    move_5d   = data.get('move_5d', 0.0)
    best_put  = data.get('best_put')
    best_call = data.get('best_call')

    if ivr_regime == 'HIGH':
        if bias == 'BULL':
            return 'jade_lizard', '', (
                f'HIGH IVR ({data["ivr"]:.0f}%) + BULL bias → Jade Lizard attempt '
                f'(put + call spread; if credit math fails, degrades to put credit spread)'
            )
        elif bias == 'BEAR':
            return 'call_credit_spread', '', (
                f'HIGH IVR ({data["ivr"]:.0f}%) + BEAR bias → call credit spread'
            )
        else:  # NEUTRAL
            return 'iron_condor', '', (
                f'HIGH IVR ({data["ivr"]:.0f}%) + NEUTRAL bias → iron condor '
                f'(sell both sides; no directional conviction)'
            )

    elif ivr_regime == 'MEDIUM':
        if bias == 'BULL':
            return 'put_credit_spread', '', (
                f'MEDIUM IVR ({data["ivr"]:.0f}%) + BULL bias → put credit spread'
            )
        elif bias == 'BEAR':
            return 'call_credit_spread', '', (
                f'MEDIUM IVR ({data["ivr"]:.0f}%) + BEAR bias → call credit spread'
            )
        else:
            return 'skip', '', (
                f'MEDIUM IVR ({data["ivr"]:.0f}%) + NEUTRAL → SKIP '
                f'(insufficient premium without directional view)'
            )

    elif ivr_regime == 'LOW':
        # Debit: price at an extreme relative to recent range
        from config import BIAS_MOVE_THRESHOLD
        if move_5d <= -BIAS_MOVE_THRESHOLD * 100:
            # Stock dropped 5%+ → mean reversion candidate → buy calls
            return 'debit_spread', 'call', (
                f'LOW IVR ({data["ivr"]:.0f}%) + stock down {move_5d:.1f}% (5d) → '
                f'call debit spread (oversold mean reversion + cheap options)'
            )
        elif move_5d >= BIAS_MOVE_THRESHOLD * 100:
            # Stock up 5%+ → overbought → buy puts
            return 'debit_spread', 'put', (
                f'LOW IVR ({data["ivr"]:.0f}%) + stock up +{move_5d:.1f}% (5d) → '
                f'put debit spread (overbought fade + cheap options)'
            )
        else:
            # Stock is quiet → calendar spread for IV expansion
            return 'calendar_spread', '', (
                f'LOW IVR ({data["ivr"]:.0f}%) + stock flat → calendar spread '
                f'(bet on IV expanding back toward mean; debit {PROFIT_TARGET_CALENDAR*100:.0f}% target)'
            )
    else:
        return 'skip', '', f'IVR unavailable or unclassified — SKIP'


# ── Jade Lizard Check ─────────────────────────────────────────────

def check_jade_lizard(best_put, best_call, call_spread_width):
    """TastyTrade Jade Lizard rule: zero upside risk when total credit ≥ call spread width.

    Structure: sell OTM put + sell OTM call spread (short call + long call higher).
    'Perfect' condition: put_credit + call_spread_credit ≥ call_spread_width
      → no upside risk at all (breakeven = short call strike).

    Returns (is_valid, total_credit, reason).
    is_valid = True  → upgrade to Jade Lizard
    is_valid = False → fall back to put credit spread only"""
    if best_put is None or best_call is None:
        return False, 0.0, 'missing put or call strike — cannot build Jade Lizard'

    put_credit          = best_put.get('bid', 0.0)
    call_credit         = best_call.get('bid', 0.0)
    total_credit        = round(put_credit + call_credit, 2)
    required_min_credit = round(call_spread_width * JADE_MIN_CREDIT_RATIO, 2)

    if total_credit >= required_min_credit:
        return True, total_credit, (
            f'Jade Lizard valid: credit ${total_credit:.2f} ≥ '
            f'call spread width ${call_spread_width:.0f} × {JADE_MIN_CREDIT_RATIO} '
            f'= ${required_min_credit:.2f} → zero upside risk'
        )
    else:
        return False, total_credit, (
            f'Jade Lizard invalid: credit ${total_credit:.2f} < '
            f'required ${required_min_credit:.2f} '
            f'(call spread width ${call_spread_width:.0f}) → downgrade to put credit spread'
        )


# ── Entry Validation ──────────────────────────────────────────────

def validate_entry(data, strategy, sub_type, earnings_cache, open_positions):
    """Validate that entry conditions are met. Returns (ok: bool, failures: list[str]).

    Checks (in order):
      1. Symbol not in BLOCKED_SYMBOLS
      2. Earnings block (within EARNINGS_BLOCK days)
      3. Strike exists for required legs
      4. POP ≥ MIN_POP (67%) for credit strategies
      5. Safe-haven call credit block
      6. IC: IVR not too extreme (IC_MAX_IVR)
      7. Sector concentration (MAX_SECTOR_POSITIONS)
      8. Max open positions (MAX_POSITIONS)
    """
    symbol  = data.get('symbol', '')
    sector  = data.get('sector', 'Unknown')
    ivr     = data.get('ivr', 0)
    failures = []

    # 1. Blocked symbols
    if symbol in BLOCKED_SYMBOLS:
        failures.append(f'{symbol} is permanently blocked (binary event risk)')
        return False, failures

    # 2. Earnings block
    if earnings_cache and symbol in earnings_cache:
        try:
            ed = date.fromisoformat(earnings_cache[symbol])
            days_away = (ed - date.today()).days
            if 0 <= days_away <= EARNINGS_BLOCK:
                failures.append(
                    f'Earnings in {days_away}d ({earnings_cache[symbol]}) — '
                    f'blocked within {EARNINGS_BLOCK}d window'
                )
                return False, failures
        except Exception:
            pass

    # 3. Strike existence for required legs
    best_put  = data.get('best_put')
    best_call = data.get('best_call')

    if strategy in ('put_credit_spread', 'jade_lizard'):
        if best_put is None:
            failures.append(f'no valid put strike found at 16 delta — cannot build {strategy}')
            return False, failures

    if strategy == 'call_credit_spread':
        if best_call is None:
            failures.append('no valid call strike found at 16 delta — cannot build call credit spread')
            return False, failures

    if strategy == 'iron_condor':
        if best_put is None or best_call is None:
            missing = 'put' if best_put is None else 'call'
            failures.append(f'no valid {missing} strike found — cannot build iron condor')
            return False, failures

    if strategy == 'jade_lizard':
        # Both legs required
        if best_call is None:
            failures.append('Jade Lizard requires call strike — missing (will degrade to put credit spread)')
            # Not a hard failure — caller handles the downgrade
            # But flag it so bot.py knows

    # 4. POP floor for credit strategies
    credit_strategies = ('put_credit_spread', 'call_credit_spread', 'iron_condor', 'jade_lizard')
    if strategy in credit_strategies:
        if strategy in ('put_credit_spread', 'jade_lizard') and best_put:
            pop = best_put.get('pop', 0.0)
            if pop < MIN_POP * 100:
                failures.append(
                    f'put POP {pop:.1f}% < {MIN_POP*100:.0f}% minimum — '
                    f'strike too close to current price'
                )
                return False, failures

        if strategy == 'call_credit_spread' and best_call:
            pop = best_call.get('pop', 0.0)
            if pop < MIN_POP * 100:
                failures.append(
                    f'call POP {pop:.1f}% < {MIN_POP*100:.0f}% minimum'
                )
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

    # 5. Safe-haven call credit block
    # TastyTrade: safe-haven assets surge on fear — selling calls on them is structurally wrong.
    if strategy == 'call_credit_spread' and symbol in SAFE_HAVEN_SYMBOLS:
        failures.append(
            f'{symbol} is a safe-haven asset — call credits always blocked '
            f'(surges during market stress regardless of IVR)'
        )
        return False, failures

    # 6. IC: block at extreme IVR (market pricing a large directional move)
    if strategy == 'iron_condor' and ivr > IC_MAX_IVR:
        failures.append(
            f'IC blocked: IVR {ivr:.0f}% > {IC_MAX_IVR:.0f}% — '
            f'extreme IV implies a large move expected; IC range thesis is wrong'
        )
        return False, failures

    # 7. Sector concentration
    sector_count = sum(
        1 for p in open_positions
        if SECTOR_MAP.get(p.get('symbol', ''), 'Unknown') == sector
        and p.get('status') == 'open'
    )
    if sector_count >= MAX_SECTOR_POSITIONS:
        failures.append(
            f'sector cap: {sector_count}/{MAX_SECTOR_POSITIONS} positions already open '
            f'in {sector}'
        )
        return False, failures

    # 8. Max total positions
    open_count = sum(1 for p in open_positions if p.get('status') == 'open')
    if open_count >= MAX_POSITIONS:
        failures.append(
            f'max positions: {open_count}/{MAX_POSITIONS} already open'
        )
        return False, failures

    return True, []


# ── Position Sizing ────────────────────────────────────────────────

def calculate_size(balance, bpr_per_contract):
    """Calculate number of contracts using flat BPR sizing.

    TastyTrade: "trade small, trade often."
    Kubera rule: max 5% of NLV per trade (MAX_BPR_PCT = 0.05).
    No grade-based sizing — all trades are treated equally.
    Minimum 1 contract always.

    bpr_per_contract: buying power reduction per contract.
      For a credit spread: (spread_width - credit) × 100
      For a debit spread:   debit × 100
    """
    if bpr_per_contract <= 0:
        return 1
    max_bpr  = balance * MAX_BPR_PCT
    contracts = max(1, int(max_bpr / bpr_per_contract))
    log.info(
        f'Sizing: balance=${balance:.0f} × {MAX_BPR_PCT*100:.0f}% = '
        f'${max_bpr:.0f} BPR budget / ${bpr_per_contract:.0f} per contract = '
        f'{contracts} contract(s)'
    )
    return contracts


# ── Signal Builder ─────────────────────────────────────────────────

def build_signal(data, strategy, sub_type, balance, call_spread_width=5.0):
    """Assemble the final signal dict that bot.py will pass to strategies.py and tasty.py.

    Returns signal dict or None if signal cannot be built.

    Signal dict keys:
      symbol, strategy, sub_type, expiry, dte,
      sell_strike, buy_strike (credit) / buy_strike, sell_strike (debit),
      credit / debit, contracts, max_loss, pop, iv, em_dollar, em_pct,
      profit_target, loss_stop, roll_pop_floor, dte_exit,
      ivr, ivr_regime, bias, bias_reason
    """
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
        # Management rules (same for all strategies, from TT research)
        'dte_exit':          DTE_EXIT,            # 21 DTE time-based exit
        'roll_pop_floor':    ROLL_POP_FLOOR,       # 0.33 — roll when POP < 33%
    }

    # ── Credit strategies ──────────────────────────────────────────
    if strategy in ('put_credit_spread', 'jade_lizard', 'iron_condor'):
        if best_put is None:
            log.warning(f'build_signal: {symbol} {strategy} — no put strike')
            return None

        put_credit   = best_put.get('bid', 0.0)
        put_strike   = best_put.get('strike', 0.0)
        put_wing     = best_put.get('wing', call_spread_width)   # strategies.py sets this
        put_pop      = best_put.get('pop', 0.0)
        put_delta    = best_put.get('delta', 0.0)
        put_iv       = best_put.get('iv', avg_iv)

        # Wing width: use expected move as guide for wing sizing
        # TT: wing placed to collect 1/3 the spread width in premium
        # strategies.py finalizes the exact buy_strike; use EM here for sizing
        wing_width = round(max(call_spread_width, em_dollar * 1.5), 0)

        bpr_per_contract = (wing_width - put_credit) * 100   # buying power used per contract
        contracts        = calculate_size(balance, bpr_per_contract)
        max_loss         = round(bpr_per_contract * contracts, 2)
        profit_target    = round(put_credit * PROFIT_TARGET_CREDIT, 2)
        loss_stop        = round(put_credit * LOSS_STOP_MULTIPLIER, 2)

        sig.update({
            'sell_strike':    put_strike,
            'buy_strike':     round(put_strike - wing_width, 2),
            'credit':         put_credit,
            'pop':            put_pop,
            'delta':          put_delta,
            'iv':             put_iv,
            'wing_width':     wing_width,
            'contracts':      contracts,
            'max_loss':       max_loss,
            'profit_target':  profit_target,   # close at this credit remaining
            'loss_stop':      loss_stop,        # 2× credit received
        })

        # Jade Lizard: attempt upgrade — add call spread
        if strategy == 'jade_lizard' and best_call is not None:
            jl_valid, jl_credit, jl_reason = check_jade_lizard(
                best_put, best_call, call_spread_width)
            if jl_valid:
                call_credit  = best_call.get('bid', 0.0)
                call_strike  = best_call.get('strike', 0.0)
                total_credit = round(put_credit + call_credit, 2)
                # Recalculate with combined credit
                bpr_per_contract = (wing_width - total_credit) * 100
                contracts        = calculate_size(balance, bpr_per_contract)
                max_loss         = round(bpr_per_contract * contracts, 2)
                profit_target    = round(total_credit * PROFIT_TARGET_CREDIT, 2)
                loss_stop        = round(total_credit * LOSS_STOP_MULTIPLIER, 2)
                sig.update({
                    'strategy':      'jade_lizard',
                    'call_strike':   call_strike,
                    'call_buy_strike': round(call_strike + call_spread_width, 2),
                    'call_spread_width': call_spread_width,
                    'credit':        total_credit,
                    'put_credit':    put_credit,
                    'call_credit':   call_credit,
                    'contracts':     contracts,
                    'max_loss':      max_loss,
                    'profit_target': profit_target,
                    'loss_stop':     loss_stop,
                    'jade_reason':   jl_reason,
                })
                log.info(f'{symbol}: Jade Lizard confirmed — {jl_reason}')
            else:
                # Degrade to put credit spread
                sig['strategy'] = 'put_credit_spread'
                log.info(f'{symbol}: Jade Lizard downgraded → put credit spread — {jl_reason}')

    elif strategy == 'call_credit_spread':
        if best_call is None:
            log.warning(f'build_signal: {symbol} call_credit_spread — no call strike')
            return None

        call_credit  = best_call.get('bid', 0.0)
        call_strike  = best_call.get('strike', 0.0)
        call_pop     = best_call.get('pop', 0.0)
        call_delta   = best_call.get('delta', 0.0)
        call_iv      = best_call.get('iv', avg_iv)

        wing_width       = round(max(call_spread_width, em_dollar * 1.5), 0)
        bpr_per_contract = (wing_width - call_credit) * 100
        contracts        = calculate_size(balance, bpr_per_contract)
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

    elif strategy == 'iron_condor':
        if best_put is None or best_call is None:
            log.warning(f'build_signal: {symbol} iron_condor — missing leg(s)')
            return None

        put_credit   = best_put.get('bid', 0.0)
        call_credit  = best_call.get('bid', 0.0)
        total_credit = round(put_credit + call_credit, 2)
        put_strike   = best_put.get('strike', 0.0)
        call_strike  = best_call.get('strike', 0.0)
        wing_width   = round(max(call_spread_width, em_dollar * 1.5), 0)

        bpr_per_contract = (wing_width - put_credit) * 100   # max loss = one side (can't lose both)
        contracts        = calculate_size(balance, bpr_per_contract)
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
            'put_pop':         best_put.get('pop', 0.0),
            'call_pop':        best_call.get('pop', 0.0),
            'wing_width':      wing_width,
            'contracts':       contracts,
            'max_loss':        max_loss,
            'profit_target':   profit_target,
            'loss_stop':       loss_stop,
        })

    # ── Debit strategies ───────────────────────────────────────────
    elif strategy == 'debit_spread':
        # sub_type: 'call' (oversold rebound) or 'put' (overbought fade)
        if sub_type == 'call':
            leg = best_call
        else:
            leg = best_put

        if leg is None:
            log.warning(f'build_signal: {symbol} debit_spread/{sub_type} — no strike found')
            return None

        # For debit spreads: buy ATM-ish, sell further OTM
        # The exact strikes are built in strategies.py using the full chain
        # Here we record the anchor leg (the long strike) for reference
        debit_iv  = leg.get('iv', avg_iv)
        debit_pop = leg.get('pop', 0.0)

        # Debit sizing: BPR = debit paid × 100
        # Placeholder debit — strategies.py calculates the real debit from the chain
        est_debit        = round(price * 0.02, 2)   # rough estimate: ~2% of stock price
        bpr_per_contract = est_debit * 100
        contracts        = calculate_size(balance, bpr_per_contract)
        max_loss         = round(bpr_per_contract * contracts, 2)
        profit_target_pct = PROFIT_TARGET_DEBIT   # 25% of debit paid

        sig.update({
            'anchor_strike':   leg.get('strike', 0.0),
            'sub_type':        sub_type,
            'est_debit':       est_debit,
            'debit_iv':        debit_iv,
            'pop':             debit_pop,
            'contracts':       contracts,
            'max_loss':        max_loss,
            'profit_target_pct': profit_target_pct,
            'loss_stop_pct':   0.50,   # close if loss > 50% of debit paid
        })

    elif strategy == 'calendar_spread':
        # Calendar: sell front-month (DTE_MIN), buy back-month (DTE_MAX)
        # strategies.py handles exact expiry selection
        # Size conservatively — calendar has complex vega risk
        est_debit        = round(price * 0.015, 2)   # rough: ~1.5% of stock price
        bpr_per_contract = est_debit * 100
        contracts        = calculate_size(balance, bpr_per_contract)
        max_loss         = round(bpr_per_contract * contracts, 2)

        sig.update({
            'est_debit':       est_debit,
            'contracts':       contracts,
            'max_loss':        max_loss,
            'profit_target_pct': PROFIT_TARGET_CALENDAR,   # 15% of debit paid
            'loss_stop_pct':   0.50,
        })

    else:
        log.warning(f'build_signal: unknown strategy {strategy}')
        return None

    log.info(
        f'Signal built: {symbol} {sig["strategy"]} '
        f'expiry={expiry} dte={dte} '
        f'ivr={ivr:.0f}% [{sig["ivr_regime"]}] bias={sig["bias"]} '
        f'contracts={sig.get("contracts",1)} '
        f'max_loss=${sig.get("max_loss",0):.0f}'
    )
    return sig
