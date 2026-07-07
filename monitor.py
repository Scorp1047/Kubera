"""
monitor.py — Kubera position monitor

Runs every 30 minutes. Applies TastyTrade's published management rules:

  Credit trades (credit spread, IC, Jade Lizard):
    1. PROFIT TARGET  — close when spread value ≤ credit × 0.50  (50%)
    2. TIME EXIT      — close when DTE ≤ 21 (gamma acceleration zone)
    3. HARD STOP      — close when spread value ≥ credit × 2.00  (2× received)
    4. POP ALERT      — alert (do not auto-close) when POP drops below 33%

  Debit trades (debit spread):
    1. PROFIT TARGET  — close when spread value ≥ debit × 1.25  (25% profit)
    2. TIME EXIT      — close when DTE ≤ 21
    3. HARD STOP      — close when spread value ≤ debit × 0.50  (50% loss)

  Calendar spreads:
    1. PROFIT TARGET  — close when spread value ≥ debit × 1.15  (15% profit)
    2. TIME EXIT      — close when front-month DTE ≤ 5  (near-month about to expire)
    3. HARD STOP      — close when spread value ≤ debit × 0.50  (50% loss)

Close execution routing by strategy:
  put_credit_spread / call_credit_spread → tt_close_position
  iron_condor                            → tt_close_iron_condor
  jade_lizard                            → tt_close_jade_lizard
  debit_spread                           → tt_close_debit_spread
  calendar_spread                        → tt_close_calendar_spread

Source: tastylive.com — managing-credit-spreads, managing-trades, rolling definition page.
"""

import asyncio
from datetime import datetime, date, timedelta
from config import (
    log, ET,
    PROFIT_TARGET_CREDIT, PROFIT_TARGET_DEBIT, PROFIT_TARGET_CALENDAR,
    LOSS_STOP_MULTIPLIER, DEBIT_HARD_STOP_PCT,
    DTE_EXIT, ROLL_POP_FLOOR,
)
from database import (
    get_open_trades, close_trade_db, get_state,
    get_oco_order_id, set_oco_order_id,
    mark_externally_closed,
    update_trade_last_spot, update_trade_last_spread,
    mark_as_rolled, get_roll_count,
)
from market import bs_prob_otm


# ── Spread Value Fetch ──────────────────────────────────────────────

async def _get_credit_spread_value(trade) -> float | None:
    """Fetch current mark for a 2-leg credit spread.
    Returns net debit to close (what we pay to buy back), or None on failure.
    Positive value = spread has positive value (we owe this to close)."""
    import tasty as tt
    from tastytrade.instruments import get_option_chain
    from datetime import date as _date
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
        # Net debit to close = buy back short − sell back long
        return round(float(sell_mid) - float(buy_mid), 4)
    except Exception as e:
        log.warning(f'Credit spread value error {trade.get("symbol")}: {str(e)[:60]}')
        return None


async def _get_ic_value(trade) -> float | None:
    """Fetch current total mark for a 4-leg iron condor.
    Returns total debit to close (sum of both spreads), or None."""
    import tasty as tt
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
            if ask > 0:
                return (bid + ask) / 2
            return p

        # Debit to close = buy back shorts − sell back longs
        debit = (_mid(sp_opt) - _mid(bp_opt)) + (_mid(sc_opt) - _mid(bc_opt))
        return round(max(debit, 0), 4)
    except Exception as e:
        log.warning(f'IC value error {trade.get("symbol")}: {str(e)[:60]}')
        return None


async def _get_jade_lizard_value(trade) -> float | None:
    """Fetch current total mark for a 3-leg Jade Lizard.
    Returns net debit to close, or None."""
    import tasty as tt
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

        # Debit to close = buy back short put + buy back short call − sell back long call
        debit = _mid(sp_opt) + _mid(sc_opt) - _mid(bc_opt)
        return round(max(debit, 0), 4)
    except Exception as e:
        log.warning(f'Jade Lizard value error {trade.get("symbol")}: {str(e)[:60]}')
        return None


async def _get_debit_spread_value(trade) -> float | None:
    """Fetch current spread mark for a debit spread.
    Returns net credit to close (what we receive when selling back), or None."""
    import tasty as tt
    try:
        symbol     = trade['symbol']
        sell_strike = float(trade['sell_strike'] or 0)   # OTM sell wing (buy to close)
        buy_strike  = float(trade['buy_strike']  or 0)   # closer-to-money bought leg (sell to close)
        expiry_str  = trade['expiry']
        # direction stored as 'call' or 'put' (sub_type from signals.py)
        strategy    = trade['strategy']
        opt_type    = 'call' if 'call' in str(trade.get('direction', '')).lower() else 'put'

        val = await tt.tt_get_spread_value(symbol, expiry_str, sell_strike, buy_strike, opt_type)
        if val is None:
            return None
        sell_mid = val['sell'].get('mid') or val['sell'].get('price', 0)
        buy_mid  = val['buy'].get('mid')  or val['buy'].get('price', 0)
        if sell_mid is None or buy_mid is None:
            return None
        # For debit spread: sell leg = the short wing, buy leg = the long leg
        # Value (credit to close) = long leg value − short wing value
        return round(float(buy_mid) - float(sell_mid), 4)
    except Exception as e:
        log.warning(f'Debit spread value error {trade.get("symbol")}: {str(e)[:60]}')
        return None


async def _get_calendar_spread_value(trade) -> float | None:
    """Fetch current value (credit to close) for a calendar spread.
    Value = far leg value − near leg value (we sold near, bought far).
    Returns credit to close, or None."""
    import tasty as tt
    from tastytrade.instruments import get_option_chain
    from datetime import date as _date
    try:
        symbol     = trade['symbol']
        near_str   = trade['near_expiry']
        far_str    = trade['far_expiry']
        strike     = float(trade['sell_strike'] or 0)
        opt_type   = 'C' if 'call' in str(trade.get('direction', '')).lower() else 'P'

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

        # Credit to close calendar = far value − near value
        val = _mid(far_opt) - _mid(near_opt)
        return round(max(val, 0), 4)
    except Exception as e:
        log.warning(f'Calendar spread value error {trade.get("symbol")}: {str(e)[:60]}')
        return None


async def _get_spot(symbol: str) -> float | None:
    """Fetch current equity spot price."""
    import tasty as tt
    try:
        spots = await tt.tt_get_spot_batch([symbol])
        return spots.get(symbol)
    except Exception:
        return None


# ── Close Execution ─────────────────────────────────────────────────

async def _close_trade(trade, reason: str, pnl: float,
                       hit_target: int = 0, was_stopped: int = 0,
                       exit_spot=None, current_value=None,
                       dry_run: bool = False) -> bool:
    """Route to the correct TT close function based on strategy.
    Updates DB on success. Returns True if close order was placed."""
    import tasty as tt
    from datetime import date as _date

    strategy    = trade['strategy']
    symbol      = trade['symbol']
    contracts   = int(trade['contracts'] or 1)
    expiry      = _date.fromisoformat(trade['expiry']) if trade.get('expiry') else None
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
                buy_strike=float(trade['buy_strike']  or 0),
                sell_strike=float(trade['sell_strike'] or 0),
                option_type=str(trade.get('direction', 'call')).lower(),
                credit=round(current_value or 0, 2),
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'calendar_spread':
            near_expiry = _date.fromisoformat(trade['near_expiry']) if trade.get('near_expiry') else expiry
            far_expiry  = _date.fromisoformat(trade['far_expiry'])  if trade.get('far_expiry')  else expiry
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

    # Gross P&L for credit trades: (credit − current_value) × 100 × contracts
    # Gross P&L for debit trades:  (current_value − |debit|) × 100 × contracts
    cv = float(current_value or 0)
    cr = float(credit)
    is_debit = 'debit' in strategy or 'calendar' in strategy
    if is_debit:
        gross_pnl = round((cv - abs(cr)) * 100 * contracts, 2)
    else:
        gross_pnl = round((abs(cr) - cv) * 100 * contracts, 2)

    close_trade_db(
        trade_id=trade['id'],
        reason=reason,
        pnl=gross_pnl,   # commission deducted inside close_trade_db
        hit_target=hit_target,
        was_stopped=was_stopped,
        exit_spot=exit_spot,
        exit_value=current_value,
        dte_at_close=dte_at_close,
    )
    log.info(f'Closed: {symbol} {strategy} id={trade["id"]} reason={reason} '
             f'pnl=${gross_pnl:.2f} cv={cv:.4f} credit={cr:.4f}')
    return True


# ── Monitor Logic ───────────────────────────────────────────────────

async def _monitor_one(trade, vix=None, dry_run: bool = False) -> dict:
    """Check and manage a single open trade. Returns a result dict for TG reporting.
    Keys: symbol, strategy, id, action, reason, value, pnl_approx, pop, dte_remaining."""
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
    }

    symbol      = trade['symbol']
    strategy    = trade['strategy']
    credit      = float(trade['credit_debit']  or 0)
    contracts   = int(trade['contracts']       or 1)
    spot_price  = float(trade['spot_price']    or 0)
    is_debit    = 'debit' in strategy or 'calendar' in strategy
    is_calendar = strategy == 'calendar_spread'

    # ── DTE remaining ──────────────────────────────────────────────
    today  = date.today()
    expiry = None
    if is_calendar and trade.get('near_expiry'):
        # For calendar: DTE based on near (short) leg expiry
        expiry = date.fromisoformat(trade['near_expiry'])
    elif trade.get('expiry'):
        expiry = date.fromisoformat(trade['expiry'])

    dte_remaining = (expiry - today).days if expiry else None
    result['dte_remaining'] = dte_remaining

    # ── Fetch current value ────────────────────────────────────────
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
        # Update rolling fallback in DB
        update_trade_last_spread(trade['id'], current_value)
        cv = float(current_value)

        # Approx P&L
        if is_debit:
            pnl_approx = round((cv - abs(credit)) * 100 * contracts, 2)
        else:
            pnl_approx = round((abs(credit) - cv) * 100 * contracts, 2)
        result['pnl_approx'] = pnl_approx

    # ── Fetch current spot ─────────────────────────────────────────
    spot = None
    try:
        spot = await _get_spot(symbol)
        if spot:
            update_trade_last_spot(trade['id'], spot)
    except Exception:
        spot = trade.get('last_spot_price') or spot_price

    # ── DTE EXIT (21 DTE) — check before value-dependent exits ─────
    dte_exit = int(DTE_EXIT)
    if dte_remaining is not None and dte_remaining <= dte_exit:
        if current_value is not None:
            closed = await _close_trade(
                trade, reason='dte_exit', pnl=result.get('pnl_approx', 0),
                hit_target=0, was_stopped=0,
                exit_spot=spot, current_value=current_value, dry_run=dry_run,
            )
            if closed:
                result['action'] = 'closed'
                result['reason'] = f'DTE exit ({dte_remaining} DTE ≤ {dte_exit})'
                return result
        else:
            # Can't fetch value — alert and hold
            result['action'] = 'alert'
            result['reason'] = f'DTE {dte_remaining} ≤ {dte_exit} — value unavailable, manual action needed'
            result['alert']  = True
            return result

    # ── If value unavailable, bail out ─────────────────────────────
    if current_value is None:
        result['reason'] = 'value unavailable — hold'
        return result

    cv = float(current_value)
    cr = abs(float(credit))

    # ── CREDIT TRADE EXIT LOGIC ────────────────────────────────────
    if not is_debit:
        # Profit target: 50% credit received
        profit_target = round(cr * (1 - PROFIT_TARGET_CREDIT), 4)  # e.g. $1.00 credit → close at $0.50
        # Hard stop: 2× credit received
        hard_stop = round(cr * LOSS_STOP_MULTIPLIER, 4)              # e.g. $1.00 credit → stop at $2.00

        if cv <= profit_target:
            closed = await _close_trade(
                trade, reason='profit_target',
                pnl=result['pnl_approx'] or 0,
                hit_target=1, was_stopped=0,
                exit_spot=spot, current_value=cv, dry_run=dry_run,
            )
            if closed:
                result['action'] = 'closed'
                result['reason'] = f'50% profit target (credit={cr:.2f} → value={cv:.2f})'
                return result

        elif cv >= hard_stop:
            closed = await _close_trade(
                trade, reason='hard_stop',
                pnl=result['pnl_approx'] or 0,
                hit_target=0, was_stopped=1,
                exit_spot=spot, current_value=cv, dry_run=dry_run,
            )
            if closed:
                result['action'] = 'closed'
                result['reason'] = f'2× hard stop (credit={cr:.2f} → value={cv:.2f})'
                return result

        # POP alert: recalculate POP on short strike using current spot
        try:
            if spot and spot > 0 and trade.get('expiry') and dte_remaining:
                T      = dte_remaining / 365
                r      = 0.05
                iv     = float(trade.get('entry_iv') or 0)
                if iv > 0:
                    # IC: check both sides; use the "more at risk" side
                    if strategy == 'iron_condor':
                        sp  = float(trade['sell_strike_put'] or 0)
                        sc  = float(trade['sell_strike']     or 0)
                        pop_put  = bs_prob_otm(spot, sp, T, r, iv / 100, 'put')
                        pop_call = bs_prob_otm(spot, sc, T, r, iv / 100, 'call')
                        pop  = min(pop_put, pop_call)
                    elif strategy == 'jade_lizard':
                        sp   = float(trade['sell_strike_put'] or 0)
                        pop  = bs_prob_otm(spot, sp, T, r, iv / 100, 'put')
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

    # ── DEBIT TRADE EXIT LOGIC ─────────────────────────────────────
    else:
        debit_paid = abs(cr)
        dte_exit_cal = 5 if is_calendar else dte_exit

        if is_calendar:
            profit_target = round(debit_paid * (1 + PROFIT_TARGET_CALENDAR), 4)
        else:
            profit_target = round(debit_paid * (1 + PROFIT_TARGET_DEBIT), 4)
        hard_stop = round(debit_paid * (1 - DEBIT_HARD_STOP_PCT), 4)

        # For debit/calendar: cv is the current spread value (credit to close)
        # profit: cv >= debit × 1.25 (or 1.15 for calendar)
        # stop:   cv <= debit × 0.50
        if cv >= profit_target:
            closed = await _close_trade(
                trade, reason='profit_target',
                pnl=result['pnl_approx'] or 0,
                hit_target=1, was_stopped=0,
                exit_spot=spot, current_value=cv, dry_run=dry_run,
            )
            if closed:
                pct = (PROFIT_TARGET_CALENDAR if is_calendar else PROFIT_TARGET_DEBIT) * 100
                result['action'] = 'closed'
                result['reason'] = f'{pct:.0f}% debit profit target (paid={debit_paid:.2f} → value={cv:.2f})'
                return result

        elif cv <= hard_stop:
            closed = await _close_trade(
                trade, reason='hard_stop',
                pnl=result['pnl_approx'] or 0,
                hit_target=0, was_stopped=1,
                exit_spot=spot, current_value=cv, dry_run=dry_run,
            )
            if closed:
                result['action'] = 'closed'
                result['reason'] = f'50% debit hard stop (paid={debit_paid:.2f} → value={cv:.2f})'
                return result

        # Calendar: also exit when near-month DTE is very small (≤ 5)
        if is_calendar and dte_remaining is not None and dte_remaining <= dte_exit_cal:
            closed = await _close_trade(
                trade, reason='calendar_dte_exit',
                pnl=result['pnl_approx'] or 0,
                hit_target=0, was_stopped=0,
                exit_spot=spot, current_value=cv, dry_run=dry_run,
            )
            if closed:
                result['action'] = 'closed'
                result['reason'] = f'Calendar near-month DTE exit ({dte_remaining} DTE ≤ {dte_exit_cal})'
                return result

    return result


async def monitor_positions(send_telegram=None, dry_run: bool = False):
    """Main monitor entry point. Called every 30 minutes by bot.py APScheduler.
    Checks all open positions, applies TT management rules, sends TG alerts.

    send_telegram: async callable(message: str) — if None, alerts logged only.
    dry_run:       if True, TT close orders are placed as dry_run (validation only).
    """
    import tasty as tt

    now = datetime.now(ET)
    # Only run during market hours and 30 min after close (positions can be monitored all day
    # but closing only executes during market hours 9:30–16:00 ET Mon–Fri)
    is_weekday   = now.weekday() < 5
    market_hour  = (9 <= now.hour < 16) or (now.hour == 16 and now.minute == 0)

    if not is_weekday:
        log.debug('Monitor: weekend — skipping')
        return []

    trades = get_open_trades()
    if not trades:
        log.info('Monitor: no open positions')
        return []

    log.info(f'Monitor: checking {len(trades)} open trade(s) '
             f'[market_hours={market_hour} dry_run={dry_run}]')

    # Fetch current VIX (used for exit snapshot logging)
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

        # Only execute closes during market hours
        effective_dry_run = dry_run or (not market_hour)
        result = await _monitor_one(trade, vix=vix, dry_run=effective_dry_run)
        results.append(result)

        # Send TG alert if action taken or alert triggered
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
                    f"{emoji} CLOSED: {symbol} ({strategy})\n"
                    f"Reason: {reason}\n"
                    f"Value: ${value:.4f} | P&L ≈ ${pnl_a:+.2f}"
                    + (f" | DTE: {dte}" if dte is not None else "")
                    + ('\n(dry_run — not executed on TT)' if effective_dry_run else '')
                )
            else:
                msg = (
                    f"⚠️ ALERT: {symbol} ({strategy})\n"
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
