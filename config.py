import os, pytz, logging
from dotenv import load_dotenv

load_dotenv('/home/trader/kubera/.env')

# ── TastyTrade ────────────────────────────────────────────────────
TT_CLIENT_SECRET  = os.getenv('TT_CLIENT_SECRET')
TT_REFRESH_TOKEN  = os.getenv('TT_REFRESH_TOKEN')
TT_ACCOUNT        = os.getenv('TT_ACCOUNT_NUMBER')
TT_SANDBOX        = os.getenv('TT_SANDBOX', 'true').lower() == 'true'

# ── Telegram ──────────────────────────────────────────────────────
TG_TOKEN          = os.getenv('TELEGRAM_BOT_TOKEN')
TG_CHAT           = str(os.getenv('TELEGRAM_CHAT_ID'))

# ── Account ───────────────────────────────────────────────────────
REAL_BALANCE      = float(os.getenv('REAL_ACCOUNT_BALANCE', 5000))
KILL_SWITCH       = float(os.getenv('KILL_SWITCH_BALANCE', 3600))
# Kill switch: halt all new entries when NLV ≤ this value.
# Set via .env KILL_SWITCH_BALANCE — .env is the single source of truth.

# ═══════════════════════════════════════════════════════════════════
# IVR CLASSIFICATION
# Source: TastyTrade Mega Strangle Study — IVR 50 was the dividing
# line between high and low IV groups. Results were materially better
# above this threshold. No hard floor is published by TastyTrade for
# the minimum trade threshold, but the study cutoff is used here.
# ═══════════════════════════════════════════════════════════════════

IVR_HIGH          = 50.0   # IVR ≥ this → HIGH regime → sell premium aggressively
                            # IC, Jade Lizard, put/call credit spread all valid
IVR_MEDIUM_FLOOR  = 25.0   # IVR ≥ this but < IVR_HIGH → MEDIUM → directional credit only
                            # Put or call credit spread only; smaller size; no IC
IVR_LOW_CEILING   = 25.0   # IVR < this → LOW regime → buy premium
                            # Debit spreads (price at extreme) or calendar (IV compression)

# ═══════════════════════════════════════════════════════════════════
# DTE PARAMETERS
# Source: TastyTrade — 25–50 DTE range, 45 DTE "sweet spot" (definitions page).
# 21 DTE exit from Market Measures study: biggest losses all occurred
# inside 21 DTE due to gamma acceleration. Exit before regardless of P&L.
# ═══════════════════════════════════════════════════════════════════

DTE_MIN           = 25     # minimum DTE at entry
DTE_MAX           = 50     # maximum DTE at entry
DTE_SWEET_SPOT    = 45     # preferred target DTE — select expiry nearest to this
DTE_EXIT          = 21     # time-based exit: close position before this DTE

# ═══════════════════════════════════════════════════════════════════
# STRIKE SELECTION
# Source: TastyTrade standard deviation page — 16 delta = 1 SD OTM = ~84% POP.
# The 1/3-width credit rule for ICs implies a minimum ~67% POP.
# Short strike is placed at TARGET_DELTA; wing is sized by expected move.
# ═══════════════════════════════════════════════════════════════════

TARGET_DELTA      = 0.16   # short strike delta — 16 delta ≈ 84% POP (1 SD OTM)
MIN_POP           = 0.67   # minimum probability of profit to enter any trade
                            # TT: 1/3-width credit rule → ~67% POP minimum

# ═══════════════════════════════════════════════════════════════════
# STRATEGY SELECTION LOGIC
# The scanner assesses IVR + directional bias and selects the optimal
# strategy. This is not a manual override — it is determined per scan.
#
# HIGH IVR + bullish  → attempt Jade Lizard, fall back to put credit spread
# HIGH IVR + bearish  → call credit spread
# HIGH IVR + neutral  → iron condor
# MEDIUM IVR + bull   → put credit spread (reduced size)
# MEDIUM IVR + bear   → call credit spread (reduced size)
# MEDIUM IVR + neutral→ SKIP (insufficient premium for IC risk)
# LOW IVR + extreme   → debit spread (call if oversold, put if overbought)
# LOW IVR + quiet     → calendar spread (bet on IV expansion)
# LOW IVR + no signal → SKIP
# ═══════════════════════════════════════════════════════════════════

# ── Jade Lizard ───────────────────────────────────────────────────
# TastyTrade: sell OTM put + sell OTM call spread.
# "Perfect" condition: total credit ≥ call spread width → zero upside risk.
# The scanner attempts a Jade Lizard in HIGH IVR + bullish conditions.
# If the credit math does not work, it falls back to a put credit spread.
JADE_MIN_CREDIT_RATIO  = 1.0    # (put premium + call spread premium) / call spread width ≥ 1.0

# ── Iron Condor ───────────────────────────────────────────────────
# TastyTrade: collect ≥ 1/3 the spread width as credit — implies ~67% POP.
# Only valid in HIGH IVR + neutral bias. Block above IC_MAX_IVR:
# extreme IV means market is pricing a large move → IC thesis is wrong.
IC_CREDIT_RATIO        = 0.333  # minimum credit / spread width ratio (1/3 rule)
IC_MAX_IVR             = 80.0   # block IC when IVR > this (extreme vol = directional move expected)

# ── Calendar Spread ───────────────────────────────────────────────
# TastyTrade: enter in LOW IVR environments when stock has been abnormally quiet.
# Profit target 10–25% of debit paid (source: TT calendar spread page).
# Sell front-month, buy back-month at same strike.
CAL_DTE_FRONT         = 30     # front month target DTE (sell this)
CAL_DTE_BACK          = 60     # back month target DTE (buy this)
CAL_IV_MAX            = 25.0   # only enter calendar when IV < this (low IV required)

# ═══════════════════════════════════════════════════════════════════
# PROFIT TARGETS
# Source: TastyTrade published rules (tastylive.com how-to-use-options article).
# Management rule: whichever comes first — profit target OR 21 DTE exit.
# ═══════════════════════════════════════════════════════════════════

PROFIT_TARGET_CREDIT    = 0.50  # credit spreads / IC / Jade Lizard / strangles: 50% of max credit
PROFIT_TARGET_DEBIT     = 0.25  # debit spreads: 25% of debit paid (TT range 25–50%; use conservative end)
PROFIT_TARGET_CALENDAR  = 0.15  # calendar spreads: 15% of debit paid (TT range 10–25%)
PROFIT_TARGET_BUTTERFLY = 0.25  # butterflies: 25% of max profit

# ═══════════════════════════════════════════════════════════════════
# LOSS MANAGEMENT
# Source: TastyTrade Market Measures research.
# 2x credit stop: widely cited from their studies (not on current site pages).
# Roll trigger: POP < 33% — confirmed on TT rolling definition page.
# P&L-based stops for strangles/credits: research showed they underperform
# buy-and-hold SPY by 40% — do NOT use tight stops, use DTE exit instead.
# ═══════════════════════════════════════════════════════════════════

LOSS_STOP_MULTIPLIER   = 2.0    # close credit trade when mark loss = 2× credit received
ROLL_POP_FLOOR         = 0.33   # roll when current POP drops below 33%
DEBIT_HARD_STOP_PCT    = 0.50   # close debit spread if loss > 50% of debit paid
# Rolling rules (TastyTrade):
# - Only roll for a credit, never for a debit
# - Only roll if the thesis is unchanged
# - Never double contracts when rolling — roll for duration only

# ═══════════════════════════════════════════════════════════════════
# DIRECTIONAL BIAS DETECTION
# TastyTrade uses three signals to determine directional bias.
# No RSI, MACD, ADX, EMA, Ichimoku, or volume triggers are used.
# Two of three signals in agreement → that bias is confirmed.
# All three mixed → neutral.
# ═══════════════════════════════════════════════════════════════════

# Signal 1: Recent price action
# TastyTrade "selling puts in beat-up stocks" study: 5%+ weekly decline
# increased IBM win rate from 80% to 91%. A stock that falls 5%+ in a week
# is a mean reversion candidate → bullish put-selling setup.
BIAS_MOVE_THRESHOLD    = 0.05   # 5-day return ≥ +5% = bearish signal; ≤ -5% = bullish signal

# Signal 2: VIX direction
# Rising VIX = fear building = bearish pressure. Falling VIX = fear receding = bullish.
BIAS_VIX_LOOKBACK      = 5      # compare current VIX to 5 days ago to determine direction
BIAS_VIX_MOVE_PCT      = 0.10   # VIX must move ≥ 10% over lookback to count as directional

# Signal 3: Volatility skew
# TastyTrade: puts trade at higher IV than equidistant calls due to skew.
# When put IV is significantly richer → favor put selling (bullish bias).
# When call IV is unusually rich vs puts → unusual bearish hedging demand.
BIAS_SKEW_RATIO        = 1.15   # put_iv / call_iv ≥ this = puts "rich" → bullish bias (sell puts)
BIAS_SKEW_NEUTRAL_LOW  = 0.95   # below this threshold: call IV unusually rich → bearish signal

# ═══════════════════════════════════════════════════════════════════
# POSITION SIZING
# TastyTrade: "trade small, trade often." No per-trade % published.
# Portfolio theta target: 0.1% of NLV/day (managing-portfolio-theta article).
# Kubera uses BPR cap: 5% of account NLV per trade (from TT expected-returns
# modeling scenario: 5% of portfolio deployed per occurrence).
# Sizing does NOT vary by grade — there is no grade system in Kubera.
# All trades are sized equally; conviction is expressed by strategy choice,
# not by risking more capital per trade.
# ═══════════════════════════════════════════════════════════════════

MAX_BPR_PCT            = 0.05   # max buying power reduction per trade = 5% of NLV
MIN_BPR_GATE_PCT       = 0.15   # skip symbol if 1-contract BPR > 15% of NLV
                                 # catches MSFT/META/BA where 10-wide spread = $800–1,200 BPR
                                 # at 5% target ($287), flooring to 1 would deploy 3× the cap
MAX_POSITIONS          = 10     # maximum concurrent open positions
MAX_SECTOR_POSITIONS   = 3      # maximum positions in the same sector simultaneously
PORTFOLIO_THETA_TARGET = 0.001  # target: 0.1% of NLV per day in portfolio theta
                                 # e.g., $3,800 account → target $3.80/day theta
MAX_CAPITAL_DEPLOYED   = 0.50   # no new entries when deployed ≥ 50% of balance
                                 # Kubera: more conservative than Nexus (Nexus was 85%)
                                 # TastyTrade: "stay small" — never over-concentrate

# ═══════════════════════════════════════════════════════════════════
# RISK GUARDRAILS
# ═══════════════════════════════════════════════════════════════════

EARNINGS_BLOCK         = 7      # no new entries within 7 days of earnings announcement
                                 # TastyTrade: earnings plays are closed the open after the announcement
                                 # For premium selling, avoid being in the position through earnings
MIN_OPTION_OI          = 300    # minimum open interest on the short strike — liquidity gate
MAX_SPREAD_PCT         = 40.0   # max bid-ask spread as % of mid — fill quality gate
CONSEC_LOSS_PAUSE      = 3      # pause entries after 3 consecutive losses — reassess conditions

# Safe-haven assets: rally during market stress. Only put credits valid on these.
# Selling calls on TLT/GLD when VIX spikes = structurally wrong direction.
SAFE_HAVEN_SYMBOLS = frozenset({'TLT', 'GLD', 'SLV', 'GDX', 'IAU'})

# Permanently blocked — binary event risk, not suitable for premium selling
BLOCKED_SYMBOLS = frozenset({
    'MRNA', 'AMGN', 'VRTX',   # biotech binary drug-approval risk
    'BIIB', 'REGN',
    'INCY', 'EXEL',
    'HOLX', 'HIMS',
})

# ═══════════════════════════════════════════════════════════════════
# EXPECTED MOVE FORMULA
# Source: tastylive.com/definitions/calculating-expected-move
# EM = Stock Price × (IV / 100) × √(DTE / 365)
# This represents 1 standard deviation (68% probability).
# Short strikes are placed at or beyond the expected move.
# Calculated on-the-fly in market.py using live IV and DTE.
# No config constant needed — formula is fixed.
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# WATCHLIST  —  liquid options universe for Kubera scans
# Criteria: avg stock vol ≥ 1M/day, OI ≥ 300 ATM at target delta,
#           tight bid-ask, TastyTrade chain confirmed
# Inherited from Nexus X v2.4 — biotech firewall applied above
# ═══════════════════════════════════════════════════════════════════

WATCHLIST = [

    # ── BROAD MARKET ETFs
    'SPY', 'QQQ', 'IWM', 'XSP', 'DIA',

    # ── MACRO / RATES / COMMODITIES
    'TLT', 'GLD', 'SLV', 'GDX', 'HYG',

    # ── SECTOR ETFs
    'XLE', 'XLF', 'XLK', 'XLV', 'XLI', 'XLP', 'XLU', 'XLC', 'XLY',
    'EEM', 'ARKK', 'XBI', 'IBB', 'XHB', 'SMH',
    'KRE', 'JETS',

    # ── MEGA-CAP TECH
    'NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOG', 'TSLA',
    'AVGO', 'AMD', 'PLTR',

    # ── MID-CAP TECH / SAAS / CRYPTO
    'MU', 'TXN', 'NFLX', 'CRM', 'NOW', 'ADBE', 'FTNT',
    'TTD', 'DELL', 'COIN', 'MARA',

    # ── FINANCIALS
    'JPM', 'BAC', 'WFC', 'C', 'GS', 'MS',
    'V', 'MA', 'PYPL', 'HOOD', 'SOFI',

    # ── HEALTHCARE
    'LLY', 'UNH', 'MRK', 'JNJ', 'ABBV',

    # ── ENERGY
    'XOM', 'COP', 'EOG', 'OXY',

    # ── CONSUMER DISCRETIONARY
    'HD', 'MCD', 'SBUX', 'NKE', 'CMG',
    'TGT', 'WMT', 'COST', 'DIS',
    'UBER', 'AAL', 'CCL', 'RIVN',

    # ── CONSUMER STAPLES
    'PG', 'PEP',

    # ── INDUSTRIALS
    'CAT', 'HON', 'GD', 'UPS', 'BA',

    # ── TECH (extended)
    'GOOGL', 'ADSK', 'ANET', 'CRWD', 'ORCL', 'PANW', 'SPOT',
    'EA', 'BKNG', 'INTC', 'SHOP', 'QCOM', 'IBM',

    # ── FINANCE (extended)
    'AXP', 'CME', 'SCHW', 'COF',

    # ── HEALTH (extended)
    'ISRG', 'HCA', 'ABT', 'BSX',

    # ── ENERGY (extended)
    'VLO', 'CVX', 'XOM', 'SLB', 'HAL',

    # ── CONSUMER (extended)
    'ABNB', 'DHI', 'LEN', 'FDX',

    # ── INDUSTRIALS (extended)
    'LMT', 'RTX', 'GE', 'MMM',

    # ── SPECULATIVE (high-beta)
    'MSTR', 'RBLX',
]

# Dedup — preserves order
WATCHLIST = list(dict.fromkeys(
    [s for s in WATCHLIST if s not in BLOCKED_SYMBOLS]
))

# ── Sector map ─────────────────────────────────────────────────────
SECTOR_MAP = {
    # Broad market
    'SPY':'BroadMarket', 'QQQ':'BroadMarket', 'IWM':'BroadMarket',
    'XSP':'BroadMarket', 'DIA':'BroadMarket',
    # Macro
    'TLT':'Macro', 'GLD':'Macro', 'SLV':'Macro', 'HYG':'Macro', 'GDX':'Macro',
    # Sector ETFs
    'XLE':'Energy',  'XLF':'Finance', 'XLK':'Tech',    'XLY':'Consumer',
    'XLV':'Health',  'XLI':'Indust',  'XLP':'Staples', 'XLU':'Util',
    'XLC':'Comms',   'EEM':'EM',      'ARKK':'Specul', 'XBI':'Health',
    'IBB':'Health',  'XHB':'Consumer','SMH':'Tech',    'KRE':'Finance',
    'JETS':'Consumer',
    # Mega tech
    'NVDA':'Tech', 'AAPL':'Tech', 'MSFT':'Tech', 'AMZN':'Tech',
    'META':'Tech', 'GOOG':'Tech', 'GOOGL':'Tech','TSLA':'Tech',
    'AVGO':'Tech', 'AMD':'Tech',  'PLTR':'Tech',
    # Mid tech / saas / crypto
    'MU':'Tech',   'TXN':'Tech',  'NFLX':'Tech', 'CRM':'Tech',
    'NOW':'Tech',  'ADBE':'Tech', 'FTNT':'Tech', 'TTD':'Tech',
    'DELL':'Tech', 'COIN':'Tech', 'MARA':'Specul',
    'ADSK':'Tech', 'ANET':'Tech', 'CRWD':'Tech', 'ORCL':'Tech',
    'PANW':'Tech', 'SPOT':'Tech', 'EA':'Tech',   'INTC':'Tech',
    'SHOP':'Tech', 'QCOM':'Tech', 'IBM':'Tech',
    # Finance
    'JPM':'Finance', 'BAC':'Finance', 'WFC':'Finance', 'C':'Finance',
    'GS':'Finance',  'MS':'Finance',  'V':'Finance',   'MA':'Finance',
    'PYPL':'Finance','HOOD':'Finance','SOFI':'Finance', 'AXP':'Finance',
    'CME':'Finance', 'SCHW':'Finance','COF':'Finance',
    # Health
    'LLY':'Health', 'UNH':'Health', 'MRK':'Health', 'JNJ':'Health',
    'ABBV':'Health','ISRG':'Health','HCA':'Health', 'ABT':'Health',
    'BSX':'Health',
    # Energy
    'XOM':'Energy', 'COP':'Energy', 'EOG':'Energy', 'OXY':'Energy',
    'VLO':'Energy', 'CVX':'Energy', 'SLB':'Energy', 'HAL':'Energy',
    # Consumer
    'HD':'Consumer',   'MCD':'Consumer',  'SBUX':'Consumer', 'NKE':'Consumer',
    'CMG':'Consumer',  'TGT':'Consumer',  'WMT':'Consumer',  'COST':'Consumer',
    'DIS':'Consumer',  'UBER':'Consumer', 'AAL':'Consumer',  'CCL':'Consumer',
    'RIVN':'Consumer', 'BKNG':'Consumer', 'ABNB':'Consumer', 'DHI':'Consumer',
    'LEN':'Consumer',  'FDX':'Consumer',
    # Staples
    'PG':'Staples', 'PEP':'Staples',
    # Industrials
    'CAT':'Indust', 'HON':'Indust', 'GD':'Indust',  'UPS':'Indust',
    'BA':'Indust',  'LMT':'Indust', 'RTX':'Indust', 'GE':'Indust',
    'MMM':'Indust',
    # Speculative
    'MSTR':'Specul', 'RBLX':'Specul',
}

# ── ETF / Stock classification ──────────────────────────────────────
ETF_SYMBOLS = [
    'SPY','QQQ','IWM','XSP','DIA',
    'TLT','GLD','SLV','GDX','HYG',
    'XLE','XLF','XLK','XLV','XLI','XLP','XLU','XLC','XLY',
    'EEM','ARKK','XBI','IBB','XHB','SMH','KRE','JETS',
]
STOCK_SYMBOLS = [s for s in WATCHLIST if s not in ETF_SYMBOLS]

# ── Liquidity tiers ────────────────────────────────────────────────
# Tier 1: OI >10K — full size
# Tier 2: OI 2K–10K — full size
# Tier 3: OI 500–2K — scan only; place only if MIN_OPTION_OI passes
LIQUIDITY_TIER = {
    1: [
        'SPY','QQQ','IWM','XSP','NVDA','AAPL','MSFT','AMZN',
        'META','GOOG','TSLA','AVGO','AMD','TLT','XLF','BAC',
        'JPM','V','MA','XLE','GDX','PLTR','SMH',
    ],
    2: [
        'DIA','XLK','XLV','XLI','XLP','XLC','XLY','EEM','XBI','GLD','HYG',
        'SLV','MU','NFLX','CRM','WFC','C','GS','MS','PYPL',
        'LLY','UNH','XOM','COP','HD','MCD','WMT','COST','TGT',
        'NKE','SBUX','DIS','MRK','JNJ','CAT','HON','UPS','TXN',
        'NOW','SOFI','HOOD','AAL','CCL','EOG','OXY','UBER',
        'XLU','ABBV','ADBE','PG','PEP','KRE','JETS',
        'GOOGL','ADSK','ANET','CRWD','ORCL','PANW','SPOT','EA',
        'BKNG','INTC','SHOP','QCOM','IBM','AXP','CME','SCHW','COF',
        'ISRG','HCA','ABT','BSX','VLO','CVX','SLB','HAL',
        'ABNB','DHI','LEN','FDX','LMT','RTX','GE','MMM',
    ],
    3: [
        'ARKK','IBB','XHB','FTNT','TTD','DELL','COIN','GD','BA',
        'CMG','MARA','RIVN','MSTR','RBLX',
    ],
}

# ── Timezones ──────────────────────────────────────────────────────
ET  = pytz.timezone('America/New_York')
UTC = pytz.utc
FR  = pytz.timezone('Europe/Paris')

# ── Logging ────────────────────────────────────────────────────────
os.makedirs('/home/trader/kubera/data', exist_ok=True)
os.makedirs('/home/trader/kubera/logs', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('/home/trader/kubera/logs/kubera.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger('kubera')
