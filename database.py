"""
database.py — Kubera SQLite persistence layer

Simplified vs Nexus X:
  - No grade column (no grade system in Kubera)
  - No Grok / AI fields (no AI in Kubera)
  - No RSI / MACD / ADX / EMA / regime columns
  - No learning_log table (no weekly Grok analysis)
  - No param_history table (TT methodology parameters are fixed)
  - No day_trades table (no PDT tracking)
  - No pending_queue (no /approve workflow — full_auto only)

Added vs Nexus X:
  - ivr_regime (HIGH / MEDIUM / LOW)
  - bias, bias_reason (directional bias for strategy selection)
  - em_dollar, em_pct (expected move at entry)
  - near_expiry, far_expiry (calendar spread dual-expiry)
  - sell_call (jade lizard third leg)
  - strategy breakdown in performance summary
"""

import sqlite3, json
from datetime import datetime, date, timedelta
from config import log, ET, REAL_BALANCE, KILL_SWITCH

DB = '/home/trader/kubera/data/kubera.db'


# ── Commission ─────────────────────────────────────────────────────
# TT commission structure (open only — TT charges $0 to close):
#   Platform fee : $1 / contract / leg, capped at $10 / leg
#   Exchange fee : $0.13 / contract / leg (charged both open AND close)
# Leg count by strategy:
#   Credit spread / debit spread / calendar = 2 legs
#   Iron condor / jade lizard               = 3–4 legs
def calc_commission(contracts, strategy):
    s = str(strategy).lower()
    if 'iron_condor' in s:
        legs = 4
    elif 'jade_lizard' in s:
        legs = 3
    else:
        legs = 2
    tt_per_leg = min(float(contracts), 10.0)   # $1/contract capped at $10
    tt_total   = round(tt_per_leg * legs, 2)
    exchange   = round(0.13 * contracts * legs * 2, 2)  # ×2 for both contract sides
    return round(tt_total + exchange, 2)


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def get_current_balance():
    """Return the authoritative account balance for sizing and guardrails.

    Paper mode: REAL_BALANCE (.env) + closed net P&L + open credits collected.
    Live mode:  tt_live_balance from bot_state (TT NLV synced at 8am ET daily).
    Falls back to REAL_BALANCE if nothing is available."""
    try:
        mode = get_state('mode') or 'paper'
        if mode == 'live':
            stored = get_state('tt_live_balance')
            if stored:
                val = float(stored)
                if val > 0:
                    return round(val, 2)
        conn = get_conn()
        closed_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status='closed' AND pnl IS NOT NULL"
        ).fetchone()[0]
        open_credits = conn.execute(
            """SELECT COALESCE(SUM(
                CASE WHEN strategy LIKE '%debit%' OR strategy LIKE '%calendar%'
                     THEN -credit_debit * 100 * contracts
                     ELSE  credit_debit * 100 * contracts - COALESCE(commission, 0)
                END
            ), 0) FROM trades WHERE status='open'"""
        ).fetchone()[0]
        conn.close()
        return round(REAL_BALANCE + closed_pnl + open_credits, 2)
    except Exception:
        return REAL_BALANCE


def init_db():
    conn = get_conn()

    # Main trades table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT DEFAULT paper,
            opened_at TEXT,
            symbol TEXT,
            strategy TEXT,
            direction TEXT,

            -- Entry market state
            ivr REAL,
            ivr_regime TEXT,
            bias TEXT,
            bias_reason TEXT,
            vix_at_entry REAL,
            avg_iv REAL,
            em_dollar REAL,
            em_pct REAL,
            sector TEXT,
            earnings_date TEXT,

            -- Strike structure
            sell_strike REAL,
            buy_strike REAL,
            sell_strike_put REAL,
            buy_strike_put REAL,
            sell_call REAL,
            near_expiry TEXT,
            far_expiry TEXT,
            expiry TEXT,
            dte_at_open INTEGER,

            -- Sizing
            credit_debit REAL,
            max_loss REAL,
            contracts INTEGER,
            capital_used REAL,
            commission REAL,

            -- Management levels
            stop_value REAL,
            target_value REAL,
            roll_pop_floor REAL,

            -- Entry greeks (sell-leg, from DXLink at open)
            entry_delta REAL,
            entry_theta REAL,
            entry_vega REAL,
            entry_gamma REAL,
            entry_iv REAL,
            spot_price REAL,

            -- OCO order tracking
            oco_order_id INTEGER,

            -- Roll tracking
            roll_count INTEGER DEFAULT 0,
            rolled_from_id INTEGER,
            rolled_to_id INTEGER,

            -- Manual override
            manual_mgmt INTEGER DEFAULT 0,

            -- Status
            status TEXT DEFAULT open,
            closed_at TEXT,
            close_reason TEXT,
            days_held INTEGER,
            pnl REAL,
            pnl_pct REAL,
            hit_profit_target INTEGER DEFAULT 0,
            was_stopped INTEGER DEFAULT 0,

            -- Exit snapshot
            exit_spot REAL,
            exit_value REAL,
            dte_at_close INTEGER,
            exit_vix REAL,
            exit_ivr REAL,
            exit_delta REAL,
            exit_iv REAL,
            close_reasoning TEXT,

            -- Rolling fallback (refreshed each monitor cycle, not used for stop/target)
            last_spot_price REAL,
            last_spread_value REAL
        )
    ''')

    # Bot state — key/value store for flags, mode, drawdown counters
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # Default bot state entries
    defaults = [
        ('mode',               'paper'),
        ('paused',             'false'),
        ('consecutive_losses', '0'),
        ('consecutive_wins',   '0'),
        ('weekly_pause',       'false'),
        ('monthly_pause',      'false'),
        ('monthly_pause_until',''),
        ('kill_alert_ack',     'false'),
        ('balance_peak',       ''),
    ]
    for k, v in defaults:
        conn.execute(
            'INSERT OR IGNORE INTO bot_state (key,value) VALUES (?,?)',
            (k, v)
        )

    conn.commit()
    conn.close()
    log.info('Database initialised')


def get_state(key):
    conn = get_conn()
    row  = conn.execute('SELECT value FROM bot_state WHERE key=?', (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def set_state(key, value):
    conn = get_conn()
    conn.execute(
        'INSERT OR REPLACE INTO bot_state (key,value) VALUES (?,?)',
        (key, str(value))
    )
    conn.commit()
    conn.close()


# ── Trade Recording ─────────────────────────────────────────────────

def record_trade(signal, data, mode='paper'):
    """Insert a new trade into the trades table.
    signal: dict from signals.py build_signal()
    data:   dict from market.py get_options_data()
    Returns the new trade ID."""
    conn      = get_conn()
    now       = datetime.now(ET).isoformat()
    credit    = float(signal.get('credit_debit') or 0)
    contracts = int(signal.get('contracts') or 1)
    max_loss  = float(signal.get('max_loss') or 0)
    strategy  = str(signal.get('strategy', ''))

    # Capital at risk: for credits = max_loss (spread width − credit × 100 × contracts)
    # For debits / calendar: debit paid × 100 × contracts
    is_debit = 'debit' in strategy or 'calendar' in strategy
    capital  = round(abs(credit) * 100 * contracts, 2) if is_debit else round(max_loss, 2)

    commission = calc_commission(contracts, strategy)

    # Profit target and loss stop — from signal (already computed in signals.py)
    tgt_val  = float(signal.get('profit_target') or round(credit * 0.50, 4))
    stop_val = float(signal.get('loss_stop')     or round(credit * 2.00, 4))
    pop_floor = float(signal.get('roll_pop_floor', 33.0))

    conn.execute('''
        INSERT INTO trades (
            mode, opened_at, symbol, strategy, direction,
            ivr, ivr_regime, bias, bias_reason,
            vix_at_entry, avg_iv, em_dollar, em_pct,
            sector, earnings_date,
            sell_strike, buy_strike, sell_strike_put, buy_strike_put,
            sell_call, near_expiry, far_expiry,
            expiry, dte_at_open,
            credit_debit, max_loss, contracts, capital_used, commission,
            stop_value, target_value, roll_pop_floor,
            entry_delta, entry_theta, entry_vega, entry_gamma, entry_iv,
            spot_price, status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        mode, now,
        signal.get('symbol'), strategy,
        signal.get('sub_type', 'neutral'),
        data.get('ivr'), data.get('ivr_regime'),
        data.get('bias'), data.get('bias_reason'),
        data.get('vix'), data.get('avg_iv'),
        data.get('em_dollar'), data.get('em_pct'),
        data.get('sector', 'Unknown'),
        data.get('earnings', [''])[0] if data.get('earnings') else None,
        signal.get('sell_strike'), signal.get('buy_strike'),
        signal.get('sell_strike_put'), signal.get('buy_strike_put'),
        signal.get('sell_call'),
        signal.get('near_expiry'), signal.get('far_expiry'),
        signal.get('expiry'), signal.get('dte'),
        credit, max_loss, contracts, capital, commission,
        stop_val, tgt_val, pop_floor,
        signal.get('entry_delta'),
        signal.get('entry_theta'),
        signal.get('entry_vega'),
        signal.get('entry_gamma'),
        signal.get('entry_iv'),
        data.get('price'),
        'open',
    ))
    trade_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.commit()
    conn.close()
    log.info(f'Trade recorded: id={trade_id} {signal.get("symbol")} {strategy} '
             f'{"debit" if is_debit else "credit"}={abs(credit):.2f} x{contracts}')
    return trade_id


def close_trade_db(trade_id, reason, pnl,
                   hit_target=0, was_stopped=0,
                   count_for_guardrail=True,
                   exit_spot=None, exit_value=None, dte_at_close=None,
                   exit_vix=None, exit_ivr=None,
                   exit_delta=None, exit_iv=None, close_reasoning=None):
    """Mark a trade as closed, compute net P&L after commission, update guardrails."""
    conn   = get_conn()
    opened = conn.execute(
        'SELECT opened_at FROM trades WHERE id=?', (trade_id,)
    ).fetchone()

    days_held = 0
    if opened:
        try:
            od = datetime.fromisoformat(opened[0])
            days_held = (datetime.now(ET) - od).days
        except Exception:
            pass

    # Subtract commission from gross P&L (commission stored at open)
    stored = conn.execute(
        'SELECT commission, capital_used FROM trades WHERE id=?', (trade_id,)
    ).fetchone()
    commission = float(stored[0] or 0) if stored else 0.0
    net_pnl    = round(pnl - commission, 2)

    pnl_pct = 0.0
    if stored and stored[1]:
        cap = float(stored[1])
        if cap > 0:
            pnl_pct = round((net_pnl / cap) * 100, 1)

    conn.execute('''
        UPDATE trades SET
            status=?, closed_at=?, close_reason=?,
            days_held=?, pnl=?, pnl_pct=?,
            hit_profit_target=?, was_stopped=?,
            exit_spot=?, exit_value=?, dte_at_close=?,
            exit_vix=?, exit_ivr=?, exit_delta=?, exit_iv=?,
            close_reasoning=?
        WHERE id=?
    ''', (
        'closed', datetime.now(ET).isoformat(), reason,
        days_held, net_pnl, pnl_pct,
        hit_target, was_stopped,
        exit_spot, exit_value, dte_at_close,
        exit_vix, exit_ivr, exit_delta, exit_iv,
        close_reasoning,
        trade_id,
    ))
    conn.commit()
    conn.close()

    if not count_for_guardrail:
        return

    # ── Guardrails — consecutive counters + drawdown checks ───────
    wins   = int(get_state('consecutive_wins')   or 0)
    losses = int(get_state('consecutive_losses') or 0)
    if net_pnl and net_pnl > 0:
        wins += 1
        set_state('consecutive_wins',   wins)
        set_state('consecutive_losses', 0)
        if wins >= 3:
            set_state('consecutive_wins', 0)
    else:
        losses += 1
        set_state('consecutive_losses', losses)
        set_state('consecutive_wins',   0)
        log.info(f'Consecutive losses: {losses}')

    # Weekly drawdown check (>10% of balance → pause until Monday)
    monday    = date.today() - timedelta(days=date.today().weekday())
    conn2     = get_conn()
    wk_rows   = conn2.execute(
        'SELECT pnl FROM trades WHERE opened_at >= ? AND status=? AND pnl IS NOT NULL',
        (monday.isoformat(), 'closed')
    ).fetchall()
    conn2.close()
    weekly_pnl = sum(r[0] for r in wk_rows if r[0])
    balance    = get_current_balance()
    if weekly_pnl < -(balance * 0.10):
        set_state('weekly_pause',   'true')
        set_state('kill_alert_ack', 'false')
        log.warning(f'Weekly drawdown ${weekly_pnl:.2f} exceeds 10% — paused until Monday')

    # Monthly drawdown check (>20% → 7-day freeze)
    month_start = date.today().replace(day=1)
    conn3       = get_conn()
    mo_rows     = conn3.execute(
        'SELECT pnl FROM trades WHERE opened_at >= ? AND status=? AND pnl IS NOT NULL',
        (month_start.isoformat(), 'closed')
    ).fetchall()
    conn3.close()
    monthly_pnl = sum(r[0] for r in mo_rows if r[0])
    if monthly_pnl < -(balance * 0.20):
        resume = (datetime.now(ET) + timedelta(days=7)).isoformat()
        set_state('monthly_pause',       'true')
        set_state('monthly_pause_until', resume)
        set_state('kill_alert_ack',      'false')
        log.warning(f'Monthly drawdown ${monthly_pnl:.2f} exceeds 20% — 7-day freeze')


def mark_externally_closed(trade_id: int) -> None:
    """Mark a trade closed by external action (manual or assignment). P&L left NULL."""
    conn = get_conn()
    conn.execute(
        '''UPDATE trades SET status=?, closed_at=?, close_reason=?,
           was_stopped=0, hit_profit_target=0 WHERE id=?''',
        ('closed', datetime.now(ET).isoformat(), 'external_close', trade_id)
    )
    conn.commit()
    conn.close()


def mark_as_rolled(trade_id: int, new_trade_id: int) -> None:
    """Mark original trade as rolled and link it to the new trade record."""
    conn = get_conn()
    conn.execute(
        '''UPDATE trades SET status=?, closed_at=?, close_reason=?, rolled_to_id=?
           WHERE id=?''',
        ('rolled', datetime.now(ET).isoformat(), 'rolled', new_trade_id, trade_id)
    )
    conn.commit()
    conn.close()


def get_roll_count(trade_id: int) -> int:
    conn = get_conn()
    row  = conn.execute('SELECT roll_count FROM trades WHERE id=?', (trade_id,)).fetchone()
    conn.close()
    return int(row[0] or 0) if row else 0


def set_oco_order_id(trade_id: int, oco_id) -> None:
    conn = get_conn()
    conn.execute('UPDATE trades SET oco_order_id=? WHERE id=?', (oco_id, trade_id))
    conn.commit()
    conn.close()


def get_oco_order_id(trade_id: int):
    conn = get_conn()
    row  = conn.execute('SELECT oco_order_id FROM trades WHERE id=?', (trade_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def get_trade_by_id(trade_id: int):
    conn = get_conn()
    row  = conn.execute('SELECT * FROM trades WHERE id=?', (trade_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_trade_last_spot(trade_id: int, spot: float):
    """Update rolling fallback spot price (not entry price — never used for stops)."""
    conn = get_conn()
    conn.execute('UPDATE trades SET last_spot_price=? WHERE id=?', (round(spot, 2), trade_id))
    conn.commit()
    conn.close()


def update_trade_last_spread(trade_id: int, val: float):
    """Update rolling fallback spread mid (display only — never triggers stops)."""
    conn = get_conn()
    conn.execute('UPDATE trades SET last_spread_value=? WHERE id=?', (round(val, 4), trade_id))
    conn.commit()
    conn.close()


# ── Portfolio Queries ───────────────────────────────────────────────

def get_open_trades():
    conn = get_conn()
    rows = conn.execute('SELECT * FROM trades WHERE status=?', ('open',)).fetchall()
    conn.close()
    return rows


def get_open_trade_count():
    conn  = get_conn()
    count = conn.execute('SELECT COUNT(*) FROM trades WHERE status=?', ('open',)).fetchone()[0]
    conn.close()
    return count


def get_deployed_capital():
    """Sum of capital_used for all open trades (BPR proxy)."""
    conn   = get_conn()
    result = conn.execute(
        "SELECT COALESCE(SUM(capital_used), 0) FROM trades WHERE status='open'"
    ).fetchone()[0]
    conn.close()
    return round(float(result), 2)


def get_open_max_loss():
    """Worst-case loss across all open trades (sum of max_loss columns)."""
    conn   = get_conn()
    result = conn.execute(
        "SELECT COALESCE(SUM(max_loss), 0) FROM trades WHERE status='open'"
    ).fetchone()[0]
    conn.close()
    return round(float(result), 2)


def trades_this_week():
    conn   = get_conn()
    monday = date.today() - timedelta(days=date.today().weekday())
    count  = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE opened_at >= ? AND status != 'cancelled'",
        (monday.isoformat(),)
    ).fetchone()[0]
    conn.close()
    return count


def symbols_today() -> set:
    """Return set of symbols that already have a trade opened today (dedup gate)."""
    conn  = get_conn()
    today = date.today().isoformat()
    rows  = conn.execute(
        "SELECT symbol FROM trades WHERE date(opened_at) = ? AND status != 'cancelled'",
        (today,)
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


# ── Guardrails ──────────────────────────────────────────────────────

def check_guardrails():
    """Returns (can_trade: bool, reason: str). Call at start of every scan."""
    from config import MAX_CAPITAL_DEPLOYED
    now     = datetime.now(ET)
    balance = get_current_balance()

    # Kill switch
    if balance <= float(KILL_SWITCH):
        return False, f'KILL SWITCH: balance ${balance} at or below ${KILL_SWITCH}'

    # Capital deployment ceiling
    deployed = get_deployed_capital()
    if balance > 0 and (deployed / balance) >= MAX_CAPITAL_DEPLOYED:
        pct = deployed / balance * 100
        return False, (f'CAPITAL CEILING: {pct:.1f}% deployed '
                       f'(${deployed:,.0f} / ${balance:,.0f}) — '
                       f'ceiling {MAX_CAPITAL_DEPLOYED*100:.0f}%')

    # Manual pause
    if get_state('paused') == 'true':
        return False, 'PAUSED: manual pause active'

    # Weekly drawdown (>10% → pause until Monday)
    if get_state('weekly_pause') == 'true':
        days_left = (7 - date.today().weekday()) % 7
        if days_left == 0 and now.hour >= 0:
            set_state('weekly_pause', 'false')
            log.info('Weekly pause cleared — new week')
        else:
            return False, 'WEEKLY DRAWDOWN >10%: paused until Monday open'

    # Monthly drawdown (>20% → 7-day freeze)
    if get_state('monthly_pause') == 'true':
        monthly_until = get_state('monthly_pause_until') or ''
        if monthly_until:
            try:
                resume_dt = datetime.fromisoformat(monthly_until)
                if now >= resume_dt:
                    set_state('monthly_pause',       'false')
                    set_state('monthly_pause_until', '')
                    log.info('Monthly pause expired — trading resumed')
                else:
                    days_left = (resume_dt - now).days
                    return False, f'MONTHLY DRAWDOWN >20%: paused {days_left} more days'
            except Exception:
                set_state('monthly_pause', 'false')

    # Peak drawdown guard
    peak_str = get_state('balance_peak')
    if peak_str:
        try:
            peak = float(peak_str)
            if peak > 0:
                drop_pct = (peak - balance) / peak * 100
                if drop_pct >= 20.0:
                    resume = (now + timedelta(days=7)).isoformat()
                    set_state('monthly_pause',       'true')
                    set_state('monthly_pause_until', resume)
                    log.warning(f'Peak drawdown freeze: -{drop_pct:.1f}% from peak ${peak:,.2f}')
                    return False, f'PEAK DRAWDOWN -{drop_pct:.1f}%: 7-day freeze'
                elif drop_pct >= 12.0:
                    resume = (now + timedelta(hours=48)).isoformat()
                    set_state('monthly_pause',       'true')
                    set_state('monthly_pause_until', resume)
                    log.warning(f'Peak drawdown pause: -{drop_pct:.1f}% from peak ${peak:,.2f}')
                    return False, f'PEAK DRAWDOWN -{drop_pct:.1f}%: 48h pause'
        except Exception:
            pass

    return True, 'ok'


def get_kill_alert_pending():
    return get_state('kill_alert_ack') == 'false'


def ack_kill_alert():
    set_state('kill_alert_ack', 'true')


# ── Performance ─────────────────────────────────────────────────────

def get_weekly_stats():
    conn   = get_conn()
    monday = date.today() - timedelta(days=date.today().weekday())
    rows   = conn.execute(
        'SELECT pnl, status, strategy, symbol FROM trades WHERE opened_at >= ?',
        (monday.isoformat(),)
    ).fetchall()
    closed_rows = conn.execute(
        'SELECT pnl, status, strategy, symbol FROM trades WHERE status=? AND closed_at >= ?',
        ('closed', monday.isoformat())
    ).fetchall()
    conn.close()

    open_rows = [r for r in rows if r[1] != 'closed']
    closed    = [r for r in closed_rows if r[0] is not None]
    winners   = [r for r in closed if r[0] > 0]
    losers    = [r for r in closed if r[0] <= 0]
    return {
        'total':     len(open_rows) + len(closed),
        'closed':    len(closed),
        'winners':   len(winners),
        'losers':    len(losers),
        'total_pnl': round(sum(r[0] for r in closed), 2),
        'win_rate':  round(len(winners) / len(closed) * 100, 1) if closed else 0.0,
    }


def get_performance_summary():
    conn        = get_conn()
    all_closed  = conn.execute(
        'SELECT pnl, strategy, symbol, days_held, closed_at FROM trades WHERE status=?',
        ('closed',)
    ).fetchall()

    today      = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_str  = today.strftime('%Y-%m')

    week_pnl  = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='closed' AND pnl IS NOT NULL "
        "AND date(closed_at) >= ?",
        (week_start.isoformat(),)
    ).fetchone()[0]
    month_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='closed' AND pnl IS NOT NULL "
        "AND strftime('%Y-%m', closed_at) = ?",
        (month_str,)
    ).fetchone()[0]
    best_trade  = conn.execute(
        "SELECT pnl, symbol, strategy FROM trades WHERE status='closed' AND pnl IS NOT NULL "
        "ORDER BY pnl DESC LIMIT 1"
    ).fetchone()
    worst_trade = conn.execute(
        "SELECT pnl, symbol, strategy FROM trades WHERE status='closed' AND pnl IS NOT NULL "
        "ORDER BY pnl ASC LIMIT 1"
    ).fetchone()
    conn.close()

    if not all_closed:
        return None

    winners   = [r for r in all_closed if r[0] and r[0] > 0]
    losers    = [r for r in all_closed if r[0] and r[0] <= 0]
    total_pnl = sum(r[0] for r in all_closed if r[0])

    # Strategy breakdown
    strategies = {}
    for r in all_closed:
        strat = r[1] or 'unknown'
        if strat not in strategies:
            strategies[strat] = {'count': 0, 'wins': 0, 'pnl': 0.0}
        strategies[strat]['count'] += 1
        if r[0]:
            strategies[strat]['pnl'] += r[0]
            if r[0] > 0:
                strategies[strat]['wins'] += 1
    for s in strategies.values():
        s['pnl']      = round(s['pnl'], 2)
        s['win_rate'] = round(s['wins'] / s['count'] * 100, 1) if s['count'] else 0.0

    return {
        'total':        len(all_closed),
        'winners':      len(winners),
        'losers':       len(losers),
        'win_rate':     round(len(winners) / len(all_closed) * 100, 1),
        'total_pnl':    round(total_pnl, 2),
        'avg_pnl':      round(total_pnl / len(all_closed), 2),
        'week_pnl':     round(float(week_pnl),  2),
        'month_pnl':    round(float(month_pnl), 2),
        'best_trade':   {'pnl': best_trade[0],  'symbol': best_trade[1],  'strategy': best_trade[2]}  if best_trade  else None,
        'worst_trade':  {'pnl': worst_trade[0], 'symbol': worst_trade[1], 'strategy': worst_trade[2]} if worst_trade else None,
        'by_strategy':  strategies,
    }
