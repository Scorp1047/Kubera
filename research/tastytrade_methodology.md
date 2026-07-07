# TastyTrade / TastyLive — Core Methodology Reference

**Researched:** 2026-07-07  
**Sources:** tastylive.com definition pages, concept pages, FAQ, blog (news-insights), and Market Measures study summaries. All facts pulled directly from published content. Where a number was not found, it is stated as "not found."

---

## 1. Core Philosophy

**Governing principle:** Sell options when IV is high; buy when IV is low.

From their FAQ verbatim:  
> "We primarily focus on options trading, with the underlying strategy being selling options rather than buying them to improve our cost basis and probability of success."

**The five pillars:**

1. **Sell premium in high IV** — IV historically overestimates realized volatility. Implied > realized is the persistent statistical edge.
2. **Trade small, trade often** — More occurrences allow the statistical edge to play out. Individual trade size is kept small to avoid blow-ups.
3. **Manage winners early** — Do not hold to expiration. Take profit at 50% of max profit.
4. **Reduce cost basis** — Continuously sell premium against existing positions (covered calls, rolls) to improve breakeven.
5. **Duration > direction** — Having time to be right matters more than directional accuracy.

---

## 2. Credit Spreads and Iron Condors — Entry Criteria

| Parameter | TastyTrade Rule | Source |
|-----------|----------------|--------|
| DTE entry range | 25–50 DTE | tastylive.com/definitions/days-to-expiration-dte |
| DTE entry sweet spot | **45 DTE** | tastylive.com/definitions/days-to-expiration-dte |
| DTE exit (time-based) | **21 DTE** — exit before this to avoid gamma acceleration | alternative-to-managing-losers; how-to-use-options-strategies |
| Profit target (credit trades) | **50% of max credit received** | tastylive.com (multiple articles) |
| Profit target (butterflies / BWB / diagonal) | **25% of max profit** | how-to-use-options-strategies |
| Profit target (ratio spreads) | **30% of max profit** | how-to-use-options-strategies |
| IVR for entry | High IV preferred; **no specific published floor** | Various |
| IVR study cutoff (Mega Strangle Study) | IVR 50 was the split used: ≥50 vs ≤50 — results better in high IV | our-3-favorite-strangle-studies |
| Delta of short strike | Not explicitly stated; standard deviation page implies **16 delta = 1 SD = ~84% POP** | tastylive.com/definitions/standard-deviation |
| Spread width rule (IC / vertical) | Collect **1/3 the spread width** as credit minimum → implies ~67% POP | big-boy-IC concept pages |
| Roll trigger | **POP drops below 33%** | tastylive.com/definitions/rolling-options |
| Loss stop (Market Measures research) | **2x the credit received** — cited from research, NOT on current definition pages | historical Market Measures studies |
| POP minimum for entry | **POP > 50%** on all entries | tastylive.com (multiple) |

**Management rule (strangles / IC):**  
Whichever comes first: **50% profit target** OR **21 DTE exit**.

**Directional selection for put credit spreads:**  
TastyTrade favors selling puts because of volatility skew — puts trade at higher IV than equidistant calls, making put premium "rich" relative to calls. This is the rationale for the Jade Lizard.

---

## 3. Iron Condor vs Credit Spread — When to Use Each

- **Iron condor:** Neutral-bias tool. Preferred when no directional conviction and IV is high. Profits from stock staying in a range.
- **Vertical credit spread (put or call):** Directional tool. Use when you have directional conviction (bullish → sell put spread; bearish → sell call spread).
- **Skewed iron condor:** When IV is high but you have a directional lean — widen one side and tighten the other.
- **No hard rule published** based on IVR level for IC vs vertical. The choice is qualitative (neutral vs directional bias).

**Backtesting study result** (tastylive.com/news-insights/backtesting-duration-in-credit-spreads):  
Tested 15 DTE, 45 DTE, and 75 DTE SPY credit spreads at 30 and 10 delta.  
- Put spread win rates: **all three DTE categories ≥88%**  
- Call spreads: 45 DTE produced the **highest average P&L** across all DTE tested  
- Put spreads outperformed call spreads across all DTE due to market's positive drift  
- Specific P&L figures: not published in the article

---

## 4. Mean Reversion / Debit Trades

- **Only deployed in low IV environments** — when IVR is at "extreme lows," buy debit spreads or calendar spreads.
- **Mean reversion framing:** Price extremes and volatility extremes revert to the mean. When a stock moves sharply, fade the move using debit spreads.
- **Key rule:** Ignore binary events when identifying mean reversion setups — earnings, FDA events, etc. are NOT mean reversion candidates.
- **IV-adjusted strategy selection:**
  - Low IV → debit spread (long call spread on oversold bounce, long put spread on overbought rejection)
  - High IV → credit spread on same directional thesis
- **Profit target for calendar spreads (low IV):** **10–25% of the debit paid** (below 10% = insufficient; above 25% = happens too rarely)
- **What signals do they use for mean reversion entry?** Their definition page does not specify RSI, MACD, or any indicator. The signal is **price at an extreme relative to recent range**, combined with IV at an extreme low. **No specific indicator is named.**

---

## 5. Strangle Management — Specific Rules

| Rule | Value | Source |
|------|-------|--------|
| Entry DTE | 45 DTE | tastylive.com (multiple) |
| Profit target | 50% of max credit | tastylive.com/news-insights/managing-strangles |
| DTE exit | 21 DTE | alternative-to-managing-losers |
| Management rule | 50% profit OR 21 DTE — whichever first | how-to-use-options-strategies |
| Strike selection | 16 delta (1 SD = ~84% POP); also 30 delta used | tastylive.com |
| Rolling | Roll for credit only; roll if POP < 33%; do not double contracts | tastylive.com/definitions/rolling-options |
| P&L-based stop | **Explicitly not recommended** — research showed it underperformed buy-and-hold SPY by 40% | alternative-to-managing-losers |

---

## 6. Position Management Rules (All Strategies)

**Winners:**
| Strategy | Close At |
|----------|---------|
| Iron condors, OTM credit spreads | 50% of max credit received |
| Butterflies, defined-risk complex | 25–50% of max profit |
| Ratio spreads | 30% of max profit |
| Calendar spreads | 10–25% of the debit paid |
| Earnings plays | On the open after the announcement (timing rule, not % target) |

**Losers:**
- Roll when POP drops below 33%
- Only roll for a credit — rolling for a debit worsens breakeven
- Only roll if the thesis is unchanged — if you've changed your mind, close it
- Do not roll defined-risk spreads in most cases (exception: short strike slightly ITM but long strike still OTM)
- The "2x the credit received" hard stop is from their Market Measures research but is **not explicitly stated on current accessible definition pages**

**Time-based exit:**
- Enter at 45 DTE. Exit at 21 DTE to avoid gamma risk acceleration. The 21 DTE rule was established by their research showing the biggest losing trades all occurred within 21 DTE of expiration.

---

## 7. Market Measures Research — Key Study Results

### Strangle Study — SPY, 45 DTE, 1 SD (16 delta), back to 2005
Source: `tastylive.com/news-insights/why-manage-at-50-not-25-for-short-strangles`

| Management Style | Win Rate | Avg Hold Time |
|-----------------|----------|---------------|
| Managed at 25% of max profit | **95%** | ~13.5 days |
| Managed at 50% of max profit | **90%** | ~23.5 days |
| Held to expiration | **82%** | full term |

**Key finding:** Managing at 50% gives a lower win rate than 25% but **daily P&L is ~77% greater** than managing at 25%.

---

### 3 Favorite Strangle Studies
Source: `tastylive.com/news-insights/our-3-favorite-strangle-studies`

**Study 1 — Mega Strangle Study (May 2014):**
- 300+ trades over 5 years, 45 DTE, 16 delta, POP at entry ~68% (POP including credit >70%)
- Split by IVR ≥50 vs IVR ≤50: results were better in high IV
- Specific win rates by IVR group: **not stated numerically** in the article

**Study 2 (Oct 2014):**
- 262 SPY strangles placed every 5 days, POP at entry ~70%
- Actual win rate: **83%**
- Total P&L: **+$20,333**

**Study 3 (Dec 2014):**
- Strangles at 15, 25, and 45 delta
- 79% of trades experienced unrealized losses at some point before expiration
- Final win rate at expiration: **83%**

---

### Realistic Expectations Study
Source: `tastylive.com/news-insights/formula-for-realistic-expectations-market-measures`

- 1,000 occurrences tested, 30 DTE
- 1 SD strangles in SPY collect ~$125 credit on average
- 50% profit target = $62.50 per trade
- 2x credit stop = $250 loss
- Expected P&L per trade: **~$31.25**
- Win rate: **~90%** (stated as "ten or eleven months out of the year")

---

### Blue-Chip Put Selling / Selling Puts in Beat-Up Stocks
Sources: `tastylive.com/news-insights/blue-chip-put-sales` and `tastylive.com/news-insights/selling-puts-in-beat-up-stocks`

- Strategy: sell puts at 1 SD expected move, 45 DTE, manage at 50% profit
- IBM: **80% win rate** in all environments over 10 years
- After 5%+ weekly decline filter: **win rate jumped to 91%** in IBM
- MMM median P&L without filter: $1.35 credit
- MMM with 5%+ down filter: $1.87 credit; max loss reduced from -$17.82 to -$13.20

---

### Stop-Loss Study — P&L-Based Management vs Time-Based
Source: `tastylive.com/news-insights/alternative-to-managing-losers`

- Examined managing strangles with stop losses at 1x–5x the credit received
- Result: **underperformed buy-and-hold SPY by 40%**
- Biggest losing trades all occurred within approximately **21 DTE**
- Conclusion: managing by P&L loss size is ineffective; recommended approach = exit before 21 DTE (time-based)

---

### Credit Spread Duration Backtest
Source: `tastylive.com/news-insights/backtesting-duration-in-credit-spreads`

- Tested 15 DTE, 45 DTE, and 75 DTE SPY credit spreads at 30/10 delta
- Put spread win rates: **all categories ≥88%**
- Call spreads: 45 DTE highest average P&L
- Specific P&L numbers: not published in the article

---

## 8. Indicators They USE

| Indicator | Role |
|-----------|------|
| **IV Rank (IVR)** | PRIMARY entry filter. "Looking at IV rank is a best practice of ours because it provides context." Determines credit vs debit strategy. |
| **IV Percentile** | Secondary to IVR; used interchangeably |
| **Implied Volatility (absolute)** | Used for expected move calculation |
| **Standard deviation / expected move** | Strike selection. One-SD EM defines the "zone" they sell around. |
| **Delta** | Probability proxy. 16 delta = ~84% POP; 30 delta = ~70% POP |
| **Probability of Profit (POP)** | Main strike selection tool, displayed directly in platform |
| **Theta** | Monitored as a portfolio-level metric (positive theta = positive cash flow from time decay) |
| **VIX** | Used directionally to anticipate market regimes, not a precise trigger |
| **Volatility skew** | To identify "rich" puts vs "cheap" calls (Jade Lizard rationale) |

---

## 9. Indicators They Do NOT Use

TastyTrade's entire system is built on options-specific metrics. Their technical analysis definition page is deliberately neutral/minimal. None of the following are mentioned in any accessible tastylive content:

- **RSI** — never mentioned
- **MACD** — never mentioned
- **Moving averages (EMA, SMA)** — never mentioned
- **ADX** — never mentioned
- **Bollinger Bands** — never mentioned
- **Ichimoku** — never mentioned
- **Chart patterns** — never mentioned
- **Volume** — never mentioned as a trading trigger

---

## 10. What They Explicitly Reject

- **Predicting direction** — "duration trumps direction over time." They do not rely on being directionally correct.
- **Holding to expiration** — "manage winners early." Expiration is almost never the target.
- **Large concentrated positions** — "stay small." A 60% loss requires a 150% recovery.
- **Buying premium in high IV** — High IV = sell. Buying in high IV means overpaying for time decay.
- **Weekly options for income** — Explicitly discouraged. Four weekly trades underperform one monthly trade due to lower theta and worse liquidity. Weeklies only for earnings plays.
- **Doubling contracts when rolling** — "We do not double our risk by doubling our contracts, we simply roll for duration."
- **P&L-based stop losses** — Research showed this underperformed buy-and-hold by 40%.
- **Technical analysis as primary filter** — Implied by complete absence from their methodology.

---

## 11. Portfolio Construction

| Rule | Detail | Source |
|------|--------|--------|
| Position size philosophy | "Trade small, trade often." No specific % per trade published. | tastylive.com/definitions/staying-small |
| Portfolio theta target | **0.1% of account NLV per day** (e.g., $100/day theta for $100k account) | tastylive.com/news-insights/managing-portfolio-theta |
| Annual return via theta | ~25% of collected theta is realized annually (implied from theta target) | managing-portfolio-theta |
| Scaling up | Widen strike widths, add positions, use undefined risk, extend duration | tastylive.com |
| Scaling down | Reduce all sizes proportionally during drawdowns | tastylive.com |
| Buying power per trade | **Not published as a % of account** | Not found |
| Maximum positions | **Not specified** — philosophy favors many small over few large | Not found |
| Portfolio delta target | **Not found** — no specific target number published | Not found |
| Correlation | Implied — avoid concentration in same sector/direction. No numerical limit published. | implied |

**Expected returns modeling scenarios** (NOT official rules — illustrative only):  
Source: `tastylive.com/definitions/expected-returns`
- 1% daily return on 5% of portfolio deployed → ~18.25% annualized
- 0.33% daily return on 20% of portfolio deployed → ~24.33% annualized

---

## 12. Jade Lizard — Exact Construction

Source: `tastylive.com/news-insights/the-jade-lizard-market-measures` and `tastylive.com/concepts-strategies/jade-lizard`

**Structure:**
- Sell 1 OTM put + sell 1 OTM call spread (short OTM call + long higher-strike call) in the same expiration
- Slightly bullish bias

**Example (from the article):**  
Stock at $10 → sell $8 put + sell $11/$12 call spread = $1.00 total credit on a $1-wide call spread

**What makes it "perfect" (no upside risk):**  
Total credit received must be **≥ the width of the call spread**  
- In the example: $1.00 credit = $1.00 spread width → zero upside risk (breakeven at the short call strike)
- If credit > spread width → no upside risk at all; only risk is the downside from the short put

**Why preferred over a simple put credit spread:**  
- Collects additional premium from the call spread, widening the profitable range
- Compared to a strangle: the long call caps the upside risk
- Gives a credit larger than a simple put spread alone with defined upside risk

Win rate, DTE, and IVR requirements for Jade Lizard: **not published** in accessible tastylive articles.

---

## 13. Expected Move Formula

Sources: `tastylive.com/definitions/calculating-expected-move`, `tastylive.com/news-insights/expected-move-sanity-checking-trade-ideas`, `tastylive.com/news-insights/options-earnings`

**Method 1 — Continuous markets (standard):**
```
EM = Stock Price × (IV / 100) × √(DTE / 365)
```
Example: SPY at $279, IV = 15%, 45 DTE → EM = $279 × 0.15 × √(45/365) = **±$14.69**  
This represents 1 standard deviation (68% probability).

**Method 2 — Binary events (earnings):**
```
EM = (ATM Call Price + ATM Put Price) × 0.85
```
i.e., 85% of the ATM straddle price.

**Alternative Method 2b:**
```
EM = (ATM Straddle + 1st OTM Strangle) ÷ 2
```

**Application:** Short strikes are placed beyond the expected move (outside the EM range) to maintain high probability of the underlying staying within strikes.

---

## 14. Undefined Risk (Naked Options)

Source: `tastylive.com/definitions/undefined-risk`, `tastylive.com/news-insights/naked-risk-is-slightly-clothed`, `tastylive.com/news-insights/selecting-strategies-based-on-risk-vs-reward`

- **No minimum account size published** for undefined risk strategies
- Naked puts are described as "slightly clothed" — max loss is capped at stock going to zero; naked calls are truly unlimited
- The practical framework: use probability (1 SD, 2 SD moves) to select strikes; small size makes the risk manageable
- "Selecting strategies based on risk vs reward" article mentions splitting capital into buckets (lower-risk and higher-risk with "substantially less capital") but **gives no specific % split**
- What drives the choice in practice: account size determines margin availability; philosophy is that undefined risk is acceptable when managed with small size and high probability strikes

---

## 15. Hard Numbers Published by TastyTrade

| # | Rule | Number |
|---|------|--------|
| 1 | DTE entry range | 25–50 DTE |
| 2 | DTE entry sweet spot | **45 DTE** |
| 3 | DTE exit (time-based) | **21 DTE** |
| 4 | Profit target — strangles / IC / OTM credit | **50% of max credit** |
| 5 | Profit target — butterflies / BWB / diagonal | **25% of max profit** |
| 6 | Profit target — ratio spreads | **30% of max profit** |
| 7 | Profit target — calendar spreads | **10–25% of debit paid** |
| 8 | Roll trigger (POP floor) | **POP < 33%** |
| 9 | Credit target for IC spread width | **1/3 the spread width** (e.g., $10 wide = $3.33 credit) |
| 10 | POP implied by 1/3-width credit | **~67%** |
| 11 | Reference POP for 1-SD short strike | **~84%** (16 delta) |
| 12 | All entries require | **POP > 50%** |
| 13 | Portfolio theta target | **0.1% of NLV per day** |
| 14 | Win rate — SPY strangles managed at 50% | **90%** |
| 15 | Win rate — SPY strangles managed at 25% | **95%** |
| 16 | Win rate — SPY strangles held to expiration | **82%** |
| 17 | Win rate — IBM puts (all environments) | **80%** |
| 18 | Win rate — IBM puts after 5%+ weekly decline | **91%** |
| 19 | Win rate — put spreads (various DTE) | **≥88%** across all DTE |
| 20 | P&L stop loss performance | **40% underperformance vs buy-and-hold SPY** |
| 21 | Jade Lizard — no upside risk condition | Credit ≥ call spread width |

---

## 16. What TastyTrade Does NOT Publish as Hard Numbers

- Specific IVR entry threshold (e.g., "IVR > 30") — the IVR 50 in their study was a research split point, not a trading floor
- Delta of short strike for credit spreads (implied 16 delta from SD page, but not stated as a rule)
- Loss stop as a multiple of credit (the 2x rule is from historical research, not on current site pages)
- Buying power per trade as % of account
- Maximum number of concurrent positions
- Beta-weighted portfolio delta target
- Minimum account size for undefined risk
- Specific IVR floor for debit trades

---

## 17. Sources Index

All URLs are from tastylive.com (rebranded from tastytrade.com for the media/education side):

| URL Path | Topic |
|----------|-------|
| /definitions/days-to-expiration-dte | DTE rules, 45 DTE sweet spot |
| /definitions/staying-small | Position sizing philosophy |
| /definitions/expected-returns | Return modeling scenarios |
| /definitions/calculating-expected-move | Expected move formula |
| /definitions/delta-neutral | Portfolio delta |
| /definitions/undefined-risk | Naked options definition |
| /definitions/standard-deviation | 16 delta = 1 SD |
| /definitions/rolling-options | Roll triggers |
| /concepts-strategies/iron-condor | IC construction |
| /concepts-strategies/jade-lizard | Jade Lizard |
| /news-insights/why-manage-at-50-not-25-for-short-strangles | 50% vs 25% management study |
| /news-insights/our-3-favorite-strangle-studies | Mega Strangle Study, 3 studies |
| /news-insights/formula-for-realistic-expectations-market-measures | Realistic expectations study |
| /news-insights/blue-chip-put-sales | Blue chip put study |
| /news-insights/selling-puts-in-beat-up-stocks | 5%+ decline filter study |
| /news-insights/alternative-to-managing-losers | Stop-loss vs 21 DTE exit study |
| /news-insights/backtesting-duration-in-credit-spreads | DTE backtest |
| /news-insights/managing-strangles | Strangle management rules |
| /news-insights/managing-portfolio-theta | Portfolio theta target |
| /news-insights/expected-move-sanity-checking-trade-ideas | EM for strike selection |
| /news-insights/options-earnings | Earnings EM formula |
| /news-insights/the-jade-lizard-market-measures | Jade Lizard study |
| /news-insights/naked-risk-is-slightly-clothed | Undefined risk framing |
| /news-insights/selecting-strategies-based-on-risk-vs-reward | Risk bucket concept |
| /news-insights/how-to-use-options-strategies-amp-key-mechanics-takeaways | Profit targets by strategy |
| /news-insights/tasty-review-portfolio-management | Portfolio management |
| /news-insights/managing-portfolio-theta | 0.1% theta target |
