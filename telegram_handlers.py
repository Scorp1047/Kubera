"""
telegram_handlers.py — Kubera Telegram command interface

Commands:
  /start     — show available commands
  /status    — bot state, balance, mode, guardrails
  /positions — open positions with current management levels
  /balance   — fetch live TT NLV and update DB
  /scan      — trigger an immediate scan (manual override)
  /pause     — halt new entries (monitor continues)
  /resume    — re-enable new entries
  /mode      — show or set trading mode (paper/live)
  /monitor   — run a manual monitor cycle now
  /history   — recent closed trades and P&L summary
"""

import logging
from datetime import datetime, date
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import log, ET, TG_CHAT

import database as db


# ── Auth guard ──────────────────────────────────────────────────────

def _authed(update: Update) -> bool:
    """Only respond to messages from the configured chat."""
    return str(update.effective_chat.id) == str(TG_CHAT)


async def _deny(update: Update):
    await update.message.reply_text('Unauthorized.')


# ── /start ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    await update.message.reply_text(
        '*Kubera — TastyTrade Options Bot*\n\n'
        'Commands:\n'
        '/status — bot state + guardrails\n'
        '/positions — open positions\n'
        '/balance — fetch live TT NLV\n'
        '/scan — trigger manual scan\n'
        '/pause — halt new entries\n'
        '/resume — re-enable entries\n'
        '/mode [paper|live] — show/set mode\n'
        '/monitor — run monitor cycle now\n'
        '/history — closed trade summary',
        parse_mode='Markdown'
    )


# ── /status ─────────────────────────────────────────────────────────

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

    from config import KILL_SWITCH, MAX_POSITIONS
    open_count  = db.get_open_trade_count()
    deployed    = db.get_deployed_capital()
    deployed_pct = round(deployed / balance * 100, 1) if balance > 0 else 0

    status_icon = '🟢' if can_trade else '🔴'
    msg = (
        f'{status_icon} *Kubera Status*\n\n'
        f'Mode: `{mode}` | Balance: `${balance:,.2f}`\n'
        f'Peak: `${peak}` | Kill Switch: `${KILL_SWITCH:,.0f}`\n'
        f'Positions: `{open_count}/{MAX_POSITIONS}` | Deployed: `{deployed_pct}%`\n\n'
        f'Paused: `{paused}` | Weekly pause: `{weekly}` | Monthly: `{monthly}`\n'
        f'Streak: `+{wins}W / {losses}L`\n\n'
    )
    if not can_trade:
        msg += f'⛔ *{guard_reason}*'
    else:
        msg += '✅ Ready to trade'

    await update.message.reply_text(msg, parse_mode='Markdown')


# ── /positions ──────────────────────────────────────────────────────

async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)

    trades = db.get_open_trades()
    if not trades:
        await update.message.reply_text('No open positions.')
        return

    today = date.today()
    lines = ['*Open Positions*\n']
    for t in trades:
        t = dict(t)
        expiry_s = t.get('expiry') or t.get('far_expiry', '?')
        try:
            exp = date.fromisoformat(expiry_s)
            dte = (exp - today).days
        except Exception:
            dte = '?'

        cr = float(t.get('credit_debit') or 0)
        is_debit = cr < 0
        cr_label = f'debit=${abs(cr):.2f}' if is_debit else f'credit=${cr:.2f}'

        # Stop and target
        stop   = t.get('stop_value')
        target = t.get('target_value')
        mgmt   = ''
        if stop and target:
            mgmt = f' | tp={target:.2f} sl={stop:.2f}'

        last_val = t.get('last_spread_value')
        val_str  = f' | val=${last_val:.4f}' if last_val else ''

        strategy = t.get('strategy', '?')
        symbol   = t.get('symbol', '?')
        contracts = t.get('contracts', 1)
        ivr      = t.get('ivr', '?')
        bias     = t.get('bias', '?')

        lines.append(
            f'#{t["id"]} *{symbol}* {strategy} x{contracts}\n'
            f'{cr_label} | DTE={dte} | IVR={ivr} bias={bias}'
            f'{mgmt}{val_str}'
        )

    await update.message.reply_text('\n\n'.join(lines), parse_mode='Markdown')


# ── /balance ────────────────────────────────────────────────────────

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    await update.message.reply_text('Fetching balance from TastyTrade...')
    try:
        from bot import sync_balance
        await sync_balance()
        balance = db.get_current_balance()
        await update.message.reply_text(f'💰 Balance: `${balance:,.2f}`', parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f'Balance fetch failed: {str(e)[:80]}')


# ── /scan ────────────────────────────────────────────────────────────

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    await update.message.reply_text('🔍 Triggering manual scan...')
    try:
        from bot import run_scan
        await run_scan()
    except Exception as e:
        await update.message.reply_text(f'Scan error: {str(e)[:80]}')


# ── /pause ───────────────────────────────────────────────────────────

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    db.set_state('paused', 'true')
    await update.message.reply_text('⏸ *Paused* — new entries halted. Monitor continues.', parse_mode='Markdown')


# ── /resume ──────────────────────────────────────────────────────────

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    db.set_state('paused', 'false')
    can_trade, reason = db.check_guardrails()
    if can_trade:
        await update.message.reply_text('▶️ *Resumed* — new entries enabled.', parse_mode='Markdown')
    else:
        await update.message.reply_text(
            f'▶️ Pause cleared, but still blocked:\n`{reason}`',
            parse_mode='Markdown'
        )


# ── /mode ────────────────────────────────────────────────────────────

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
        f'{icon} Mode set to `{new_mode}`. Scans and monitor will use this mode.',
        parse_mode='Markdown'
    )


# ── /monitor ─────────────────────────────────────────────────────────

async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)
    await update.message.reply_text('🔄 Running manual monitor cycle...')
    try:
        from bot import run_monitor
        await run_monitor()
    except Exception as e:
        await update.message.reply_text(f'Monitor error: {str(e)[:80]}')


# ── /history ─────────────────────────────────────────────────────────

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authed(update):
        return await _deny(update)

    summary = db.get_performance_summary()
    if summary is None:
        await update.message.reply_text('No closed trades yet.')
        return

    by_strat = summary.get('by_strategy', {})
    strat_lines = []
    for strat, s in sorted(by_strat.items(), key=lambda x: -abs(x[1]['pnl'])):
        strat_lines.append(
            f'  {strat}: {s["count"]}t {s["win_rate"]:.0f}% ${s["pnl"]:+.2f}'
        )

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


# ── Registration ─────────────────────────────────────────────────────

def register_handlers(app: Application):
    """Register all command handlers. Called from bot.py post_init."""
    app.add_handler(CommandHandler('start',     cmd_start))
    app.add_handler(CommandHandler('status',    cmd_status))
    app.add_handler(CommandHandler('positions', cmd_positions))
    app.add_handler(CommandHandler('balance',   cmd_balance))
    app.add_handler(CommandHandler('scan',      cmd_scan))
    app.add_handler(CommandHandler('pause',     cmd_pause))
    app.add_handler(CommandHandler('resume',    cmd_resume))
    app.add_handler(CommandHandler('mode',      cmd_mode))
    app.add_handler(CommandHandler('monitor',   cmd_monitor))
    app.add_handler(CommandHandler('history',   cmd_history))
    log.info('Telegram handlers registered: start status positions balance scan pause resume mode monitor history')
