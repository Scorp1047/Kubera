"""
strategies.py — Kubera order builder

What this file does:
  Given a short strike already selected at 16 delta (from market.py) and a strategy
  chosen by the decision tree (from signals.py), find the wing (buy) strike from the
  live chain and return a finalized order dict with exact real-chain prices.

  One function per strategy type:
    build_put_credit_spread()
    build_call_credit_spread()
    build_iron_condor()
    build_jade_lizard()
    build_debit_spread()
    build_calendar_spread()

  Wing selection rule (TastyTrade):
    Collect ≥ 1/3 the spread width as credit (IC_CREDIT_RATIO = 0.333).
    Try standard wing widths from the chain; pick the one that maximises
    net_credit while meeting the 1/3 rule and OI/bid-ask quality gates.
    If no wing meets the 1/3 rule, take the best available (don't force-skip
    the trade on a technicality — but log the shortfall).

What this file does NOT do:
  - RSI/EMA/MACD/ADX directional routing (that's signals.py)
  - Strategy selection (that's signals.py)
  - Strike selection at 16 delta (that's market.py)
  - Position sizing (that's signals.py.calculate_size())
  - Order placement (that's tasty.py)
"""

from datetime import date
from config import (
    log, IC_CREDIT_RATIO, MIN_OPTION_OI, MAX_SPREAD_PCT,
    PROFIT_TARGET_CREDIT, PROFIT_TARGET_DEBIT, PROFIT_TARGET_CALENDAR,
    LOSS_STOP_MULTIPLIER, DTE_EXIT, ROLL_POP_FLOOR,
    CAL_DTE_FRONT, CAL_DTE_BACK, CAL_IV_MAX,
)
from market import bs_delta, bs_prob_otm


# ── Wing Finder ────────────────────────────────────────────────────

def _find_wing(chain, short_strike, direction, price, em_dollar, min_credit_ratio=IC_CREDIT_RATIO):
    """Find the best wing (buy) strike from the live chain.

    Wing selection logic:
      1. Determine candidate wing widths based on stock price tier.
      2. For each candidate width, check:
           a. Strike exists in chain
           b. OI ≥ MIN_OPTION_OI
           c. Bid-ask spread ≤ MAX_SPREAD_PCT
      3. Compute net_credit = sell_mid - buy_mid
         and credit_ratio = net_credit / wing_width
      4. Score = net_credit × credit_ratio² (rewards both premium and ratio quality)
      5. Return best-scoring wing. Log if credit_ratio < 1/3.

    TastyTrade 1/3 rule: credit ≥ 1/3 spread width → ~67% POP mathematically.
    Source: tastylive.com (big-boy IC, chicken IC concept pages).

    direction: 'put' (wing below short) or 'call' (wing above short)
    em_dollar: expected move in dollars — used to warn if wing is narrower than EM"""

    # Wing width candidates by price tier
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
        if direction == 'put':
            buy_strike = round(short_strike - wing_width, 2)
        else:
            buy_strike = round(short_strike + wing_width, 2)

        # Find wing in chain
        wing_entry = next(
            (o for o in chain if abs(float(o.get('strike', 0)) - buy_strike) < 0.01),
            None
        )
        if wing_entry is None:
            continue

        # OI gate on wing
        wing_oi = wing_entry.get('open_interest')
        if wing_oi is not None and wing_oi < MIN_OPTION_OI:
            continue

        # Bid-ask gate on wing
        wing_bid = float(wing_entry.get('bid', 0))
        wing_ask = float(wing_entry.get('ask', 0))
        if wing_bid > 0 and wing_ask > 0:
            wing_mid = (wing_bid + wing_ask) / 2
            if wing_mid > 0 and (wing_ask - wing_bid) / wing_mid * 100 > MAX_SPREAD_PCT:
                continue
        wing_mid = round((wing_bid + wing_ask) / 2, 2) if wing_bid > 0 else round(wing_ask * 0.5, 2)

        # Find short leg in chain for sell_mid
        short_entry = next(
            (o for o in chain if abs(float(o.get('strike', 0)) - short_strike) < 0.01),
            None
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

        # Score: rewards higher credit AND better 1/3 ratio adherence
        score = net_credit * (credit_ratio ** 2)

        if score > best_score:
            best_score = score
            best_setup = {
                'wing_width':    wing_width,
                'buy_strike':    buy_strike,
                'net_credit':    net_credit,
                'credit_ratio':  credit_ratio,
                'max_loss':      max_loss,
                'sell_mid':      sell_mid,
                'wing_mid':      wing_mid,
            }

    if best_setup is None:
        return None

    # Log if TT 1/3 rule not met (trade still proceeds — don't block on technicality)
    if best_setup['credit_ratio'] < min_credit_ratio:
        log.info(
            f'Wing: credit ratio {best_setup["credit_ratio"]:.3f} < '
            f'{min_credit_ratio:.3f} (1/3 rule) — best available wing used '
            f'(wing={best_setup["wing_width"]} credit=${best_setup["net_credit"]:.2f})'
        )
    if em_dollar > 0 and best_setup['wing_width'] < em_dollar:
        log.info(
            f'Wing ${best_setup["wing_width"]:.0f} < EM ${em_dollar:.2f} — '
            f'wing is narrower than expected move'
        )

    return best_setup


# ── Put Credit Spread ──────────────────────────────────────────────

def build_put_credit_spread(data, signal):
    """Build a put credit spread using the 16-delta short strike from data.

    Sell OTM put at 16 delta + buy lower put (wing from chain).
    Bullish/neutral thesis — profits if stock stays above short strike.
    Management: close at 50% profit OR 21 DTE, whichever first.

    Returns finalized order dict or None."""
    best_put  = data.get('best_put')
    chain     = data.get('chain_puts', [])
    price     = data.get('price', 0.0)
    em_dollar = data.get('em_dollar', 0.0)

    if best_put is None:
        log.warning(f'{data.get("symbol")}: build_put_credit_spread — no put strike')
        return None

    short_strike = best_put['strike']
    wing = _find_wing(chain, short_strike, 'put', price, em_dollar)
    if wing is None:
        log.info(f'{data.get("symbol")}: build_put_credit_spread — no valid wing found')
        return None

    contracts    = signal.get('contracts', 1)
    net_credit   = wing['net_credit']
    max_loss     = round(wing['max_loss'] * contracts, 2)
    profit_target = round(net_credit * PROFIT_TARGET_CREDIT, 2)
    loss_stop     = round(net_credit * LOSS_STOP_MULTIPLIER, 2)

    return {
        'strategy':       'put_credit_spread',
        'symbol':         data['symbol'],
        'expiry':         str(data['expiry']),
        'dte':            data['dte'],
        'price':          price,
        'sell_strike':    short_strike,
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
        # Management rules
        'profit_target':  profit_target,   # close when mark credit remaining ≤ this
        'loss_stop':      loss_stop,        # close when mark loss = 2× credit received
        'dte_exit':       DTE_EXIT,
        'roll_pop_floor': ROLL_POP_FLOOR,
    }


# ── Call Credit Spread ─────────────────────────────────────────────

def build_call_credit_spread(data, signal):
    """Build a call credit spread using the 16-delta short strike from data.

    Sell OTM call at 16 delta + buy higher call (wing from chain).
    Bearish/neutral thesis — profits if stock stays below short strike.
    Management: close at 50% profit OR 21 DTE, whichever first.

    Returns finalized order dict or None."""
    best_call = data.get('best_call')
    chain     = data.get('chain_calls', [])
    price     = data.get('price', 0.0)
    em_dollar = data.get('em_dollar', 0.0)

    if best_call is None:
        log.warning(f'{data.get("symbol")}: build_call_credit_spread — no call strike')
        return None

    short_strike = best_call['strike']
    wing = _find_wing(chain, short_strike, 'call', price, em_dollar)
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
        'sell_strike':    short_strike,
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


# ── Iron Condor ────────────────────────────────────────────────────

def build_iron_condor(data, signal):
    """Build an iron condor: sell OTM put spread + sell OTM call spread.

    Both short strikes at 16 delta. Wings found independently.
    Neutral thesis — profits if stock stays between the two short strikes.
    TastyTrade: use when HIGH IVR + NEUTRAL bias. Both sides collect premium.
    Max loss = one side (cannot lose both sides simultaneously).
    Management: close at 50% of TOTAL credit OR 21 DTE.

    Returns finalized order dict or None."""
    best_put  = data.get('best_put')
    best_call = data.get('best_call')
    chain_puts  = data.get('chain_puts',  [])
    chain_calls = data.get('chain_calls', [])
    price     = data.get('price', 0.0)
    em_dollar = data.get('em_dollar', 0.0)

    if best_put is None or best_call is None:
        log.warning(f'{data.get("symbol")}: build_iron_condor — missing leg(s)')
        return None

    put_wing  = _find_wing(chain_puts,  best_put['strike'],  'put',  price, em_dollar)
    call_wing = _find_wing(chain_calls, best_call['strike'], 'call', price, em_dollar)

    if put_wing is None or call_wing is None:
        missing = 'put' if put_wing is None else 'call'
        log.info(f'{data.get("symbol")}: build_iron_condor — no {missing} wing found')
        return None

    total_credit  = round(put_wing['net_credit'] + call_wing['net_credit'], 2)
    # IC max loss = one side (worst case: only one side breached at expiry)
    wing_max      = max(put_wing['wing_width'], call_wing['wing_width'])
    max_loss_1ct  = round((wing_max - max(put_wing['net_credit'], call_wing['net_credit'])) * 100, 2)
    contracts     = signal.get('contracts', 1)
    max_loss      = round(max_loss_1ct * contracts, 2)
    profit_target = round(total_credit * PROFIT_TARGET_CREDIT, 2)
    loss_stop     = round(total_credit * LOSS_STOP_MULTIPLIER, 2)

    # TT 1/3 rule check on combined credit
    total_width    = put_wing['wing_width'] + call_wing['wing_width']
    combined_ratio = round(total_credit / total_width, 3) if total_width > 0 else 0.0
    if combined_ratio < IC_CREDIT_RATIO:
        log.info(
            f'{data.get("symbol")}: IC combined credit ratio {combined_ratio:.3f} '
            f'< {IC_CREDIT_RATIO:.3f} (1/3 rule) — proceeding with best available'
        )

    return {
        'strategy':         'iron_condor',
        'symbol':           data['symbol'],
        'expiry':           str(data['expiry']),
        'dte':              data['dte'],
        'price':            price,
        # Put spread (lower)
        'put_sell_strike':  best_put['strike'],
        'put_buy_strike':   put_wing['buy_strike'],
        'put_wing_width':   put_wing['wing_width'],
        'put_credit':       put_wing['net_credit'],
        'put_pop':          best_put.get('pop', 0.0),
        'put_delta':        best_put.get('delta', 0.0),
        # Call spread (upper)
        'call_sell_strike': best_call['strike'],
        'call_buy_strike':  call_wing['buy_strike'],
        'call_wing_width':  call_wing['wing_width'],
        'call_credit':      call_wing['net_credit'],
        'call_pop':         best_call.get('pop', 0.0),
        'call_delta':       best_call.get('delta', 0.0),
        # Combined
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


# ── Jade Lizard ────────────────────────────────────────────────────

def build_jade_lizard(data, signal):
    """Build a Jade Lizard: sell OTM put + sell OTM call spread.

    TastyTrade Jade Lizard rules:
      - Sell 1 OTM put (bullish bias, collect fat premium from skew)
      - Sell 1 OTM call spread (short OTM call + long higher-strike call)
      - 'Perfect' condition: total credit ≥ call spread width → zero upside risk
      - Only downside risk: put breached on a large decline
      - Preferred over a simple put credit spread when the credit math works

    Source: tastylive.com/concepts-strategies/jade-lizard,
            tastylive.com/news-insights/the-jade-lizard-market-measures

    Falls back to put credit spread if:
      - No call strike available
      - Credit math does not satisfy the zero-upside-risk condition

    Returns finalized order dict or None."""
    best_put    = data.get('best_put')
    best_call   = data.get('best_call')
    chain_puts  = data.get('chain_puts',  [])
    chain_calls = data.get('chain_calls', [])
    price       = data.get('price', 0.0)
    em_dollar   = data.get('em_dollar', 0.0)
    symbol      = data.get('symbol', '')

    if best_put is None:
        log.warning(f'{symbol}: build_jade_lizard — no put strike')
        return None

    # Build put side first
    put_wing = _find_wing(chain_puts, best_put['strike'], 'put', price, em_dollar)
    if put_wing is None:
        log.info(f'{symbol}: build_jade_lizard — no put wing, falling back to put credit spread')
        return build_put_credit_spread(data, signal)

    put_credit = put_wing['net_credit']

    # Try to add call spread
    if best_call is None:
        log.info(f'{symbol}: build_jade_lizard — no call strike, falling back to put credit spread')
        return build_put_credit_spread(data, signal)

    call_wing = _find_wing(chain_calls, best_call['strike'], 'call', price, em_dollar)
    if call_wing is None:
        log.info(f'{symbol}: build_jade_lizard — no call wing, falling back to put credit spread')
        return build_put_credit_spread(data, signal)

    call_credit      = call_wing['net_credit']
    total_credit     = round(put_credit + call_credit, 2)
    call_spread_width = call_wing['wing_width']

    # TastyTrade Jade Lizard check: total credit ≥ call spread width → zero upside risk
    from config import JADE_MIN_CREDIT_RATIO
    if total_credit >= call_spread_width * JADE_MIN_CREDIT_RATIO:
        jade_valid  = True
        jade_reason = (
            f'Jade Lizard valid: total credit ${total_credit:.2f} ≥ '
            f'call spread width ${call_spread_width:.0f} → zero upside risk'
        )
        log.info(f'{symbol}: {jade_reason}')
    else:
        jade_valid  = False
        jade_reason = (
            f'Jade Lizard degraded: credit ${total_credit:.2f} < '
            f'call spread width ${call_spread_width:.0f} → downgrade to put credit spread'
        )
        log.info(f'{symbol}: {jade_reason}')
        # Degrade to put credit spread
        return build_put_credit_spread(data, signal)

    contracts     = signal.get('contracts', 1)
    # BPR for Jade Lizard: put side max loss (call side is capped by call spread)
    max_loss_1ct  = round((put_wing['wing_width'] - put_credit) * 100, 2)
    max_loss      = round(max_loss_1ct * contracts, 2)
    profit_target = round(total_credit * PROFIT_TARGET_CREDIT, 2)
    loss_stop     = round(total_credit * LOSS_STOP_MULTIPLIER, 2)

    return {
        'strategy':           'jade_lizard',
        'symbol':             symbol,
        'expiry':             str(data['expiry']),
        'dte':                data['dte'],
        'price':              price,
        # Put leg (the naked-put equivalent)
        'put_sell_strike':    best_put['strike'],
        'put_buy_strike':     put_wing['buy_strike'],
        'put_wing_width':     put_wing['wing_width'],
        'put_credit':         put_credit,
        'put_pop':            best_put.get('pop', 0.0),
        'put_delta':          best_put.get('delta', 0.0),
        # Call spread leg
        'call_sell_strike':   best_call['strike'],
        'call_buy_strike':    call_wing['buy_strike'],
        'call_wing_width':    call_spread_width,
        'call_credit':        call_credit,
        'call_pop':           best_call.get('pop', 0.0),
        'call_delta':         best_call.get('delta', 0.0),
        # Combined
        'credit':             total_credit,
        'jade_valid':         jade_valid,
        'jade_reason':        jade_reason,
        'contracts':          contracts,
        'max_loss':           max_loss,
        'iv':                 data.get('avg_iv', 30.0),
        'em_dollar':          em_dollar,
        'em_pct':             data.get('em_pct', 0.0),
        'ivr':                data.get('ivr', 0.0),
        'ivr_regime':         data.get('ivr_regime', ''),
        'bias':               data.get('bias', 'NEUTRAL'),
        'profit_target':      profit_target,
        'loss_stop':          loss_stop,
        'dte_exit':           DTE_EXIT,
        'roll_pop_floor':     ROLL_POP_FLOOR,
    }


# ── Debit Spread ───────────────────────────────────────────────────

def build_debit_spread(data, signal):
    """Build a debit spread for low-IV directional plays.

    TastyTrade: deploy in LOW IVR when price is at an extreme.
    Buy ATM-ish (35-45 delta) option + sell further OTM (same expiry).
    Direction (sub_type) already determined by signals.py from the 5-day move:
      call debit: stock oversold (down 5%+ in 5 days) → mean reversion up
      put debit:  stock overbought (up 5%+ in 5 days) → fade the move

    Profit target: 25% of debit paid (TT: 25–50% range; conservative end).
    Stop: 50% loss of debit paid.

    Returns finalized order dict or None."""
    sub_type  = signal.get('sub_type', 'call')
    price     = data.get('price', 0.0)
    expiry    = data.get('expiry')
    dte       = data.get('dte', 0)
    avg_iv    = data.get('avg_iv', 30.0)
    symbol    = data.get('symbol', '')
    T         = dte / 365 if dte > 0 else 0.1
    r         = 0.05

    # For debit: buy the ~40-delta strike (more directional than 16-delta)
    # Sell the further OTM (same expiry) to reduce cost
    TARGET_DEBIT_DELTA = 0.40   # buy leg — more premium, more directional

    if sub_type == 'call':
        chain = data.get('chain_calls', [])
        opt_type = 'call'
    else:
        chain = data.get('chain_puts', [])
        opt_type = 'put'

    if not chain:
        log.info(f'{symbol}: build_debit_spread/{sub_type} — no chain data')
        return None

    # Find buy leg (~40 delta)
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

    # Find sell leg (further OTM to reduce debit cost)
    # For call debit: sell strike = buy_strike + wing_width (above buy)
    # For put debit:  sell strike = buy_strike - wing_width (below buy)
    if price < 20:
        wing_width = 2
    elif price < 50:
        wing_width = 3
    elif price < 100:
        wing_width = 5
    else:
        wing_width = 10

    if sub_type == 'call':
        sell_target = round(buy_leg['strike'] + wing_width, 2)
    else:
        sell_target = round(buy_leg['strike'] - wing_width, 2)

    sell_leg = next(
        (o for o in chain if abs(float(o.get('strike', 0)) - sell_target) < 0.01),
        None
    )

    buy_ask  = buy_leg['ask']
    sell_bid = float(sell_leg.get('bid', 0)) if sell_leg else 0.0
    net_debit = round(buy_ask - sell_bid, 2)

    if net_debit <= 0:
        log.info(f'{symbol}: build_debit_spread — zero or negative net debit')
        return None

    contracts  = signal.get('contracts', 1)
    max_loss   = round(net_debit * 100 * contracts, 2)
    max_profit = round((wing_width - net_debit) * 100 * contracts, 2)

    return {
        'strategy':      'debit_spread',
        'sub_type':      sub_type,
        'symbol':        symbol,
        'expiry':        str(expiry),
        'dte':           dte,
        'price':         price,
        'buy_strike':    buy_leg['strike'],
        'sell_strike':   sell_target,
        'wing_width':    wing_width,
        'debit':         net_debit,
        'contracts':     contracts,
        'max_loss':      max_loss,
        'max_profit':    max_profit,
        'pop':           buy_leg.get('pop', 0.0),
        'delta':         buy_leg.get('delta', 0.0),
        'iv':            buy_leg.get('iv', avg_iv),
        'em_dollar':     data.get('em_dollar', 0.0),
        'em_pct':        data.get('em_pct', 0.0),
        'ivr':           data.get('ivr', 0.0),
        'ivr_regime':    data.get('ivr_regime', ''),
        'bias':          data.get('bias', 'NEUTRAL'),
        # Management
        'profit_target_pct': PROFIT_TARGET_DEBIT,    # 0.25 = close at 25% of debit paid
        'loss_stop_pct':     0.50,                    # close if loss > 50% of debit
        'dte_exit':          DTE_EXIT,
    }


# ── Calendar Spread ────────────────────────────────────────────────

def build_calendar_spread(data, signal):
    """Build a calendar spread for low-IV / quiet-stock environments.

    TastyTrade: deploy in LOW IVR when stock has been abnormally quiet.
    Structure: sell front-month option + buy back-month at same strike.
    Profit from IV expansion + front-month theta decay.
    Profit target: 15% of debit paid (TT range 10–25%; midpoint used).
    Source: tastylive.com calendar spread page.

    Calendar requires a second option chain fetch (two expiries).
    This function records the structure — bot.py handles the two-expiry fetch.

    Returns a partial order dict with the target strikes.
    Full execution details are completed in bot.py after chain fetch."""
    price   = data.get('price', 0.0)
    avg_iv  = data.get('avg_iv', 30.0)
    symbol  = data.get('symbol', '')

    # For calendar: ATM strike (closest to spot)
    # Round to nearest standard strike interval
    if price < 20:
        interval = 0.50
    elif price < 50:
        interval = 1.0
    elif price < 100:
        interval = 1.0
    else:
        interval = 5.0

    atm_strike = round(round(price / interval) * interval, 2)

    contracts = signal.get('contracts', 1)

    # Estimated debit: calendar typically costs 1–2% of stock price
    est_debit = round(price * 0.015, 2)
    max_loss  = round(est_debit * 100 * contracts, 2)

    log.info(
        f'{symbol}: calendar spread — ATM={atm_strike} '
        f'front_dte≈{CAL_DTE_FRONT} back_dte≈{CAL_DTE_BACK} '
        f'est_debit=${est_debit:.2f}'
    )

    return {
        'strategy':          'calendar_spread',
        'symbol':            symbol,
        'price':             price,
        'atm_strike':        atm_strike,
        'front_dte_target':  CAL_DTE_FRONT,   # ~30 DTE (sell this)
        'back_dte_target':   CAL_DTE_BACK,    # ~60 DTE (buy this)
        'contracts':         contracts,
        'est_debit':         est_debit,
        'max_loss':          max_loss,
        'iv':                avg_iv,
        'ivr':               data.get('ivr', 0.0),
        'ivr_regime':        data.get('ivr_regime', ''),
        'bias':              data.get('bias', 'NEUTRAL'),
        # Management
        'profit_target_pct': PROFIT_TARGET_CALENDAR,   # 0.15 = 15% of debit paid
        'loss_stop_pct':     0.50,
        'dte_exit':          DTE_EXIT,
        # Flags for bot.py to know this needs a two-expiry chain fetch
        'needs_two_expiries': True,
    }


# ── Dispatcher ─────────────────────────────────────────────────────

def build_order(data, signal):
    """Dispatch to the correct builder based on signal['strategy'].
    Returns finalized order dict or None.

    Called by bot.py after signals.py produces a valid signal."""
    strategy = signal.get('strategy', '')
    sub_type = signal.get('sub_type', '')

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
