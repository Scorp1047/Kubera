"""
tasty.py — Kubera TastyTrade order placement and market data layer

Carried from Nexus X tasty.py with the following changes:
  - Logger: 'kubera'
  - Removed: tt_get_option_market_data (uses /market-data/by-type → 403 blocked endpoint)
  - Added:   tt_place_jade_lizard    (3-leg: short put + short call + long call wing)
  - Added:   tt_close_jade_lizard    (3-leg close)
  - Added:   tt_place_debit_spread   (buy closer strike, sell further OTM — DEBIT)
  - Added:   tt_close_debit_spread   (reverse legs of a debit spread)
  - Added:   tt_place_calendar_spread (same strike, two expiries — DEBIT)
  - Added:   tt_close_calendar_spread (close calendar: sell near short, buy far long back)
"""

import asyncio
import logging
import pandas as pd
from decimal import Decimal
from datetime import date
from tastytrade import Session, Account
from tastytrade.instruments import get_option_chain
from tastytrade.order import (
    NewOrder, OrderAction, OrderTimeInForce,
    OrderType, PriceEffect
)
from tastytrade.utils import get_tasty_monthly

log = logging.getLogger('kubera')

TT_SESSION  = None
TT_ACCOUNT  = None
_TT_CREDS   = {}   # stored at tt_connect() time; used for auto-reconnect on session loss


async def _tt_guard():
    """Ensure TT session is live and token is fresh before any API call.
    On refresh failure: attempts auto-reconnect using stored credentials.
    Raises RuntimeError if session cannot be recovered."""
    global TT_SESSION, TT_ACCOUNT
    if TT_SESSION is None or TT_ACCOUNT is None:
        raise RuntimeError('TT session not initialised — call tt_connect first')
    try:
        await TT_SESSION.refresh()
    except Exception as _e:
        log.warning(f'TT token refresh failed ({_e}) — attempting reconnect')
        if not _TT_CREDS:
            raise RuntimeError('TT session lost — no stored credentials for reconnect') from _e
        ok = await tt_connect(**_TT_CREDS)
        if not ok:
            raise RuntimeError('TT session lost — reconnect failed. Re-auth required.') from _e
        log.info('TT session auto-reconnected')


async def tt_connect(provider_secret, refresh_token, account_number, is_test=True):
    global TT_SESSION, TT_ACCOUNT, _TT_CREDS
    try:
        TT_SESSION = Session(
            provider_secret=provider_secret,
            refresh_token=refresh_token,
            is_test=is_test,
            timeout=60.0
        )
        accounts   = await Account.get(TT_SESSION)
        TT_ACCOUNT = next(a for a in accounts if a.account_number == account_number)
        _TT_CREDS  = {
            'provider_secret': provider_secret,
            'refresh_token':   refresh_token,
            'account_number':  account_number,
            'is_test':         is_test,
        }
        log.info(f'TastyTrade connected: {account_number} | sandbox={is_test}')
        return True
    except Exception as e:
        log.error(f'TastyTrade connect failed: {e}')
        return False


async def tt_get_balance() -> float:
    try:
        await TT_SESSION.refresh()
        bal = await TT_ACCOUNT.get_balances(TT_SESSION)
        return float(bal.net_liquidating_value)
    except Exception as e:
        log.error(f'Balance fetch failed: {e}')
        return None


async def tt_get_positions() -> list:
    try:
        positions = await TT_ACCOUNT.get_positions(TT_SESSION)
        result = []
        for p in positions:
            result.append({
                'symbol':             p.symbol,
                'underlying_symbol':  p.underlying_symbol,
                'quantity':           float(p.quantity),
                'direction':          p.quantity_direction,
                'average_open_price': float(p.average_open_price) if p.average_open_price else 0.0,
                'instrument_type':    p.instrument_type.value if p.instrument_type else '',
            })
        return result
    except Exception as e:
        log.error(f'Positions fetch failed: {e}')
        return []


async def tt_get_orders() -> list:
    try:
        orders = await TT_ACCOUNT.get_live_orders(TT_SESSION)
        return orders
    except Exception as e:
        log.error(f'Orders fetch failed: {e}')
        return []


async def tt_find_option_symbols(symbol: str, expiry: date, sell_strike: float,
                                  buy_strike: float, option_type: str) -> tuple:
    try:
        chain   = await get_option_chain(TT_SESSION, symbol)
        if expiry not in chain:
            available = sorted(chain.keys())
            expiry    = min(available, key=lambda d: abs((d - expiry).days))
            log.warning(f'Expiry not found, using closest: {expiry}')
        options  = chain[expiry]
        opt_type = option_type.upper()[0]
        filtered = [o for o in options if o.option_type.value == opt_type]
        sell_opt = next((o for o in filtered if float(o.strike_price) == sell_strike), None)
        buy_opt  = next((o for o in filtered if float(o.strike_price) == buy_strike),  None)
        if not sell_opt or not buy_opt:
            log.error(f'Strikes not found: sell={sell_strike} buy={buy_strike} {symbol} {expiry}')
            return None, None
        return sell_opt, buy_opt
    except Exception as e:
        log.error(f'Option symbol lookup failed: {e}')
        return None, None


# ── Credit Spreads ──────────────────────────────────────────────────

async def tt_place_credit_spread(symbol: str, expiry: date,
                                  sell_strike: float, buy_strike: float,
                                  option_type: str, credit: float,
                                  contracts: int = 1,
                                  dry_run: bool = True) -> dict:
    try:
        await _tt_guard()
        sell_opt, buy_opt = await tt_find_option_symbols(
            symbol, expiry, sell_strike, buy_strike, option_type
        )
        if not sell_opt or not buy_opt:
            msg = f'Strikes not found: sell={sell_strike} buy={buy_strike} {symbol} {expiry}'
            log.error(msg)
            return {'error': msg, 'status': 'FAILED'}

        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            price=Decimal(str(round(credit, 2))),
            price_effect=PriceEffect.CREDIT,
            legs=[
                sell_opt.build_leg(contracts, OrderAction.SELL_TO_OPEN),
                buy_opt.build_leg(contracts,  OrderAction.BUY_TO_OPEN),
            ]
        )
        response = await TT_ACCOUNT.place_order(TT_SESSION, order, dry_run=dry_run)
        placed   = response.order
        log.info(f'Credit spread placed: {symbol} {option_type} | '
                 f'sell={sell_strike} buy={buy_strike} @ {credit} x{contracts} | '
                 f'dry_run={dry_run} | status={placed.status}')
        return {
            'order_id':  getattr(placed, 'id', None),
            'status':    str(placed.status),
            'symbol':    symbol,
            'expiry':    str(expiry),
            'sell':      sell_strike,
            'buy':       buy_strike,
            'type':      option_type,
            'credit':    credit,
            'contracts': contracts,
            'dry_run':   dry_run,
        }
    except Exception as e:
        log.error(f'Credit spread placement failed: {e}')
        return {'error': str(e), 'status': 'FAILED'}


# ── Iron Condor ─────────────────────────────────────────────────────

async def tt_place_iron_condor(symbol: str, expiry: date,
                                sell_put: float, buy_put: float,
                                sell_call: float, buy_call: float,
                                total_credit: float,
                                contracts: int = 1,
                                dry_run: bool = True) -> dict:
    """Place a 4-leg iron condor as a single TT order.
    Legs: SELL PUT / BUY PUT / SELL CALL / BUY CALL.
    Price = total_credit (combined net credit across both spreads)."""
    try:
        await _tt_guard()
        chain = await get_option_chain(TT_SESSION, symbol)
        if expiry not in chain:
            available = sorted(chain.keys())
            expiry    = min(available, key=lambda d: abs((d - expiry).days))
            log.warning(f'IC expiry not found, using closest: {expiry}')
        options = chain[expiry]
        puts    = [o for o in options if o.option_type.value == 'P']
        calls   = [o for o in options if o.option_type.value == 'C']

        def _find(opts, strike):
            return next((o for o in opts if abs(float(o.strike_price) - strike) < 0.01), None)

        sell_put_opt  = _find(puts,  sell_put)
        buy_put_opt   = _find(puts,  buy_put)
        sell_call_opt = _find(calls, sell_call)
        buy_call_opt  = _find(calls, buy_call)

        missing = []
        if not sell_put_opt:  missing.append(f'sell_put={sell_put}')
        if not buy_put_opt:   missing.append(f'buy_put={buy_put}')
        if not sell_call_opt: missing.append(f'sell_call={sell_call}')
        if not buy_call_opt:  missing.append(f'buy_call={buy_call}')
        if missing:
            msg = f'IC strikes not found: {missing} — {symbol} {expiry}'
            log.error(msg)
            return {'error': msg, 'status': 'FAILED'}

        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            price=Decimal(str(round(total_credit, 2))),
            price_effect=PriceEffect.CREDIT,
            legs=[
                sell_put_opt.build_leg(contracts,  OrderAction.SELL_TO_OPEN),
                buy_put_opt.build_leg(contracts,   OrderAction.BUY_TO_OPEN),
                sell_call_opt.build_leg(contracts, OrderAction.SELL_TO_OPEN),
                buy_call_opt.build_leg(contracts,  OrderAction.BUY_TO_OPEN),
            ]
        )
        response = await TT_ACCOUNT.place_order(TT_SESSION, order, dry_run=dry_run)
        placed   = response.order
        log.info(f'IC order placed: {symbol} | '
                 f'put={sell_put}/{buy_put} call={sell_call}/{buy_call} @ {total_credit} x{contracts} | '
                 f'dry_run={dry_run} | status={placed.status}')
        return {
            'order_id':    getattr(placed, 'id', None),
            'status':      str(placed.status),
            'symbol':      symbol,
            'expiry':      str(expiry),
            'sell_put':    sell_put,
            'buy_put':     buy_put,
            'sell_call':   sell_call,
            'buy_call':    buy_call,
            'credit':      total_credit,
            'contracts':   contracts,
            'dry_run':     dry_run,
        }
    except Exception as e:
        log.error(f'IC order placement failed: {e}')
        return {'error': str(e), 'status': 'FAILED'}


async def tt_close_iron_condor(symbol: str, expiry: date,
                                sell_put: float, buy_put: float,
                                sell_call: float, buy_call: float,
                                debit: float,
                                contracts: int = 1,
                                dry_run: bool = False) -> dict:
    """Close a 4-leg iron condor: BUY_TO_CLOSE short legs, SELL_TO_CLOSE long legs."""
    try:
        await _tt_guard()
        chain = await get_option_chain(TT_SESSION, symbol)
        if expiry not in chain:
            available = sorted(chain.keys())
            expiry    = min(available, key=lambda d: abs((d - expiry).days))
            log.warning(f'IC close expiry adjusted to: {expiry}')
        options = chain[expiry]
        puts    = [o for o in options if o.option_type.value == 'P']
        calls   = [o for o in options if o.option_type.value == 'C']

        def _find(opts, strike):
            return next((o for o in opts if abs(float(o.strike_price) - strike) < 0.01), None)

        sell_put_opt  = _find(puts,  sell_put)
        buy_put_opt   = _find(puts,  buy_put)
        sell_call_opt = _find(calls, sell_call)
        buy_call_opt  = _find(calls, buy_call)

        missing = []
        if not sell_put_opt:  missing.append(f'sell_put={sell_put}')
        if not buy_put_opt:   missing.append(f'buy_put={buy_put}')
        if not sell_call_opt: missing.append(f'sell_call={sell_call}')
        if not buy_call_opt:  missing.append(f'buy_call={buy_call}')
        if missing:
            msg = f'IC close strikes not found: {missing} — {symbol} {expiry}'
            log.error(msg)
            return {'error': msg, 'status': 'FAILED'}

        # Round to nearest $0.05 tick — TT rejects multi-leg combo orders at arbitrary decimals
        _debit_tick = max(0.01, round(round(debit / 0.05) * 0.05, 2))
        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            price=Decimal(str(_debit_tick)),
            price_effect=PriceEffect.DEBIT,
            legs=[
                sell_put_opt.build_leg(contracts,  OrderAction.BUY_TO_CLOSE),
                buy_put_opt.build_leg(contracts,   OrderAction.SELL_TO_CLOSE),
                sell_call_opt.build_leg(contracts, OrderAction.BUY_TO_CLOSE),
                buy_call_opt.build_leg(contracts,  OrderAction.SELL_TO_CLOSE),
            ]
        )
        response = await TT_ACCOUNT.place_order(TT_SESSION, order, dry_run=dry_run)
        placed   = response.order
        log.info(f'IC close placed: {symbol} | '
                 f'put={sell_put}/{buy_put} call={sell_call}/{buy_call} @ {debit} x{contracts} | '
                 f'dry_run={dry_run} | status={placed.status}')
        return {'order_id': getattr(placed, 'id', None), 'status': str(placed.status)}
    except Exception as e:
        log.error(f'IC close position failed: {e}')
        return {'error': str(e), 'status': 'FAILED'}


# ── Jade Lizard ────────────────────────────────────────────────────
# Structure: SELL OTM put + SELL OTM call + BUY further OTM call (call spread wing)
# Entry rule: total credit ≥ call spread width → zero upside risk
# Source: tastylive.com/definitions/jade-lizard

async def tt_place_jade_lizard(symbol: str, expiry: date,
                                sell_put: float,
                                sell_call: float, buy_call: float,
                                total_credit: float,
                                contracts: int = 1,
                                dry_run: bool = True) -> dict:
    """Place a 3-leg Jade Lizard: sell OTM put + sell OTM call + buy further OTM call.
    total_credit = net credit across all 3 legs.
    Prerequisite: total_credit ≥ (buy_call − sell_call) i.e. call spread width → zero upside risk."""
    try:
        await _tt_guard()
        chain = await get_option_chain(TT_SESSION, symbol)
        if expiry not in chain:
            available = sorted(chain.keys())
            expiry    = min(available, key=lambda d: abs((d - expiry).days))
            log.warning(f'Jade Lizard expiry adjusted to: {expiry}')
        options = chain[expiry]
        puts    = [o for o in options if o.option_type.value == 'P']
        calls   = [o for o in options if o.option_type.value == 'C']

        def _find(opts, strike):
            return next((o for o in opts if abs(float(o.strike_price) - strike) < 0.01), None)

        sp_opt  = _find(puts,  sell_put)
        sc_opt  = _find(calls, sell_call)
        bc_opt  = _find(calls, buy_call)

        missing = []
        if not sp_opt:  missing.append(f'sell_put={sell_put}')
        if not sc_opt:  missing.append(f'sell_call={sell_call}')
        if not bc_opt:  missing.append(f'buy_call={buy_call}')
        if missing:
            msg = f'Jade Lizard strikes not found: {missing} — {symbol} {expiry}'
            log.error(msg)
            return {'error': msg, 'status': 'FAILED'}

        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            price=Decimal(str(round(total_credit, 2))),
            price_effect=PriceEffect.CREDIT,
            legs=[
                sp_opt.build_leg(contracts, OrderAction.SELL_TO_OPEN),   # short put
                sc_opt.build_leg(contracts, OrderAction.SELL_TO_OPEN),   # short call
                bc_opt.build_leg(contracts, OrderAction.BUY_TO_OPEN),    # long call wing
            ]
        )
        response = await TT_ACCOUNT.place_order(TT_SESSION, order, dry_run=dry_run)
        placed   = response.order
        log.info(f'Jade Lizard placed: {symbol} | '
                 f'put={sell_put} call={sell_call}/{buy_call} @ {total_credit} x{contracts} | '
                 f'dry_run={dry_run} | status={placed.status}')
        return {
            'order_id':    getattr(placed, 'id', None),
            'status':      str(placed.status),
            'symbol':      symbol,
            'expiry':      str(expiry),
            'sell_put':    sell_put,
            'sell_call':   sell_call,
            'buy_call':    buy_call,
            'credit':      total_credit,
            'contracts':   contracts,
            'dry_run':     dry_run,
        }
    except Exception as e:
        log.error(f'Jade Lizard placement failed: {e}')
        return {'error': str(e), 'status': 'FAILED'}


async def tt_close_jade_lizard(symbol: str, expiry: date,
                                sell_put: float,
                                sell_call: float, buy_call: float,
                                debit: float,
                                contracts: int = 1,
                                dry_run: bool = False) -> dict:
    """Close a Jade Lizard: BUY_TO_CLOSE short put + BUY_TO_CLOSE short call + SELL_TO_CLOSE long call."""
    try:
        await _tt_guard()
        chain = await get_option_chain(TT_SESSION, symbol)
        if expiry not in chain:
            available = sorted(chain.keys())
            expiry    = min(available, key=lambda d: abs((d - expiry).days))
            log.warning(f'Jade Lizard close expiry adjusted to: {expiry}')
        options = chain[expiry]
        puts    = [o for o in options if o.option_type.value == 'P']
        calls   = [o for o in options if o.option_type.value == 'C']

        def _find(opts, strike):
            return next((o for o in opts if abs(float(o.strike_price) - strike) < 0.01), None)

        sp_opt  = _find(puts,  sell_put)
        sc_opt  = _find(calls, sell_call)
        bc_opt  = _find(calls, buy_call)

        missing = []
        if not sp_opt:  missing.append(f'sell_put={sell_put}')
        if not sc_opt:  missing.append(f'sell_call={sell_call}')
        if not bc_opt:  missing.append(f'buy_call={buy_call}')
        if missing:
            msg = f'Jade Lizard close strikes not found: {missing} — {symbol} {expiry}'
            log.error(msg)
            return {'error': msg, 'status': 'FAILED'}

        # Round to nearest $0.05 tick for multi-leg orders
        _debit_tick = max(0.01, round(round(debit / 0.05) * 0.05, 2))
        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            price=Decimal(str(_debit_tick)),
            price_effect=PriceEffect.DEBIT,
            legs=[
                sp_opt.build_leg(contracts, OrderAction.BUY_TO_CLOSE),   # close short put
                sc_opt.build_leg(contracts, OrderAction.BUY_TO_CLOSE),   # close short call
                bc_opt.build_leg(contracts, OrderAction.SELL_TO_CLOSE),  # close long call wing
            ]
        )
        response = await TT_ACCOUNT.place_order(TT_SESSION, order, dry_run=dry_run)
        placed   = response.order
        log.info(f'Jade Lizard close placed: {symbol} | '
                 f'put={sell_put} call={sell_call}/{buy_call} @ {debit} x{contracts} | '
                 f'dry_run={dry_run} | status={placed.status}')
        return {'order_id': getattr(placed, 'id', None), 'status': str(placed.status)}
    except Exception as e:
        log.error(f'Jade Lizard close failed: {e}')
        return {'error': str(e), 'status': 'FAILED'}


# ── Debit Spreads ───────────────────────────────────────────────────

async def tt_place_debit_spread(symbol: str, expiry: date,
                                 buy_strike: float, sell_strike: float,
                                 option_type: str, debit: float,
                                 contracts: int = 1,
                                 dry_run: bool = True) -> dict:
    """Place a debit spread: BUY closer-to-money strike + SELL further OTM strike.
    Used in LOW IVR regime when directional bias is confirmed.
    buy_strike:  the 40-delta leg (closer to money, paid for)
    sell_strike: the further OTM wing (sold to offset cost)
    option_type: 'call' (bullish) or 'put' (bearish)"""
    try:
        await _tt_guard()
        chain = await get_option_chain(TT_SESSION, symbol)
        if expiry not in chain:
            available = sorted(chain.keys())
            expiry    = min(available, key=lambda d: abs((d - expiry).days))
            log.warning(f'Debit spread expiry adjusted to: {expiry}')
        options  = chain[expiry]
        opt_type = option_type.upper()[0]
        filtered = [o for o in options if o.option_type.value == opt_type]

        def _find(strike):
            return next((o for o in filtered if abs(float(o.strike_price) - strike) < 0.01), None)

        buy_opt  = _find(buy_strike)
        sell_opt = _find(sell_strike)

        missing = []
        if not buy_opt:  missing.append(f'buy={buy_strike}')
        if not sell_opt: missing.append(f'sell={sell_strike}')
        if missing:
            msg = f'Debit spread strikes not found: {missing} — {symbol} {expiry} {option_type}'
            log.error(msg)
            return {'error': msg, 'status': 'FAILED'}

        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            price=Decimal(str(round(debit, 2))),
            price_effect=PriceEffect.DEBIT,
            legs=[
                buy_opt.build_leg(contracts,  OrderAction.BUY_TO_OPEN),   # long leg (paid)
                sell_opt.build_leg(contracts, OrderAction.SELL_TO_OPEN),  # short wing (offset)
            ]
        )
        response = await TT_ACCOUNT.place_order(TT_SESSION, order, dry_run=dry_run)
        placed   = response.order
        log.info(f'Debit spread placed: {symbol} {option_type} | '
                 f'buy={buy_strike} sell={sell_strike} @ {debit} x{contracts} | '
                 f'dry_run={dry_run} | status={placed.status}')
        return {
            'order_id':  getattr(placed, 'id', None),
            'status':    str(placed.status),
            'symbol':    symbol,
            'expiry':    str(expiry),
            'buy':       buy_strike,
            'sell':      sell_strike,
            'type':      option_type,
            'debit':     debit,
            'contracts': contracts,
            'dry_run':   dry_run,
        }
    except Exception as e:
        log.error(f'Debit spread placement failed: {e}')
        return {'error': str(e), 'status': 'FAILED'}


async def tt_close_debit_spread(symbol: str, expiry: date,
                                 buy_strike: float, sell_strike: float,
                                 option_type: str, credit: float,
                                 contracts: int = 1,
                                 dry_run: bool = False) -> dict:
    """Close a debit spread: SELL_TO_CLOSE the long leg + BUY_TO_CLOSE the short wing.
    credit = amount received to close (what's left of the spread's value)."""
    try:
        await _tt_guard()
        chain = await get_option_chain(TT_SESSION, symbol)
        if expiry not in chain:
            available = sorted(chain.keys())
            expiry    = min(available, key=lambda d: abs((d - expiry).days))
        options  = chain[expiry]
        opt_type = option_type.upper()[0]
        filtered = [o for o in options if o.option_type.value == opt_type]

        def _find(strike):
            return next((o for o in filtered if abs(float(o.strike_price) - strike) < 0.01), None)

        buy_opt  = _find(buy_strike)
        sell_opt = _find(sell_strike)

        missing = []
        if not buy_opt:  missing.append(f'buy={buy_strike}')
        if not sell_opt: missing.append(f'sell={sell_strike}')
        if missing:
            msg = f'Debit spread close strikes not found: {missing} — {symbol} {expiry}'
            log.error(msg)
            return {'error': msg, 'status': 'FAILED'}

        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            price=Decimal(str(round(credit, 2))),
            price_effect=PriceEffect.CREDIT,
            legs=[
                buy_opt.build_leg(contracts,  OrderAction.SELL_TO_CLOSE),  # sell the long leg
                sell_opt.build_leg(contracts, OrderAction.BUY_TO_CLOSE),   # buy back the wing
            ]
        )
        response = await TT_ACCOUNT.place_order(TT_SESSION, order, dry_run=dry_run)
        placed   = response.order
        log.info(f'Debit spread close placed: {symbol} {option_type} | '
                 f'buy={buy_strike} sell={sell_strike} @ {credit} credit x{contracts} | '
                 f'dry_run={dry_run} | status={placed.status}')
        return {'order_id': getattr(placed, 'id', None), 'status': str(placed.status)}
    except Exception as e:
        log.error(f'Debit spread close failed: {e}')
        return {'error': str(e), 'status': 'FAILED'}


# ── Calendar Spread ─────────────────────────────────────────────────
# Structure: SELL near-term expiry + BUY far-term expiry, same strike (ATM)
# Used in LOW IVR + quiet environment to harvest theta differential.
# Source: tastylive.com — calendar spread study (IV expansion profits long vega)

async def tt_place_calendar_spread(symbol: str,
                                    near_expiry: date, far_expiry: date,
                                    strike: float,
                                    option_type: str,
                                    debit: float,
                                    contracts: int = 1,
                                    dry_run: bool = True) -> dict:
    """Place a calendar spread: SELL near expiry strike + BUY far expiry strike.
    Both legs are the same strike and option type (usually call for neutral/slight bullish).
    debit = net cost (far leg costs more due to more time value).
    near_expiry: the short (sold) expiry — collects theta faster
    far_expiry:  the long (bought) expiry — long vega, profits from IV expansion"""
    try:
        await _tt_guard()
        chain = await get_option_chain(TT_SESSION, symbol)
        opt_type = option_type.upper()[0]

        # Resolve near expiry
        if near_expiry not in chain:
            available = sorted(chain.keys())
            near_expiry = min(available, key=lambda d: abs((d - near_expiry).days))
            log.warning(f'Calendar near expiry adjusted to: {near_expiry}')
        # Resolve far expiry
        if far_expiry not in chain:
            available = sorted(chain.keys())
            far_expiry = min(available, key=lambda d: abs((d - far_expiry).days))
            log.warning(f'Calendar far expiry adjusted to: {far_expiry}')

        if near_expiry >= far_expiry:
            msg = f'Calendar: near_expiry {near_expiry} >= far_expiry {far_expiry} — invalid'
            log.error(msg)
            return {'error': msg, 'status': 'FAILED'}

        def _find_at(expiry, strike_target):
            opts = [o for o in chain[expiry] if o.option_type.value == opt_type]
            return next((o for o in opts if abs(float(o.strike_price) - strike_target) < 0.01), None)

        near_opt = _find_at(near_expiry, strike)
        far_opt  = _find_at(far_expiry,  strike)

        missing = []
        if not near_opt: missing.append(f'near={near_expiry}@{strike}')
        if not far_opt:  missing.append(f'far={far_expiry}@{strike}')
        if missing:
            msg = f'Calendar strikes not found: {missing} — {symbol} {option_type}'
            log.error(msg)
            return {'error': msg, 'status': 'FAILED'}

        # Compute actual debit from real bid/ask — the estimate (price * 0.015) is
        # wildly wrong for low-IV ETFs (e.g. TLT: estimate $1.27, real $0.37;
        # HYG: estimate $1.20, real $0.10). TT rejects orders far outside market.
        order_debit = debit  # fallback to passed estimate if greeks fetch fails
        try:
            greeks = await tt_get_greeks_for_options([near_opt, far_opt])
            near_d = greeks.get(near_opt.symbol, {})
            far_d  = greeks.get(far_opt.symbol,  {})
            near_bid = near_d.get('bid', 0)
            near_ask = near_d.get('ask', 0)
            far_bid  = far_d.get('bid',  0)
            far_ask  = far_d.get('ask',  0)
            if near_bid > 0 and far_ask > 0:
                near_mid    = round((near_bid + near_ask) / 2, 2)
                far_mid     = round((far_bid  + far_ask)  / 2, 2)
                order_debit = max(round(far_mid - near_mid, 2), 0.01)
                log.info(f'Calendar {symbol}: real debit=${order_debit:.2f} '
                         f'(near_mid={near_mid:.2f} far_mid={far_mid:.2f}) '
                         f'vs estimate=${debit:.2f}')
            else:
                log.warning(f'Calendar {symbol}: greeks missing bid/ask — using estimate ${debit:.2f}')
        except Exception as _e:
            log.warning(f'Calendar {symbol}: greeks fetch failed ({_e!s:.60}) — using estimate ${debit:.2f}')

        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            price=Decimal(str(order_debit)),
            price_effect=PriceEffect.DEBIT,
            legs=[
                near_opt.build_leg(contracts, OrderAction.SELL_TO_OPEN),  # short near leg
                far_opt.build_leg(contracts,  OrderAction.BUY_TO_OPEN),   # long far leg
            ]
        )
        response = await TT_ACCOUNT.place_order(TT_SESSION, order, dry_run=dry_run)
        placed   = response.order
        log.info(f'Calendar spread placed: {symbol} {option_type} @ {strike} | '
                 f'near={near_expiry} far={far_expiry} @ {order_debit} debit x{contracts} | '
                 f'dry_run={dry_run} | status={placed.status}')
        return {
            'order_id':    getattr(placed, 'id', None),
            'status':      str(placed.status),
            'symbol':      symbol,
            'strike':      strike,
            'near_expiry': str(near_expiry),
            'far_expiry':  str(far_expiry),
            'type':        option_type,
            'debit':       debit,
            'contracts':   contracts,
            'dry_run':     dry_run,
        }
    except Exception as e:
        log.error(f'Calendar spread placement failed: {e}')
        return {'error': str(e), 'status': 'FAILED'}


async def tt_close_calendar_spread(symbol: str,
                                    near_expiry: date, far_expiry: date,
                                    strike: float,
                                    option_type: str,
                                    credit: float,
                                    contracts: int = 1,
                                    dry_run: bool = False) -> dict:
    """Close a calendar spread: BUY_TO_CLOSE short near leg + SELL_TO_CLOSE long far leg.
    credit = net credit received to unwind (far leg worth more than near if held correctly)."""
    try:
        await _tt_guard()
        chain    = await get_option_chain(TT_SESSION, symbol)
        opt_type = option_type.upper()[0]

        if near_expiry not in chain:
            available   = sorted(chain.keys())
            near_expiry = min(available, key=lambda d: abs((d - near_expiry).days))
        if far_expiry not in chain:
            available  = sorted(chain.keys())
            far_expiry = min(available, key=lambda d: abs((d - far_expiry).days))

        def _find_at(expiry, strike_target):
            opts = [o for o in chain[expiry] if o.option_type.value == opt_type]
            return next((o for o in opts if abs(float(o.strike_price) - strike_target) < 0.01), None)

        near_opt = _find_at(near_expiry, strike)
        far_opt  = _find_at(far_expiry,  strike)

        missing = []
        if not near_opt: missing.append(f'near={near_expiry}@{strike}')
        if not far_opt:  missing.append(f'far={far_expiry}@{strike}')
        if missing:
            msg = f'Calendar close strikes not found: {missing} — {symbol} {option_type}'
            log.error(msg)
            return {'error': msg, 'status': 'FAILED'}

        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            price=Decimal(str(round(credit, 2))),
            price_effect=PriceEffect.CREDIT,
            legs=[
                near_opt.build_leg(contracts, OrderAction.BUY_TO_CLOSE),   # buy back short near
                far_opt.build_leg(contracts,  OrderAction.SELL_TO_CLOSE),  # sell long far
            ]
        )
        response = await TT_ACCOUNT.place_order(TT_SESSION, order, dry_run=dry_run)
        placed   = response.order
        log.info(f'Calendar close placed: {symbol} {option_type} @ {strike} | '
                 f'near={near_expiry} far={far_expiry} @ {credit} credit x{contracts} | '
                 f'dry_run={dry_run} | status={placed.status}')
        return {'order_id': getattr(placed, 'id', None), 'status': str(placed.status)}
    except Exception as e:
        log.error(f'Calendar close failed: {e}')
        return {'error': str(e), 'status': 'FAILED'}


# ── Close / Cancel ──────────────────────────────────────────────────

async def tt_cancel_order(order_id: int) -> bool:
    try:
        await TT_ACCOUNT.delete_order(TT_SESSION, order_id)
        log.info(f'Order cancelled: {order_id}')
        return True
    except Exception as e:
        log.error(f'Cancel failed {order_id}: {e}')
        return False


async def tt_cancel_complex_order(oco_id: int) -> bool:
    """Cancel a complex (OCO/OTOCO) order by ID. Returns True on success or if already gone."""
    try:
        await TT_ACCOUNT.delete_complex_order(TT_SESSION, oco_id)
        log.info(f'Complex order cancelled: {oco_id}')
        return True
    except Exception as e:
        log.warning(f'Complex order cancel {oco_id}: {str(e)[:80]} (may already be filled/cancelled)')
        return False


async def tt_close_position(symbol: str, expiry: date,
                             sell_strike: float, buy_strike: float,
                             option_type: str, debit: float,
                             contracts: int = 1,
                             dry_run: bool = False) -> dict:
    """Close a 2-leg credit spread: BUY_TO_CLOSE original short, SELL_TO_CLOSE original long."""
    try:
        await _tt_guard()
        sell_opt, buy_opt = await tt_find_option_symbols(
            symbol, expiry, sell_strike, buy_strike, option_type
        )
        if not sell_opt or not buy_opt:
            msg = f'Close strikes not found: sell={sell_strike} buy={buy_strike} {symbol} {expiry}'
            log.error(msg)
            return {'error': msg, 'status': 'FAILED'}

        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            price=Decimal(str(round(debit, 2))),
            price_effect=PriceEffect.DEBIT,
            legs=[
                sell_opt.build_leg(contracts, OrderAction.BUY_TO_CLOSE),
                buy_opt.build_leg(contracts,  OrderAction.SELL_TO_CLOSE),
            ]
        )
        response = await TT_ACCOUNT.place_order(TT_SESSION, order, dry_run=dry_run)
        placed   = response.order
        log.info(f'Close order placed: {symbol} | dry_run={dry_run} | status={placed.status}')
        return {'order_id': getattr(placed, 'id', None), 'status': str(placed.status)}
    except Exception as e:
        log.error(f'Close position failed: {e}')
        return {'error': str(e), 'status': 'FAILED'}


# ── OCO Close ───────────────────────────────────────────────────────

async def tt_place_oco_close(
    symbol: str,
    expiry: date,
    sell_strike: float,
    buy_strike: float,
    option_type: str,
    tp_price: float,
    sl_price: float,
    contracts: int,
    is_ic: bool = False,
    sell_put: float = None,
    buy_put: float = None,
    sell_call: float = None,
    buy_call: float = None,
) -> dict:
    """Place a GTC OCO pair: TP close + SL close.
    When one fills, TastyTrade automatically cancels the other.
    tp_price: target close debit (e.g. credit × 0.25 = 75% profit taken)
    sl_price: stop  close debit (e.g. credit × 2.00 = hard stop)
    Returns {'oco_id': int, 'status': str} or {'error': str, 'status': 'FAILED'}."""
    from tastytrade.order import NewComplexOrder, ComplexOrderType
    try:
        await _tt_guard()

        # Round prices to nearest $0.05 tick (TT rejects arbitrary decimals on multi-leg combos)
        def _tick(val):
            return max(0.01, round(round(float(val) / 0.05) * 0.05, 2))

        tp_rounded = _tick(tp_price)
        sl_rounded = _tick(sl_price)

        if is_ic:
            chain = await get_option_chain(TT_SESSION, symbol)
            if expiry not in chain:
                available = sorted(chain.keys())
                expiry = min(available, key=lambda d: abs((d - expiry).days))
            options = chain[expiry]
            puts  = [o for o in options if o.option_type.value == 'P']
            calls = [o for o in options if o.option_type.value == 'C']

            def _find(opts, strike):
                return next((o for o in opts if abs(float(o.strike_price) - strike) < 0.01), None)

            sp_opt = _find(puts,  sell_put)
            bp_opt = _find(puts,  buy_put)
            sc_opt = _find(calls, sell_call)
            bc_opt = _find(calls, buy_call)

            if not all([sp_opt, bp_opt, sc_opt, bc_opt]):
                missing = [n for n, o in [('sell_put', sp_opt), ('buy_put', bp_opt),
                                           ('sell_call', sc_opt), ('buy_call', bc_opt)] if not o]
                msg = f'OCO IC strikes not found: {missing} — {symbol} {expiry}'
                log.error(msg)
                return {'error': msg, 'status': 'FAILED'}

            def _ic_tp_order(price):
                return NewOrder(
                    time_in_force=OrderTimeInForce.GTC,
                    order_type=OrderType.LIMIT,
                    price=Decimal(str(price)),
                    price_effect=PriceEffect.DEBIT,
                    legs=[
                        sp_opt.build_leg(contracts, OrderAction.BUY_TO_CLOSE),
                        bp_opt.build_leg(contracts, OrderAction.SELL_TO_CLOSE),
                        sc_opt.build_leg(contracts, OrderAction.BUY_TO_CLOSE),
                        bc_opt.build_leg(contracts, OrderAction.SELL_TO_CLOSE),
                    ],
                )

            def _ic_sl_order(stop, limit):
                return NewOrder(
                    time_in_force=OrderTimeInForce.GTC,
                    order_type=OrderType.STOP_LIMIT,
                    stop_trigger=Decimal(str(stop)),
                    price=Decimal(str(limit)),
                    price_effect=PriceEffect.DEBIT,
                    legs=[
                        sp_opt.build_leg(contracts, OrderAction.BUY_TO_CLOSE),
                        bp_opt.build_leg(contracts, OrderAction.SELL_TO_CLOSE),
                        sc_opt.build_leg(contracts, OrderAction.BUY_TO_CLOSE),
                        bc_opt.build_leg(contracts, OrderAction.SELL_TO_CLOSE),
                    ],
                )

            sl_limit_rounded = _tick(sl_rounded * 1.10)
            tp_order = _ic_tp_order(tp_rounded)
            sl_order = _ic_sl_order(sl_rounded, sl_limit_rounded)

        else:
            sell_opt, buy_opt = await tt_find_option_symbols(
                symbol, expiry, sell_strike, buy_strike, option_type
            )
            if not sell_opt or not buy_opt:
                msg = f'OCO spread strikes not found: sell={sell_strike} buy={buy_strike} {symbol} {expiry}'
                log.error(msg)
                return {'error': msg, 'status': 'FAILED'}

            def _spread_tp_order(price):
                return NewOrder(
                    time_in_force=OrderTimeInForce.GTC,
                    order_type=OrderType.LIMIT,
                    price=Decimal(str(price)),
                    price_effect=PriceEffect.DEBIT,
                    legs=[
                        sell_opt.build_leg(contracts, OrderAction.BUY_TO_CLOSE),
                        buy_opt.build_leg(contracts, OrderAction.SELL_TO_CLOSE),
                    ],
                )

            def _spread_sl_order(stop, limit):
                return NewOrder(
                    time_in_force=OrderTimeInForce.GTC,
                    order_type=OrderType.STOP_LIMIT,
                    stop_trigger=Decimal(str(stop)),
                    price=Decimal(str(limit)),
                    price_effect=PriceEffect.DEBIT,
                    legs=[
                        sell_opt.build_leg(contracts, OrderAction.BUY_TO_CLOSE),
                        buy_opt.build_leg(contracts, OrderAction.SELL_TO_CLOSE),
                    ],
                )

            sl_limit_rounded = _tick(sl_rounded * 1.10)
            tp_order = _spread_tp_order(tp_rounded)
            sl_order = _spread_sl_order(sl_rounded, sl_limit_rounded)

        oco = NewComplexOrder(
            type=ComplexOrderType.OCO,
            orders=[tp_order, sl_order],
        )
        response = await TT_ACCOUNT.place_complex_order(TT_SESSION, oco, dry_run=False)
        oco_id   = response.complex_order.id
        log.info(f'OCO placed: {symbol} tp_limit=${tp_rounded} sl_stop=${sl_rounded} '
                 f'sl_limit=${_tick(sl_rounded * 1.10)} x{contracts} oco_id={oco_id}')
        return {'oco_id': oco_id, 'status': str(response.complex_order.type)}

    except Exception as e:
        log.error(f'OCO placement failed {symbol}: {e}')
        return {'error': str(e), 'status': 'FAILED'}


# ── Fill Polling ────────────────────────────────────────────────────

async def tt_poll_order_fill(order_id: int, intervals: list = None) -> dict:
    """Poll TT order status until FILLED or all intervals exhausted.
    intervals: seconds to sleep before each poll. Default [15, 15, 30, 60] = 120s total.

    Returns one of:
      {'filled': True,  'fill_price': float|None, 'waited_s': int}
      {'filled': False, 'terminal': True, 'status': str, 'waited_s': int}
      {'filled': False, 'timed_out': True,           'waited_s': int}
    """
    from tastytrade.order import OrderStatus as _OS
    if intervals is None:
        intervals = [15, 15, 30, 60]
    _TERMINAL = {_OS.CANCELLED, _OS.REJECTED, _OS.EXPIRED, _OS.REMOVED, _OS.PARTIALLY_REMOVED}
    total_waited = 0
    for delay in intervals:
        await asyncio.sleep(delay)
        total_waited += delay
        try:
            await TT_SESSION.refresh()
            placed = await TT_ACCOUNT.get_order(TT_SESSION, order_id)
            status = placed.status
            if status == _OS.FILLED:
                fill_price = None
                for leg in placed.legs:
                    for f in (leg.fills or []):
                        fill_price = float(f.fill_price)
                        break
                    if fill_price is not None:
                        break
                log.info(f'Order {order_id} FILLED: fill_price={fill_price} after {total_waited}s')
                return {'filled': True, 'fill_price': fill_price, 'waited_s': total_waited}
            elif status in _TERMINAL:
                log.warning(f'Order {order_id} terminal: {status.value} after {total_waited}s')
                return {'filled': False, 'terminal': True, 'status': status.value, 'waited_s': total_waited}
            else:
                log.info(f'Order {order_id} status={status.value} at {total_waited}s — continuing poll')
        except Exception as e:
            log.warning(f'Poll order {order_id} at {total_waited}s error: {str(e)[:100]}')
    log.warning(f'Order {order_id} not filled after {total_waited}s — timed out')
    return {'filled': False, 'timed_out': True, 'waited_s': total_waited}


# ── Market Data Helpers ─────────────────────────────────────────────

async def tt_get_spot_batch(symbols: list) -> dict:
    """Batch fetch current prices for equity symbols via DXLink Quote."""
    from tastytrade.streamer import DXLinkStreamer
    from tastytrade.dxfeed import Quote
    result = {}
    batch = [s for s in symbols if s]
    if not batch:
        return result
    try:
        async with DXLinkStreamer(TT_SESSION) as streamer:
            await streamer.subscribe(Quote, batch)
            await asyncio.sleep(5)
            while True:
                q = streamer.get_event_nowait(Quote)
                if q is None:
                    break
                sym = q.event_symbol
                bid = float(q.bid_price or 0)
                ask = float(q.ask_price or 0)
                if bid > 0 and ask > 0:
                    result[sym] = round((bid + ask) / 2, 2)
                elif bid > 0:
                    result[sym] = round(bid, 2)
    except Exception as e:
        log.warning(f'Spot batch DXLink error: {str(e)[:60]}')
    log.info(f'Spot prefetch: {len(result)}/{len(batch)} symbols via DXLink')
    return result


async def tt_get_metrics_batch(symbols: list) -> dict:
    """Batch fetch market metrics (IVR, IV, earnings) — up to 50 per call."""
    from tastytrade.metrics import get_market_metrics
    result = {}
    batch = list(symbols)
    for i in range(0, len(batch), 50):
        chunk = batch[i:i + 50]
        try:
            metrics = await get_market_metrics(TT_SESSION, chunk)
            for m in metrics:
                result[m.symbol] = m
        except Exception as e:
            log.warning(f'Metrics batch error: {str(e)[:60]}')
    return result


async def tt_prefetch_history(symbols: list, days: int = 100) -> dict:
    """Fetch daily OHLCV candle history for all symbols in one DXLink session.
    Returns {symbol: DataFrame} with columns Open/High/Low/Close/Volume.
    Required by market.py get_options_data() for 5-day price move (bias signal 1)."""
    from tastytrade.streamer import DXLinkStreamer
    from tastytrade.dxfeed import Candle
    from datetime import datetime, timedelta

    start_time = datetime.now() - timedelta(days=days + 5)
    raw = {}
    try:
        async with DXLinkStreamer(TT_SESSION) as streamer:
            await streamer.subscribe_candle(list(symbols), '1d', start_time=start_time)
            await asyncio.sleep(12)
            while True:
                c = streamer.get_event_nowait(Candle)
                if c is None:
                    break
                sym = c.event_symbol.split('{')[0] if '{' in c.event_symbol else c.event_symbol
                raw.setdefault(sym, []).append({
                    'time':   c.time,
                    'Open':   float(c.open   or 0),
                    'High':   float(c.high   or 0),
                    'Low':    float(c.low    or 0),
                    'Close':  float(c.close  or 0),
                    'Volume': float(c.volume or 0),
                })
    except Exception as e:
        log.warning(f'History prefetch error: {str(e)[:80]}')

    dfs = {}
    for sym, candles in raw.items():
        if len(candles) >= 6:   # need at least 6 closes for 5-day move
            df = pd.DataFrame(sorted(candles, key=lambda x: x['time']))
            df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
            df = df[df['Close'] > 0].reset_index(drop=True)
            dfs[sym] = df

    got = len(dfs)
    avg = sum(len(v) for v in raw.values()) // max(len(raw), 1)
    log.info(f'History prefetch: {got}/{len(symbols)} symbols, avg {avg} candles')
    return dfs


async def tt_get_vix() -> float:
    """Get current VIX via DXLink daily candle (most recent close)."""
    from tastytrade.streamer import DXLinkStreamer
    from tastytrade.dxfeed import Candle
    from datetime import datetime, timedelta
    try:
        start_time = datetime.now() - timedelta(days=5)
        best = None
        async with DXLinkStreamer(TT_SESSION) as streamer:
            await streamer.subscribe_candle(['$VIX.X'], '1d', start_time=start_time)
            await asyncio.sleep(5)
            while True:
                c = streamer.get_event_nowait(Candle)
                if c is None:
                    break
                close = float(c.close or 0)
                if close > 0:
                    if best is None or c.time > best[0]:
                        best = (c.time, close)
        if best:
            log.info(f'VIX from DXLink candle: {best[1]}')
            return round(best[1], 2)
    except Exception as e:
        log.warning(f'TT VIX error: {str(e)[:60]}')
    return 20.0


def _is_monthly(d) -> bool:
    """True if date is the 3rd Friday of its month (standard monthly option expiry).
    Monthly expirations have the deepest OI and tightest bid-ask spreads."""
    from calendar import weekday, FRIDAY
    return weekday(d.year, d.month, d.day) == FRIDAY and 15 <= d.day <= 21


async def tt_get_option_instruments(symbol: str, dte_min: int, dte_max: int,
                                    prefer_nearest: bool = False,
                                    ranked: bool = False):
    """Return expiry info for the best expiry in DTE range.
    prefer_nearest=False (default, credits): prefer monthly (3rd Friday) — deepest OI.
    prefer_nearest=True  (debits): prefer nearest expiry — minimise time premium paid.
    ranked=True: return [(expiry_str, dte, options), ...] sorted by preference.
    ranked=False: return (expiry_str, dte, options) single best expiry."""
    from tastytrade.instruments import get_option_chain
    from datetime import date as _date
    await _tt_guard()
    try:
        chain = await get_option_chain(TT_SESSION, symbol)
        today = _date.today()
        candidates = []
        for expiry in sorted(chain.keys()):
            dte = (expiry - today).days
            if dte_min <= dte <= dte_max:
                candidates.append((expiry, dte, chain[expiry]))
        if not candidates:
            return [] if ranked else (None, None, [])
        if prefer_nearest:
            sorted_cands = sorted(candidates, key=lambda x: x[1])
            if ranked:
                return [(e.isoformat(), d, o) for e, d, o in sorted_cands]
            return sorted_cands[0][0].isoformat(), sorted_cands[0][1], sorted_cands[0][2]
        # Credits: monthly (3rd Friday = deepest OI) first, then weeklies by DTE
        monthlies = sorted([(e, d, o) for e, d, o in candidates if _is_monthly(e)],     key=lambda x: x[1])
        weeklies  = sorted([(e, d, o) for e, d, o in candidates if not _is_monthly(e)], key=lambda x: x[1])
        sorted_cands = monthlies + weeklies
        if ranked:
            return [(e.isoformat(), d, o) for e, d, o in sorted_cands]
        best = sorted_cands[0]
        return best[0].isoformat(), best[1], best[2]
    except Exception as e:
        log.warning(f'Option instruments error {symbol}: {str(e)[:60]}')
    return [] if ranked else (None, None, [])


async def tt_get_greeks_for_options(option_objects: list, spot_symbols: list = None) -> dict:
    """Fetch live greeks + bid/ask + open_interest for specific Option objects via DXLink.
    Returns {occ_symbol: {delta, gamma, theta, vega, rho, mid_iv, bid, ask, open_interest}}.
    If spot_symbols provided, equity Quote events are fetched in the SAME DXLink session."""
    from tastytrade.streamer import DXLinkStreamer
    from tastytrade.dxfeed import Greeks, Quote, Summary

    sym_map = {}
    for opt in option_objects:
        ss = getattr(opt, 'streamer_symbol', None)
        if ss:
            sym_map[ss] = opt.symbol

    if not sym_map:
        log.warning(f'tt_get_greeks: sym_map empty — no streamer_symbol on {len(option_objects)} options')
        return {}

    _spot_set = set(spot_symbols or [])
    result = {}
    for _attempt in range(2):
        try:
            async with DXLinkStreamer(TT_SESSION) as streamer:
                await streamer.subscribe(Greeks,   list(sym_map.keys()))
                await streamer.subscribe(Quote,    list(sym_map.keys()))
                await streamer.subscribe(Summary,  list(sym_map.keys()))
                if _spot_set:
                    await streamer.subscribe(Quote, list(_spot_set))
                await asyncio.sleep(8)

                while True:
                    g = streamer.get_event_nowait(Greeks)
                    if g is None:
                        break
                    occ  = sym_map.get(g.event_symbol, g.event_symbol)
                    theo = float(g.price or 0)
                    result[occ] = {
                        'delta':  float(g.delta      or 0),
                        'gamma':  float(g.gamma      or 0),
                        'theta':  float(g.theta      or 0),
                        'vega':   float(g.vega       or 0),
                        'rho':    float(g.rho        or 0),
                        'mid_iv': float(g.volatility or 0),
                        'price':  theo,
                        'bid':    round(theo * 0.95, 2),
                        'ask':    round(theo * 1.05, 2),
                    }

                while True:
                    q = streamer.get_event_nowait(Quote)
                    if q is None:
                        break
                    if q.event_symbol in _spot_set:
                        bid = float(q.bid_price or 0)
                        ask = float(q.ask_price or 0)
                        if bid > 0 and ask > 0:
                            result[f'_spot_{q.event_symbol}'] = round((bid + ask) / 2, 2)
                        elif bid > 0:
                            result[f'_spot_{q.event_symbol}'] = round(bid, 2)
                    else:
                        occ = sym_map.get(q.event_symbol, q.event_symbol)
                        result.setdefault(occ, {})
                        bid = float(q.bid_price or 0)
                        ask = float(q.ask_price or 0)
                        if bid > 0:
                            result[occ]['bid'] = bid
                            result[occ]['ask'] = ask

                while True:
                    s = streamer.get_event_nowait(Summary)
                    if s is None:
                        break
                    occ = sym_map.get(s.event_symbol, s.event_symbol)
                    result.setdefault(occ, {})
                    result[occ]['open_interest'] = int(s.open_interest or 0)
                    if s.prev_day_volume is not None:
                        result[occ]['volume'] = int(s.prev_day_volume)

            break  # success

        except Exception as e:
            log.warning(f'TT greeks error (attempt {_attempt + 1}/2): {str(e)[:60]}')
            if _attempt == 0:
                await asyncio.sleep(3)

    bids_received = sum(1 for k, v in result.items()
                        if not k.startswith('_spot_') and isinstance(v, dict) and v.get('bid', 0) > 0)
    oi_received   = sum(1 for k, v in result.items()
                        if not k.startswith('_spot_') and isinstance(v, dict) and v.get('open_interest') is not None)
    log.info(f'tt_get_greeks: {len(sym_map)} subscribed → {len(result)} entries, {bids_received} bid>0, {oi_received} OI')
    return result


async def tt_get_spread_value(symbol: str, expiry_str: str,
                               sell_strike: float, buy_strike: float,
                               opt_type: str) -> dict | None:
    """Get current bid/ask and greeks for both legs of a spread.
    Returns {sell: {mid, bid, ask, delta, gamma, theta, vega, mid_iv},
             buy:  {...}, spot: float} or None on failure."""
    from tastytrade.instruments import get_option_chain
    from datetime import date as _date
    try:
        expiry = _date.fromisoformat(expiry_str)
        chain  = await get_option_chain(TT_SESSION, symbol)
        if expiry not in chain:
            available = sorted(chain.keys())
            expiry = min(available, key=lambda d: abs((d - expiry).days))
        options    = chain[expiry]
        opt_letter = 'C' if opt_type == 'call' else 'P'
        sell_opt   = next((o for o in options
                           if o.option_type.value == opt_letter
                           and abs(float(o.strike_price) - sell_strike) < 0.01), None)
        buy_opt    = next((o for o in options
                           if o.option_type.value == opt_letter
                           and abs(float(o.strike_price) - buy_strike)  < 0.01), None)
        if not sell_opt and not buy_opt:
            log.warning(f'Legs not found: {symbol} {expiry_str} {sell_strike}/{buy_strike}')
            return None
        legs_data  = await tt_get_greeks_for_options(
            [o for o in [sell_opt, buy_opt] if o],
            spot_symbols=[symbol]
        )
        spot_price = legs_data.pop(f'_spot_{symbol}', None)

        def _leg(opt):
            if opt is None:
                return {}
            d     = legs_data.get(opt.symbol, {})
            bid   = d.get('bid', 0)
            ask   = d.get('ask', 0)
            price = d.get('price', 0)
            if ask > 0:
                mid = round((bid + ask) / 2, 2)
            elif bid > 0:
                mid = round(bid, 2)
            elif price > 0:
                mid = round(price, 2)
            else:
                mid = None
            return {**d, 'mid': mid, 'strike': float(opt.strike_price)}

        return {'sell': _leg(sell_opt), 'buy': _leg(buy_opt), 'spot': spot_price}
    except Exception as e:
        log.warning(f'tt_get_spread_value error {symbol}: {str(e)[:70]}')
        return None


# ── Roll (credit spreads only) ──────────────────────────────────────

async def tt_roll_spread(
    symbol:       str,
    cur_expiry:   date,
    cur_sell:     float,
    cur_buy:      float,
    new_expiry:   date,
    new_sell:     float,
    new_buy:      float,
    option_type:  str,
    close_debit:  float,
    open_credit:  float,
    contracts:    int = 1,
    dry_run:      bool = True,
) -> dict:
    """Roll a credit spread: close existing position then open new one.
    Executes as two sequential limit orders.
    Returns dict with: net_credit, close_order, open_order, success, error."""
    try:
        await _tt_guard()

        log.info(f'ROLL {symbol}: closing {cur_sell}/{cur_buy} @ {close_debit} debit | '
                 f'dry_run={dry_run}')
        close_result = await tt_close_position(
            symbol=symbol, expiry=cur_expiry,
            sell_strike=cur_sell, buy_strike=cur_buy,
            option_type=option_type, debit=close_debit,
            contracts=contracts, dry_run=dry_run,
        )
        if close_result.get('error') or close_result.get('status') == 'FAILED':
            msg = f'Roll close leg failed: {close_result.get("error", close_result.get("status"))}'
            log.error(msg)
            return {'success': False, 'error': msg,
                    'close_order': close_result, 'open_order': None, 'net_credit': 0}

        log.info(f'ROLL {symbol}: opening {new_sell}/{new_buy} @ {open_credit} credit | '
                 f'expiry={new_expiry} dry_run={dry_run}')
        open_result = await tt_place_credit_spread(
            symbol=symbol, expiry=new_expiry,
            sell_strike=new_sell, buy_strike=new_buy,
            option_type=option_type, credit=open_credit,
            contracts=contracts, dry_run=dry_run,
        )
        if open_result.get('error') or open_result.get('status') == 'FAILED':
            msg = f'Roll open leg failed: {open_result.get("error", open_result.get("status"))}'
            log.error(msg)
            return {'success': False, 'error': msg,
                    'close_order': close_result, 'open_order': open_result, 'net_credit': 0}

        net_credit = round(open_credit - close_debit, 2)
        log.info(f'ROLL complete: {symbol} {cur_sell}/{cur_buy} → {new_sell}/{new_buy} | '
                 f'net_credit={net_credit} | dry_run={dry_run}')
        return {
            'success':     True,
            'net_credit':  net_credit,
            'close_order': close_result,
            'open_order':  open_result,
            'symbol':      symbol,
            'cur_sell':    cur_sell,
            'cur_buy':     cur_buy,
            'new_sell':    new_sell,
            'new_buy':     new_buy,
            'new_expiry':  str(new_expiry),
            'contracts':   contracts,
            'dry_run':     dry_run,
        }
    except Exception as e:
        log.error(f'tt_roll_spread error {symbol}: {e}')
        return {'success': False, 'error': str(e),
                'close_order': None, 'open_order': None, 'net_credit': 0}
