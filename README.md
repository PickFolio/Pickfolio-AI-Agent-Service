# AI Agent Service

This service runs Pickfolio bot participants. Bots authenticate through the normal Auth Service, fetch live contests and portfolios from the Contest Service, read market context from the Market Data Service, and submit trades through the same transaction API used by human players.

The target architecture is deterministic:

```text
data -> signals -> weighted score -> rule-based decision -> risk gate -> execution
```

LLMs do not decide `BUY`, `SELL`, or `HOLD`. If an LLM is used later, it should only generate commentary after the deterministic engine has already selected an action.

## Runtime Flow

1. The scheduler runs every 5 minutes.
2. Market context is fetched once per cycle from Market Data Service:
   - `/api/market-data/trending`
   - `/api/market-data/catalysts/top`
   - `/api/market-data/pulse`
3. Each bot logs in, fetches live contests, and fetches its portfolio for each contest.
4. Simple persona bots run their fixed strategies.
5. `analyst_bot` runs the deterministic decision engine.
6. Approved trades are submitted to Contest Service through `/api/contests/{contestId}/transactions`.
7. Every analyst decision is persisted in `ai_bot_decisions`.

## Bots

| Bot | Type | Behavior |
| --- | --- | --- |
| `warren_bot` | `VALUE` | Buys a random daily loser with 20% of cash when cash is above 10000. |
| `quant_bot` | `MOMENTUM` | Buys one of the top three gainers with 40% of cash when cash is above 5000. |
| `chad_bot` | `WSB_YOLO` | Sometimes sells all holdings; otherwise buys the top gainer with 95% of cash. |
| `analyst_bot` | `AI_ANALYST` | Uses deterministic scoring, thresholds, and risk gates. |

## Analyst Bot

`analyst_bot` does not call OpenRouter and does not ask an LLM for trade decisions.

### Candidate Set

Candidates are built from:

- top gainers
- top losers
- top positive catalysts
- top negative catalysts
- current holdings

Catalyst-only candidates may not include a price in the top-catalysts payload. The bot still keeps them in the candidate set because news impact can be scored without price. Before scoring, it calls `/api/market-data/catalysts` for candidate symbols and merges returned catalyst fields:

- `catalyst`
- `sentiment_score`
- `sentiment_method`
- `event_type`

To reduce unnecessary market-data calls, the bot does not quote-fill every catalyst candidate. If the final deterministic decision is a BUY and the selected candidate has no price, the bot fetches `/api/market-data/quote/{symbol}` once for that selected symbol only. If that quote is unavailable, the decision is persisted as rejected. SELL decisions use portfolio holding quantity and do not require a fresh quote.

`sentiment_score` is the market impact score produced by Market Data Service. It is interpreted as:

```text
-1.0 = strongly bearish impact
 0.0 = neutral / low impact
+1.0 = strongly bullish impact
```

### Signals

The engine currently uses only signals already available to the service:

| Signal | Source | Meaning |
| --- | --- | --- |
| `momentum_score` | `pChange` | Higher when the stock is moving up. |
| `downside_momentum_score` | `pChange` | Higher when the stock is moving down. |
| `mean_reversion_score` | `pChange` | Higher for downside moves that may revert. |
| `raw_news_impact` | `sentiment_score` | Intrinsic headline impact from Market Data Service. |
| `effective_news_impact` | `sentiment_score * news_freshness_weight` | News impact after market-time freshness decay. |
| `news_impact_score` | `effective_news_impact` | Converts effective news impact into a `0..100` buy-support score. |
| `news_sell_score` | `effective_news_impact` | Converts effective news impact into a `0..100` sell-risk score. |
| `trading_news_age_minutes` | `published_at` or `fetch_time` | Counts only minutes during regular market hours. |
| `news_freshness_weight` | `trading_news_age_minutes` | Decay multiplier applied to news impact. |
| `portfolio_fit` | current holding value | Penalizes adding to already concentrated positions. |
| `concentration_risk` | current holding value | Raises sell pressure for oversized held positions. |
| `cash_fit` | cash balance | Supports buy decisions when enough cash exists. |
| `volatility_risk_score` | absolute `pChange` | Treats large moves as higher risk. |

If a future signal is not available yet, it is skipped rather than guessed.

### Market-Time News Freshness

News is not decayed by wall-clock age. Closed-market time does not count because the market has not had a chance to price in the headline.

The engine computes:

```text
effective_news_impact =
  sentiment_score * market_freshness_weight(trading_minutes_since_published)
```

`trading_minutes_since_published` counts only regular NSE-style session minutes:

```text
09:15-15:30 IST
Monday-Friday
```

Examples:

| Published | Evaluated | Trading age |
| --- | --- | --- |
| 19:00 IST today | 09:15 IST next trading day | 0 minutes |
| Friday 20:00 IST | Monday 09:15 IST | 0 minutes |
| 11:00 IST same session | 13:00 IST same session | 120 minutes |

Current freshness curve:

| Trading age | Weight |
| --- | --- |
| `0-60` minutes | `1.00` |
| `60-180` minutes | `0.80` |
| `180-390` minutes | `0.55` |
| `390-780` minutes | `0.30` |
| `>780` minutes | `0.10` |

This decay is applied only in AI Agent scoring. `market_news_archive.sentiment_score` remains the intrinsic headline score.

### Buy Score

For every candidate, the engine computes:

```text
buy_score =
  momentum_score * (0.35 + horizon_weight * 0.10)
+ news_impact_score * 0.30
+ cash_fit * 0.10
+ portfolio_fit * 0.15
+ source_bonus
- overextension_penalty
```

`horizon_weight` is higher for contests closer to expiry. `source_bonus` favors candidates coming from positive catalysts and penalizes candidates coming from negative catalysts. `overextension_penalty` reduces the score after very large positive intraday moves.

### Sell Score

Sell score is only active for stocks already held:

```text
sell_score =
  downside_momentum_score * 0.30
+ news_sell_score * 0.40
+ volatility_risk_score * 0.15
+ concentration_risk * 0.15
```

Strong negative news therefore supports selling a held stock, while strong positive news suppresses selling unless the sell score is extremely high.

### Decision Selection

The engine chooses exactly one action:

1. Check held stocks first. Sell the held candidate with the highest `sell_score` if it is above `AI_ANALYST_SELL_THRESHOLD`.
2. If no sell qualifies, buy the non-held candidate with the highest `buy_score` if it is above `AI_ANALYST_BUY_THRESHOLD`.
3. Otherwise hold.

Default thresholds:

```env
AI_ANALYST_BUY_THRESHOLD=68
AI_ANALYST_SELL_THRESHOLD=65
AI_ANALYST_STRONG_NEGATIVE_NEWS=-0.55
AI_ANALYST_STRONG_POSITIVE_NEWS=0.55
```

Position sizing is deterministic:

- BUY uses 25% of cash for very high scores, 18% for strong scores, otherwise 12%.
- SELL exits 100%, 60%, or 35% of the holding depending on score and news severity.

### Risk Gate

The risk gate runs after the deterministic decision and before execution.

It rejects:

- `HOLD`
- confidence below `AI_BOT_MIN_CONFIDENCE`
- missing symbol
- symbols outside the scored candidate universe
- candidates without a usable price
- BUY when quantity is zero
- BUY that would exceed 40% portfolio concentration
- BUY when news impact is strongly negative
- BUY below the buy threshold
- SELL for a stock that is not held
- SELL when news impact is strongly positive unless sell score is very high
- SELL below the sell threshold

### Persistence

Analyst decisions are written to `ai_bot_decisions`.

Important fields:

| Column | Content |
| --- | --- |
| `decision` | `BUY`, `SELL`, or `HOLD`. |
| `symbol` | Selected stock, if any. |
| `quantity` | Executed or intended quantity. |
| `confidence` | Deterministic score divided by 100. |
| `strategy` | `deterministic_signal_buy`, `deterministic_signal_sell`, or `hold_cash`. |
| `reason` | Human-readable deterministic reason string. |
| `risks` | Structured deterministic risk notes. |
| `candidate_scores` | Full candidate map with signal scores. |
| `prompt_snapshot` | Legacy column reused for deterministic decision context. |
| `raw_llm_response` | Legacy column; no LLM response is written by the deterministic engine. |
| `risk_status` | `APPROVED` or `REJECTED`. |
| `execution_status` | `EXECUTED`, `FAILED`, `DRY_RUN`, or `SKIPPED`. |

## Local Configuration

Example `.env`:

```env
AUTH_SERVICE_URL=http://localhost:8080
CONTEST_SERVICE_URL=http://localhost:8081
MARKET_DATA_SERVICE_URL=http://localhost:8082
AI_DATABASE_URL=postgresql://pickfolio_user:pickfolio_pass@localhost:5432/pickfolio_ai
AI_BOT_EXECUTE_TRADES=true
AI_BOT_DECISION_INTERVAL_MINUTES=15
AI_BOT_MIN_CONFIDENCE=0.60
AI_ANALYST_BUY_THRESHOLD=68
AI_ANALYST_SELL_THRESHOLD=65
AI_ANALYST_STRONG_NEGATIVE_NEWS=-0.55
AI_ANALYST_STRONG_POSITIVE_NEWS=0.55
```

Set `AI_BOT_EXECUTE_TRADES=false` to run the analyst engine in dry-run mode while still persisting decisions.
