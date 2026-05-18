# AI Trading Bot Problem Statement

Pickfolio currently has rule-based bot participants that make simple trades using top gainers and losers. These bots create activity, but they do not make intelligent, context-aware trading decisions.

The goal is to design and build a scalable AI trading bot system that can participate in contests like a thoughtful market participant: adapting to contest duration, market regime, technical signals, news and events, fundamentals, valuation, risk, and portfolio state.

The system must support high scale, potentially millions of users and many concurrent contests. It should avoid holding large datasets in memory, favor durable and queryable storage, compute reusable signals asynchronously, cache only hot short-lived data, and keep LLM usage controlled and cost-efficient.

## Core Objective

Build an AI bot framework that answers:

```text
Is this a good trade for this contest, at this time, with this holding period, given available evidence, portfolio state, and risk?
```

The bot should not blindly buy gainers or losers. It should evaluate trade quality using structured signals and then decide whether to buy, sell, hold, or wait.

## Key Capabilities

- Short-term technical analysis for intraday contests.
- News and event awareness for sudden catalysts.
- Fundamental and valuation analysis for longer contests.
- Horizon-aware strategy selection.
- Risk-managed position sizing.
- Portfolio-aware decision making.
- Persistent memory of prior bot decisions and outcomes.
- Strict validation before any AI-suggested trade is executed.
- Scalable data ingestion, storage, and signal computation.

## Decision Philosophy

The bot must adapt its strategy to the contest horizon.

For very short contests, such as 15 minutes to 1 hour, the bot should primarily use:

- Momentum
- Relative volume
- VWAP behavior
- Opening range breakout
- Gap movement
- Market/index alignment
- Fresh news or event catalysts

For intraday or full-day contests, the bot should combine:

- Technical momentum
- Volume confirmation
- Market breadth
- News and corporate events
- Basic business quality filters

For multi-day or longer contests, the bot should increase weight on:

- Earnings quality
- Revenue and EPS growth
- Margins
- ROE/ROCE
- Debt
- Cash flow
- Valuation
- PEG
- GARP-style analysis
- Longer-term trend confirmation

## Proposed Architecture

```text
Data Ingestion Layer
  -> Raw Data Store
  -> Feature/Signal Computation Layer
  -> Signal Store
  -> Candidate Generator
  -> Strategy/Horizon Classifier
  -> Trade Quality Scorer
  -> AI Decision Layer
  -> Risk Gate
  -> Execution Layer
  -> Decision/Outcome Store
```

## Data Sources

The system should eventually ingest:

- Live and historical price data
- Intraday candles
- Volume data
- Index and sector data
- Top gainers and losers
- Corporate announcements
- Earnings results
- News articles
- Balance sheet data
- Income statement data
- Cash flow data
- Ratios such as P/E, P/B, EV/EBITDA, ROE, ROCE, debt/equity, PEG
- Promoter/shareholding data where available
- Contest state and portfolio state
- Bot trade history and outcomes

## Scalability Requirements

The system must be designed for scale from the beginning.

- Do not keep large market datasets permanently in memory.
- Store raw data and computed signals in durable storage.
- Use caching only for hot data with TTLs.
- Compute common signals once and reuse them across contests.
- Avoid calling external APIs per bot per contest.
- Avoid calling LLMs for every possible stock.
- Use candidate shortlisting before expensive AI evaluation.
- Batch data ingestion and signal computation where possible.
- Make bot decisions asynchronously where possible.
- Persist every bot decision, input snapshot, score, and outcome.
- Support horizontal scaling of bot workers.
- Avoid duplicated decisions when multiple workers run concurrently.

## Storage Expectations

Use durable stores for different data types:

- Relational DB for contests, bot decisions, trades, portfolios, and audit logs.
- Time-series optimized storage for prices, candles, volume, and technical signals.
- Document/object storage for news payloads, filings, LLM prompts, and LLM responses.
- Cache layer for latest quotes, trending movers, active contest symbols, and hot signal snapshots.

The system should be able to rebuild signals from stored raw data when formulas change.

## Signal Computation

Signals should be computed outside the request path.

Technical signals may include:

- Price change percentage
- Gap percentage
- Relative volume
- VWAP distance
- RSI
- MACD
- EMA/SMA alignment
- Bollinger band position
- ATR
- Opening range breakout
- Momentum acceleration
- Liquidity score
- Volatility score
- Index/sector confirmation

Fundamental signals may include:

- Revenue growth
- EPS growth
- Margin trend
- ROE
- ROCE
- Debt/equity
- Interest coverage
- Free cash flow conversion
- Promoter pledge
- Valuation versus sector
- PEG ratio
- Earnings quality score

News and event signals may include:

- Event type
- Source quality
- Freshness
- Surprise factor
- Sentiment
- Severity
- Relevance to the company
- Confidence score

## Trade Quality Scoring

Before the LLM is involved, the system should compute a deterministic trade quality score.

Example:

```text
trade_quality =
  momentum_weight * momentum_score
+ volume_weight * volume_confirmation
+ news_weight * news_surprise_score
+ valuation_weight * valuation_score
+ fundamental_weight * fundamental_quality
+ risk_reward_weight * risk_reward_score
- penalties
```

Weights must depend on contest duration and market regime.

Penalties may include:

- Stale data
- Low liquidity
- Excess volatility
- Overextended price
- Weak balance sheet
- Negative news
- Poor risk/reward
- Existing overconcentration
- Repeated trade too soon
- Unclear catalyst

## AI Decision Layer

The LLM should not be trusted as the sole decision maker. It should operate as a judgment layer over structured evidence.

The LLM receives:

- Contest metadata
- Portfolio state
- Candidate stocks
- Technical scores
- Fundamental scores
- News/event summaries
- Current risk limits
- Prior bot decisions
- Candidate trade quality scores

The LLM must return strict structured JSON:

```json
{
  "decision": "BUY",
  "symbol": "ABC.NS",
  "quantity_pct_cash": 15,
  "confidence": 0.67,
  "strategy": "momentum_event",
  "time_horizon": "intraday",
  "reason": "Breakout with volume confirmation and fresh positive catalyst.",
  "risks": ["extended intraday move", "news may already be priced in"],
  "invalid_if": ["falls below VWAP", "volume fades"]
}
```

Invalid, unsupported, or unsafe responses must be rejected.

## Risk Gate

No AI decision should execute directly.

The risk gate must enforce:

- Symbol must be in the allowed candidate universe.
- Contest must be live.
- Market data must be fresh enough.
- Quantity must be affordable.
- Max position size per stock.
- Max portfolio concentration.
- Max trade frequency per contest.
- Max number of trades per bot per contest.
- No duplicate trade within cooldown.
- Optional stop-loss or exit condition tracking.
- No trading on invalid or stale news.
- Fallback to HOLD if confidence is too low.

## Bot Personas

Bots should represent different realistic strategy styles, not hardcoded gimmicks.

Possible personas:

- Momentum trader
- GARP investor
- Quality compounder
- Event-driven trader
- Mean reversion trader
- Risk-managed balanced investor

The best AI bot may blend these styles depending on contest horizon and available evidence.

## Performance Goal

For millions of users, the system should not scale by increasing AI calls linearly with users or contests.

Preferred approach:

- Compute market-wide signals once.
- Generate a small candidate set per contest type.
- Reuse candidate rankings across contests with similar duration.
- Run LLM only on shortlisted candidates.
- Cache AI analysis for a symbol/timeframe briefly.
- Separate analysis generation from trade execution.
- Use deterministic fallback when LLM is unavailable.

## Initial MVP Scope

The first useful version should include:

- One new bot type: `AI_PM` or `AI_ANALYST`.
- Candidate generation from gainers, losers, active symbols, and top volume movers.
- Technical signals: momentum, relative volume, VWAP or moving average proxy, volatility.
- News/event hook, even if initially limited to headlines.
- Basic trade quality scoring.
- One LLM call per bot per contest every 15 minutes at most.
- Strict JSON output.
- Risk gate.
- Persistent decision log.
- No in-memory long-term datasets.

## Success Criteria

The system is successful when:

- Bots do not spend all cash in the first few cycles.
- Bots adapt to short versus long contests.
- Every trade has a recorded rationale and input snapshot.
- Trades are explainable to users.
- The system avoids repeated, low-quality trades.
- LLM failures do not break trading.
- Data ingestion and signal computation scale independently from bot execution.
- Expensive API calls are minimized through caching, batching, and precomputed signals.

## Non-Goals

The system does not need to guarantee profitable trades. It should simulate intelligent, risk-aware market behavior for competitive contests.

It should not:

- Let the LLM invent symbols.
- Let the LLM bypass risk rules.
- Fetch external APIs separately for every bot.
- Keep large datasets only in memory.
- Trade solely based on headlines.
- Trade solely based on valuation without considering contest duration.
- Treat any one strategy as always correct.

The final product should feel like a disciplined trading desk: data-driven, context-aware, risk-controlled, explainable, and scalable.
