"""
bot.py — Kubera main orchestrator

Scan schedule (market days only):
  09:45 ET — full watchlist scan (strategy selection + order placement)
  every 30 min (9:30–16:30 ET) — position monitor
  08:00 ET — balance sync from TastyTrade NLV

Scan flow per symbol:
  1. get_options_data()   — market data, IVR, bias, chain, expected move
  2. select_strategy()   — IVR + bias → strategy choice
  3. validate_entry()    — gates: earnings, OI, DTE, sector concentration, etc.
  4. build_signal()      — size, profit target, loss stop
  5. build_order()       — strike selection, wing sizing
  6. Place order via TT  — routed by strategy
  7. Poll fill           — 120s with backoff
  8. Record in DB + OCO  — persistent state
"""

import asyncio
import logging
import os
from datetime import datetime, date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.ext import Application

import config as _cfg
from config import (
    log, ET, TG_TOKEN, TG_CHAT,
    TT_CLIENT_SECRET, TT_REFRESH_TOKEN, TT_ACCOUNT, TT_SANDBOX,
    WATCHLIST, MAX_POSITIONS, MAX_SECTOR_POSITIONS,
    DTE_MIN, DTE_MAX, DTE_SWEET_SPOT, DTE_EXIT,
    PROFIT_TARGET_CREDIT, PROFIT_TARGET_DEBIT, PROFIT_TARGET_CALENDAR,
    LOSS_STOP_MULTIPLIER, DEBIT_HARD_STOP_PCT,
    CAL_DTE_FRONT, CAL_DTE_BACK,
    KILL_SWITCH, REAL_BALANCE,
)
import tasty as tt
import market as mkt
import signals as sig
import strategies as strat
import database as db
import monitor as mon


# ── Telegram helper ─────────────────────────────────────────────────

_tg_bot: Bot = None

async def tg(message: str):
    """Send a Telegram message. Silently swallows failures."""
    global _tg_bot
    try:
        if _tg_bot is None:
            _tg_bot = Bot(token=TG_TOKEN)
        await _tg_bot.send_message(chat_id=TG_CHAT, text=message,
                                   parse_mode='Markdown')
    except Exception as e:
        log.warning(f'TG send failed: {str(e)[:60]}')


# ── Kill switch check ───────────────────────────────────────────────

async def _check_kill_switch():
    """Check balance vs kill switch. Alert if triggered."""
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
    """Fetch TT NLV and store as authoritative live balance.
    Runs at 08:00 ET — after overnight settlement, before scan."""
    try:
        nlv = await tt.tt_get_balance()
        if nlv and nlv > 0:
            db.set_state('tt_live_balance', str(nlv))
            log.info(f'Balance sync: TT NLV = ${nlv:,.2f}')
            # Update peak balance
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
    """Route order dict to the correct TT placement function.
    Returns TT response dict or None on failure."""
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
                sell_strike=order['sell_strike'],
                buy_strike=order['buy_strike'],
                option_type=opt_type,
                credit=order['credit'],
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'iron_condor':
            return await tt.tt_place_iron_condor(
                symbol=symbol, expiry=expiry,
                sell_put=order['sell_put'], buy_put=order['buy_put'],
                sell_call=order['sell_call'], buy_call=order['buy_call'],
                total_credit=order['credit'],
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'jade_lizard':
            return await tt.tt_place_jade_lizard(
                symbol=symbol, expiry=expiry,
                sell_put=order['sell_put'],
                sell_call=order['sell_call'],
                buy_call=order['buy_call'],
                total_credit=order['credit'],
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'debit_spread':
            opt_type = str(order.get('sub_type', 'call')).lower()
            return await tt.tt_place_debit_spread(
                symbol=symbol, expiry=expiry,
                buy_strike=order['buy_strike'],
                sell_strike=order['sell_strike'],
                option_type=opt_type,
                debit=order['debit'],
                contracts=contracts, dry_run=dry_run,
            )

        elif strategy == 'calendar_spread':
            near_expiry = date.fromisoformat(order['near_expiry']) if order.get('near_expiry') else expiry
            far_expiry  = date.fromisoformat(order['far_expiry'])  if order.get('far_expiry')  else expiry
            opt_type    = str(order.get('sub_type', 'call')).lower()
            return await tt.tt_place_calendar_spread(
                symbol=symbol,
                near_expiry=near_expiry, far_expiry=far_expiry,
                strike=order['strike'],
                option_type=opt_type,
                debit=order['debit'],
                contracts=contracts, dry_run=dry_run,
            )

        else:
            log.error(f'Unknown strategy for placement: {strategy} ({symbol})')
            return None

    except Exception as e:
        log.error(f'Order placement exception {symbol} {strategy}: {e}')
        return None


def _build_record_kwargs(order: dict, signal: dict, data: dict) -> dict:
    """Map from order/signal/data dicts to record_trade() kwargs.
    Extracts entry greeks from the best_put/best_call in data."""
    strategy = order.get('strategy', '')
    s = dict(signal)
    # Push order-level strike details into signal for record_trade
    s['sell_strike']     = order.get('sell_strike') or order.get('sell_put')
    s['buy_strike']      = order.get('buy_strike')  or order.get('buy_call')
    s['sell_strike_put'] = order.get('sell_put')
    s['buy_strike_put']  = order.get('buy_put')
    s['sell_call']       = order.get('sell_call')
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

    # credit_debit: positive for credits, negative for debits
    is_debit = 'debit' in strategy or 'calendar' in strategy
    if is_debit:
        s['credit_debit'] = -abs(float(order.get('debit') or 0))
    else:
        s['credit_debit'] = abs(float(order.get('credit') or 0))

    # Entry greeks from best_put or best_call
    bp = data.get('best_put')
    bc = data.get('best_call')
    ref = bp or bc
    if ref:
        s['entry_delta'] = ref.get('delta')
        s['entry_iv']    = ref.get('iv')
        # theta/vega/gamma typically in greeks from DXLink; not always in best_put
        s['entry_theta'] = ref.get('theta')
        s['entry_vega']  = ref.get('vega')
        s['entry_gamma'] = ref.get('gamma')

    d = dict(data)
    # Attach VIX from bot context if available
    return {'signal': s, 'data': d}


async def _execute_signal(order: dict, signal: dict, data: dict,
                           dry_run: bool = True) -> bool:
    """Place order, poll fill, record in DB, set OCO.
    Returns True if successfully placed (fill pending is OK)."""
    symbol    = order.get('symbol', '')
    strategy  = order.get('strategy', '')
    contracts = int(order.get('contracts', 1))
    mode      = db.get_state('mode') or 'paper'

    log.info(f'Executing: {symbol} {strategy} x{contracts} | dry_run={dry_run}')

    # Place order
    result = await _place_order(order, dry_run=dry_run)
    if not result or result.get('status') == 'FAILED' or result.get('error'):
        err = result.get('error', 'unknown') if result else 'placement returned None'
        log.error(f'Order failed: {symbol} {strategy} — {err}')
        await tg(f'❌ Order failed: *{symbol}* {strategy}\n{err}')
        return False

    order_id = result.get('order_id')
    status   = result.get('status', 'unknown')
    log.info(f'Order placed: {symbol} id={order_id} status={status}')

    # Record in DB immediately (status='open' regardless of fill — fill confirmed below)
    kwargs = _build_record_kwargs(order, signal, data)
    try:
        trade_id = db.record_trade(kwargs['signal'], kwargs['data'], mode=mode)
    except Exception as e:
        log.error(f'DB record failed {symbol}: {e}')
        trade_id = None

    is_debit  = 'debit' in strategy or 'calendar' in strategy
    cr        = float(order.get('credit') or order.get('debit') or 0)
    credit_str = f'{"debit" if is_debit else "credit"}=${cr:.2f}'

    await tg(
        f'📋 *{symbol}* {strategy} x{contracts}\n'
        f'{credit_str} | expiry={order.get("expiry")} | id={trade_id}\n'
        f'Order: {order_id} status={status}'
        + ('\n*(dry_run — not live)*' if dry_run else '')
    )

    # Poll fill (skip for dry_run)
    if not dry_run and order_id:
        fill = await tt.tt_poll_order_fill(order_id, intervals=[15, 15, 30, 60])
        if fill.get('filled'):
            fill_price = fill.get('fill_price')
            log.info(f'Filled: {symbol} order={order_id} fill_price={fill_price}')
            await tg(f'✅ *{symbol}* filled @ {fill_price or "N/A"} (order {order_id})')
        elif fill.get('terminal'):
            await tg(
                f'⚠️ *{symbol}* order {order_id} terminal: {fill.get("status")} — position NOT opened'
            )
            if trade_id:
                db.mark_externally_closed(trade_id)
            return False
        else:
            # Timed out — order still live (may fill later via OCO or manually)
            await tg(f'⏳ *{symbol}* order {order_id} not yet filled after {fill.get("waited_s")}s — will monitor')

    # Set OCO TP/SL close orders (credits only, not debit/calendar)
    if not dry_run and not is_debit and order_id and trade_id:
        try:
            credit_val = cr
            tp_price   = round(credit_val * (1 - PROFIT_TARGET_CREDIT), 2)  # 50% target
            sl_price   = round(credit_val * LOSS_STOP_MULTIPLIER, 2)         # 2× stop

            is_ic = (strategy == 'iron_condor')
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
    """Full watchlist scan. Called at 09:45 ET on market days.

    Flow:
      1. Guardrail check
      2. Batch prefetch: spot / metrics / history for all watchlist symbols
      3. Fetch VIX + direction
      4. Per-symbol: data → strategy → validate → signal → order → place
      5. TG progress updates
    """
    now = datetime.now(ET)
    log.info(f'=== SCAN START {now.strftime("%Y-%m-%d %H:%M ET")} ===')

    # ── Guardrails ──────────────────────────────────────────────────
    can_trade, reason = db.check_guardrails()
    if not can_trade:
        log.info(f'SCAN BLOCKED: {reason}')
        await tg(f'🚫 Scan blocked: {reason}')
        return

    # ── Position count check ────────────────────────────────────────
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

    # ── VIX + direction ─────────────────────────────────────────────
    vix_data = None
    vix_dir  = 'unknown'
    try:
        vix_data = await mkt.get_vix_data()
        vix_dir  = vix_data.get('vix_dir', 'unknown')
        log.info(f'VIX: {vix_data.get("vix")} dir={vix_dir}')
    except Exception as e:
        log.warning(f'VIX fetch failed: {str(e)[:80]}')
        await tg(f'⚠️ VIX unavailable — scan continuing with vix_dir=unknown')

    # ── Batch prefetch ──────────────────────────────────────────────
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

    # Earnings cache (uses already-fetched metrics)
    earnings_cache = {}
    try:
        earnings_cache = mkt.fetch_earnings_cache(tt_metrics=metrics)
        log.info(f'Earnings: {len(earnings_cache)} upcoming announcements cached')
    except Exception as e:
        log.warning(f'Earnings cache failed: {str(e)[:60]}')

    # Sector concentration tracking
    open_trades = db.get_open_trades()
    sector_counts: dict[str, int] = {}
    for t in open_trades:
        s = t['sector'] or 'Unknown'
        sector_counts[s] = sector_counts.get(s, 0) + 1

    # Already traded today (dedup gate)
    traded_today = db.symbols_today()

    # ── Symbol loop ─────────────────────────────────────────────────
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
            # ── Market data ────────────────────────────────────────
            data = mkt.get_options_data(
                symbol, vix_dir=vix_dir, tt_cache=tt_cache
            )
            if data is None:
                skipped += 1
                continue

            # ── Strategy selection ─────────────────────────────────
            strategy, sub_type = sig.select_strategy(
                data['ivr_regime'], data['bias'], data
            )
            if strategy is None:
                log.info(f'SKIP {symbol}: no strategy (IVR={data["ivr"]} bias={data["bias"]})')
                skipped += 1
                continue

            # ── Entry validation ───────────────────────────────────
            open_pos_list = [dict(t) for t in open_trades]
            ok, val_reason = sig.validate_entry(
                data, strategy, sub_type, earnings_cache, open_pos_list
            )
            if not ok:
                log.info(f'SKIP {symbol} ({strategy}): {val_reason}')
                skipped += 1
                continue

            # Sector concentration gate
            sector = data.get('sector', 'Unknown')
            if sector_counts.get(sector, 0) >= MAX_SECTOR_POSITIONS:
                log.info(f'SKIP {symbol}: sector gate ({sector} has {sector_counts[sector]} positions)')
                skipped += 1
                continue

            # ── Signal ────────────────────────────────────────────
            # For calendar: need far-expiry option data
            call_spread_width = 5.0  # default; build_signal picks best from chain
            if strategy == 'jade_lizard':
                # Estimate call spread width from chain
                bc = data.get('best_call')
                if bc:
                    bc_strike = bc['strike']
                    calls = sorted([c for c in data.get('chain_calls', [])
                                    if c['strike'] > bc_strike], key=lambda x: x['strike'])
                    if calls:
                        call_spread_width = round(calls[0]['strike'] - bc_strike, 0)

            signal = sig.build_signal(data, strategy, sub_type, balance, call_spread_width)
            if signal is None:
                skipped += 1
                continue

            # ── Order construction ─────────────────────────────────
            # Calendar: need far_expiry option data (second chain fetch)
            if strategy == 'calendar_spread':
                # Fetch far expiry chain (CAL_DTE_BACK)
                far_list = await tt.tt_get_option_instruments(
                    symbol, CAL_DTE_BACK - 10, CAL_DTE_BACK + 15,
                    prefer_nearest=False, ranked=True
                )
                if not far_list:
                    log.info(f'SKIP {symbol}: no far expiry for calendar (DTE {CAL_DTE_BACK})')
                    skipped += 1
                    continue
                # Attach as signal hint for build_order
                signal['_cal_far_expiry_list'] = far_list

            order = strat.build_order(data, signal)
            if order is None or order.get('error'):
                log.info(f'SKIP {symbol}: build_order failed — {order.get("error") if order else "None"}')
                skipped += 1
                continue

            # ── Execute ────────────────────────────────────────────
            success = await _execute_signal(order, signal, data, dry_run=dry_run)
            if success:
                placed  += 1
                open_now += 1
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
                # Reload traded_today to prevent double-scanning same symbol
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


# ── Monitor wrapper ─────────────────────────────────────────────────

async def run_monitor():
    """Wrapper for APScheduler: call monitor.monitor_positions()."""
    mode    = db.get_state('mode') or 'paper'
    dry_run = (mode != 'live')
    try:
        await mon.monitor_positions(send_telegram=tg, dry_run=dry_run)
    except Exception as e:
        log.error(f'Monitor exception: {e}', exc_info=True)
        await tg(f'❌ Monitor error: {str(e)[:80]}')


# ── Application ─────────────────────────────────────────────────────

async def post_init(application):
    """Called after Telegram Application starts. Connects TT, inits DB, wires async loop."""
    global _tg_bot
    _tg_bot = application.bot

    # Init DB
    db.init_db()
    log.info('DB initialised')

    # Connect TastyTrade
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

    # Wire the asyncio event loop so market.py can call TT from sync context
    loop = asyncio.get_event_loop()
    mkt.set_tasty_loop(loop)

    # APScheduler
    scheduler = AsyncIOScheduler(timezone='America/New_York')

    # Balance sync: 08:00 ET daily
    scheduler.add_job(sync_balance, 'cron',
                      day_of_week='mon-fri', hour=8, minute=0,
                      id='sync_balance')

    # Scan: 09:45 ET Mon–Fri
    scheduler.add_job(run_scan, 'cron',
                      day_of_week='mon-fri', hour=9, minute=45,
                      id='run_scan')

    # Monitor: every 30 min 09:30–16:30 ET Mon–Fri
    scheduler.add_job(run_monitor, 'cron',
                      day_of_week='mon-fri', hour='9-16', minute='0,30',
                      id='run_monitor')

    scheduler.start()
    log.info('APScheduler started: sync=08:00, scan=09:45, monitor=*/30min')


def main():
    """Entry point. Run: venv/bin/python3 bot.py"""
    from telegram.ext import Application as TgApp
    app = (
        TgApp.builder()
        .token(TG_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Import and wire Telegram command handlers
    from telegram_handlers import register_handlers
    register_handlers(app)

    log.info('Kubera starting...')
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
