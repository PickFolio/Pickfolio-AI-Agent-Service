import logging
import os
import random
import time
import uuid
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from urllib.parse import parse_qsl, urlparse

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("AIAgent")

AUTH_URL = os.getenv("AUTH_SERVICE_URL", "http://auth-service:8080")
CONTEST_URL = os.getenv("CONTEST_SERVICE_URL", "http://contest-service:8081")
MARKET_DATA_URL = os.getenv("MARKET_DATA_SERVICE_URL", "http://market-data-service:8082")

AI_DATABASE_URL = os.getenv("AI_DATABASE_URL", "")
AI_BOT_EXECUTE_TRADES = os.getenv("AI_BOT_EXECUTE_TRADES", "true").lower() == "true"
AI_BOT_DECISION_INTERVAL_MINUTES = int(os.getenv("AI_BOT_DECISION_INTERVAL_MINUTES", "15"))
AI_BOT_MIN_CONFIDENCE = float(os.getenv("AI_BOT_MIN_CONFIDENCE", "0.60"))
AI_ANALYST_BUY_THRESHOLD = float(os.getenv("AI_ANALYST_BUY_THRESHOLD", "68"))
AI_ANALYST_SELL_THRESHOLD = float(os.getenv("AI_ANALYST_SELL_THRESHOLD", "65"))
AI_ANALYST_STRONG_NEGATIVE_NEWS = float(os.getenv("AI_ANALYST_STRONG_NEGATIVE_NEWS", "-0.55"))
AI_ANALYST_STRONG_POSITIVE_NEWS = float(os.getenv("AI_ANALYST_STRONG_POSITIVE_NEWS", "0.55"))
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN_TIME = datetime_time(hour=9, minute=15)
MARKET_CLOSE_TIME = datetime_time(hour=15, minute=30)

BOTS = [
    {"username": "warren_bot", "type": "VALUE"},
    {"username": "chad_bot", "type": "WSB_YOLO"},
    {"username": "quant_bot", "type": "MOMENTUM"},
    {"username": "analyst_bot", "type": "AI_ANALYST"},
]
PASSWORD_SUFFIX = "_secret_123!"


class TradeDecision(BaseModel):
    decision: str = Field(pattern="^(BUY|SELL|HOLD)$")
    symbol: Optional[str] = None
    quantity_pct_cash: float = Field(default=0, ge=0, le=100)
    confidence: float = Field(default=0, ge=0, le=1)
    strategy: str = "hold_cash"
    reason: str = ""
    risks: List[str] = Field(default_factory=list)
    invalid_if: List[str] = Field(default_factory=list)


class DecisionStore:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.available = False

    def initialize(self) -> None:
        if not self.database_url:
            logger.warning("AI_DATABASE_URL is not configured; analyst_bot will not trade.")
            return

        ddl = """
        CREATE TABLE IF NOT EXISTS ai_bot_decisions (
            id UUID PRIMARY KEY,
            bot_username TEXT NOT NULL,
            contest_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            symbol TEXT,
            quantity INTEGER NOT NULL DEFAULT 0,
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
            strategy TEXT,
            reason TEXT,
            risks JSONB NOT NULL DEFAULT '[]'::jsonb,
            invalid_if JSONB NOT NULL DEFAULT '[]'::jsonb,
            candidate_scores JSONB NOT NULL DEFAULT '{}'::jsonb,
            prompt_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            raw_llm_response JSONB NOT NULL DEFAULT '{}'::jsonb,
            risk_status TEXT NOT NULL,
            execution_status TEXT NOT NULL,
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            executed_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_ai_bot_decisions_lookup
            ON ai_bot_decisions (bot_username, contest_id, created_at DESC);
        """

        self._ensure_database_exists()
        for attempt in range(1, 6):
            try:
                with psycopg.connect(self.database_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute(ddl)
                    conn.commit()
                self.available = True
                logger.info("AI decision store initialized.")
                return
            except Exception as exc:
                logger.warning("AI decision store initialization failed on attempt %s: %s", attempt, exc)
                time.sleep(min(attempt * 2, 10))

    def _ensure_database_exists(self) -> None:
        parsed = urlparse(self.database_url)
        database_name = parsed.path.lstrip("/")
        if not parsed.scheme.startswith("postgres") or not database_name:
            return

        admin_params = {
            "dbname": "postgres",
            "user": parsed.username,
            "password": parsed.password,
            "host": parsed.hostname,
            "port": parsed.port or 5432,
            **dict(parse_qsl(parsed.query)),
        }
        try:
            with psycopg.connect(**admin_params, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,))
                    if cur.fetchone():
                        return
                    cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
                    logger.info("Created AI database %s.", database_name)
        except Exception as exc:
            logger.warning("Could not ensure AI database exists: %s", exc)

    def recently_decided(self, bot_username: str, contest_id: str, cooldown_minutes: int) -> bool:
        if not self.available:
            return True
        query = """
            SELECT 1
            FROM ai_bot_decisions
            WHERE bot_username = %s
              AND contest_id = %s
              AND created_at > NOW() - (%s::text || ' minutes')::interval
            LIMIT 1
        """
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (bot_username, contest_id, cooldown_minutes))
                    return cur.fetchone() is not None
        except Exception as exc:
            logger.error("Failed to check AI decision cooldown: %s", exc)
            return True

    def insert_decision(self, row: Dict[str, Any]) -> None:
        if not self.available:
            return
        query = """
            INSERT INTO ai_bot_decisions (
                id, bot_username, contest_id, decision, symbol, quantity, confidence,
                strategy, reason, risks, invalid_if, candidate_scores, prompt_snapshot,
                raw_llm_response, risk_status, execution_status, error_message, executed_at
            ) VALUES (
                %(id)s, %(bot_username)s, %(contest_id)s, %(decision)s, %(symbol)s,
                %(quantity)s, %(confidence)s, %(strategy)s, %(reason)s, %(risks)s,
                %(invalid_if)s, %(candidate_scores)s, %(prompt_snapshot)s,
                %(raw_llm_response)s, %(risk_status)s, %(execution_status)s,
                %(error_message)s, %(executed_at)s
            )
        """
        payload = row.copy()
        for key in ("risks", "invalid_if", "candidate_scores", "prompt_snapshot", "raw_llm_response"):
            payload[key] = Jsonb(payload.get(key, [] if key in ("risks", "invalid_if") else {}))
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, payload)
                conn.commit()
        except Exception as exc:
            logger.error("Failed to persist AI decision: %s", exc)


class BotClient:
    def __init__(self, username: str, persona_type: str, decision_store: DecisionStore):
        self.username = username
        self.password = f"{username}{PASSWORD_SUFFIX}"
        self.persona_type = persona_type
        self.token = None
        self.session = requests.Session()
        self.decision_store = decision_store

    def login(self) -> None:
        try:
            # Clear any existing Authorization header before logging in
            if "Authorization" in self.session.headers:
                del self.session.headers["Authorization"]
                
            res = self.session.post(
                f"{AUTH_URL}/api/auth/login",
                json={
                    "username": self.username,
                    "password": self.password,
                    "deviceInfo": "AI_Agent_Service",
                },
                timeout=10,
            )
            if res.status_code == 200:
                self.token = res.json().get("accessToken")
                self.session.headers.update({"Authorization": f"Bearer {self.token}"})
                logger.info("%s logged in successfully.", self.username)
            else:
                logger.error("%s failed to login: %s", self.username, res.text)
        except Exception as exc:
            logger.error("%s login error: %s", self.username, exc)

    def execute_strategy(self, market_context: Dict[str, Any]) -> None:
        if not self.token:
            self.login()
            if not self.token:
                return

        try:
            res = self.session.get(f"{CONTEST_URL}/api/contests/my-contests", timeout=10)
            if res.status_code == 401:
                self.token = None
                return
            if res.status_code != 200:
                logger.warning("%s failed to fetch contests: %s", self.username, res.text)
                return

            contests = res.json()
            live_contests = [c for c in contests if c.get("status") == "LIVE"]
            if not live_contests:
                logger.info("%s has no live contests currently.", self.username)
                return

            for contest in live_contests:
                self._play_contest(contest, market_context)
        except Exception as exc:
            logger.error("%s strategy execution error: %s", self.username, exc)

    def _play_contest(self, contest: Dict[str, Any], market_context: Dict[str, Any]) -> None:
        contest_id = contest["id"]
        res = self.session.get(f"{CONTEST_URL}/api/contests/{contest_id}/portfolio", timeout=10)
        if res.status_code != 200:
            logger.warning("%s failed to fetch portfolio for contest %s: %s", self.username, contest_id, res.text)
            return
        portfolio = res.json()

        cash = float(portfolio.get("cashBalance") or 0)
        holdings = portfolio.get("holdings") or []
        trending = market_context.get("trending", {"gainers": [], "losers": []})

        has_market_candidates = any(
            trending.get(group)
            for group in ("gainers", "losers", "positive_catalysts", "negative_catalysts")
        )
        if not has_market_candidates and not holdings:
            logger.info("%s skipped contest %s because trending data is empty.", self.username, contest_id)
            return

        if self.persona_type == "VALUE":
            self._value_strategy(contest_id, cash, trending)
        elif self.persona_type == "WSB_YOLO":
            self._yolo_strategy(contest_id, cash, holdings, trending)
        elif self.persona_type == "MOMENTUM":
            self._momentum_strategy(contest_id, cash, trending)
        elif self.persona_type == "AI_ANALYST":
            self._analyst_strategy(contest, portfolio, market_context)

    def _execute_trade(self, contest_id: str, txn_type: str, symbol: str, quantity: int) -> Tuple[bool, str]:
        if quantity <= 0:
            return False, "quantity must be positive"
        try:
            res = self.session.post(
                f"{CONTEST_URL}/api/contests/{contest_id}/transactions",
                json={
                    "transactionType": txn_type,
                    "stockSymbol": symbol,
                    "quantity": quantity,
                },
                timeout=10,
            )
            if res.status_code == 200:
                logger.info("%s executed %s %s shares of %s in contest %s", self.username, txn_type, quantity, symbol, contest_id)
                return True, ""
            logger.warning("%s trade failed: %s", self.username, res.text)
            return False, res.text
        except Exception as exc:
            logger.error("%s trade error: %s", self.username, exc)
            return False, str(exc)

    def _value_strategy(self, contest_id: str, cash: float, trending: Dict[str, Any]) -> None:
        if cash > 10000 and trending.get("losers"):
            target = random.choice(trending["losers"])
            price = float(target.get("price") or 100)
            if price > 0:
                qty = int((cash * 0.2) / price)
                self._execute_trade(contest_id, "BUY", target["symbol"], qty)

    def _yolo_strategy(self, contest_id: str, cash: float, holdings: List[Dict[str, Any]], trending: Dict[str, Any]) -> None:
        if holdings and random.random() < 0.15:
            logger.info("%s is panic selling!", self.username)
            for holding in holdings:
                self._execute_trade(contest_id, "SELL", holding["stockSymbol"], int(holding["quantity"]))
            return

        if cash > 1000 and trending.get("gainers"):
            target = trending["gainers"][0]
            price = float(target.get("price") or 100)
            if price > 0:
                qty = int((cash * 0.95) / price)
                self._execute_trade(contest_id, "BUY", target["symbol"], qty)

    def _momentum_strategy(self, contest_id: str, cash: float, trending: Dict[str, Any]) -> None:
        if cash > 5000 and trending.get("gainers"):
            target = random.choice(trending["gainers"][:3])
            price = float(target.get("price") or 100)
            if price > 0:
                qty = int((cash * 0.4) / price)
                self._execute_trade(contest_id, "BUY", target["symbol"], qty)

    def _analyst_strategy(self, contest: Dict[str, Any], portfolio: Dict[str, Any], market_context: Dict[str, Any]) -> None:
        contest_id = contest["id"]
        if self.decision_store.recently_decided(self.username, contest_id, AI_BOT_DECISION_INTERVAL_MINUTES):
            logger.info("%s skipped contest %s due to decision cooldown.", self.username, contest_id)
            return

        candidates = build_candidates(portfolio, market_context.get("trending", {}))
        if not candidates:
            self._persist_ai_decision(contest_id, hold_decision("No candidates available"), {}, {}, "REJECTED", "SKIPPED", "No candidates available")
            return

        logger.info(
            "%s analyst universe for contest %s: candidates=%s holdings=%s cash=%.2f total_value=%.2f sources=%s",
            self.username,
            contest_id,
            len(candidates),
            len(portfolio.get("holdings") or []),
            float(portfolio.get("cashBalance") or 0),
            float(portfolio.get("totalPortfolioValue") or 0),
            candidate_source_counts(candidates),
        )
        self._enrich_candidates_with_catalysts(candidates)
        logger.info(
            "%s analyst enrichment for contest %s: catalysts=%s scored_news=%s priced=%s",
            self.username,
            contest_id,
            sum(1 for candidate in candidates.values() if candidate.get("catalyst")),
            sum(1 for candidate in candidates.values() if float(candidate.get("sentiment_score") or 0) != 0),
            sum(1 for candidate in candidates.values() if float(candidate.get("price") or 0) > 0),
        )
        scored_candidates = score_candidates(contest, portfolio, candidates)
        logger.info(
            "%s analyst top candidates for contest %s: %s",
            self.username,
            contest_id,
            format_candidate_summaries(scored_candidates),
        )
        decision_context = build_decision_context(contest, portfolio, market_context, scored_candidates)
        decision = choose_deterministic_decision(scored_candidates, portfolio)
        price_error = self._fill_selected_buy_price(decision, scored_candidates)
        if price_error:
            self._persist_ai_decision(contest_id, decision, scored_candidates, decision_context, "REJECTED", "SKIPPED", price_error)
            logger.info(
                "%s AI decision rejected before risk gate for contest %s: %s decision=%s selected=%s",
                self.username,
                contest_id,
                price_error,
                decision.decision,
                selected_candidate_summary(scored_candidates, decision.symbol),
            )
            return
        logger.info(
            "%s deterministic decision for contest %s: %s %s confidence=%.2f reason=%s",
            self.username,
            contest_id,
            decision.decision,
            decision.symbol,
            decision.confidence,
            decision.reason,
        )

        risk_ok, txn_type, symbol, quantity, risk_error = apply_risk_gate(decision, portfolio, scored_candidates)
        if not risk_ok:
            self._persist_ai_decision(contest_id, decision, scored_candidates, decision_context, "REJECTED", "SKIPPED", risk_error)
            logger.info(
                "%s AI decision rejected by risk gate for contest %s: %s selected=%s",
                self.username,
                contest_id,
                risk_error,
                selected_candidate_summary(scored_candidates, decision.symbol),
            )
            return

        execution_status = "SKIPPED"
        executed_at = None
        error_message = ""
        if AI_BOT_EXECUTE_TRADES:
            executed, error_message = self._execute_trade(contest_id, txn_type, symbol, quantity)
            execution_status = "EXECUTED" if executed else "FAILED"
            executed_at = datetime.now(timezone.utc) if executed else None
        else:
            execution_status = "DRY_RUN"
        logger.info(
            "%s analyst execution outcome for contest %s: decision=%s symbol=%s quantity=%s status=%s error=%s",
            self.username,
            contest_id,
            txn_type,
            symbol,
            quantity,
            execution_status,
            error_message or "",
        )

        self._persist_ai_decision(
            contest_id,
            decision,
            scored_candidates,
            decision_context,
            "APPROVED",
            execution_status,
            error_message,
            quantity=quantity,
            executed_at=executed_at,
        )

    def _enrich_candidates_with_catalysts(self, candidates: Dict[str, Dict[str, Any]]) -> None:
        symbols_to_query = list(candidates.keys())
        if not symbols_to_query:
            return
        try:
            cat_res = self.session.post(
                f"{MARKET_DATA_URL}/api/market-data/catalysts",
                json={"symbols": symbols_to_query},
                timeout=15,
            )
            if cat_res.status_code != 200:
                logger.warning("%s failed to fetch catalysts: %s", self.username, cat_res.text)
                return
            for cat in cat_res.json():
                sym = cat.get("symbol")
                if sym not in candidates:
                    continue
                headline = cat.get("headline")
                if headline:
                    candidates[sym]["catalyst"] = headline
                candidates[sym]["sentiment_score"] = float(cat.get("sentiment_score") or 0)
                candidates[sym]["sentiment_method"] = cat.get("sentiment_method")
                candidates[sym]["event_type"] = cat.get("event_type")
                candidates[sym]["published_at"] = cat.get("published_at")
                candidates[sym]["fetch_time"] = cat.get("fetch_time")
        except Exception as exc:
            logger.warning("%s failed to fetch catalysts: %s", self.username, exc)

    def _fill_selected_buy_price(self, decision: TradeDecision, scored_candidates: Dict[str, Any]) -> str:
        if decision.decision != "BUY" or not decision.symbol:
            return ""
        candidate = scored_candidates.get(decision.symbol)
        if not candidate or float(candidate.get("price") or 0) > 0:
            return ""
        try:
            quote_res = self.session.get(f"{MARKET_DATA_URL}/api/market-data/quote/{decision.symbol}", timeout=8)
            if quote_res.status_code != 200:
                return "Selected BUY candidate price unavailable"
            quote = quote_res.json()
            price = float(quote.get("price") or 0)
            if price <= 0:
                return "Selected BUY candidate price unavailable"
            candidate["price"] = price
            candidate["pChange"] = float(quote.get("changePercent") or candidate.get("pChange") or 0)
            return ""
        except Exception as exc:
            logger.warning("%s failed to fill selected BUY price for %s: %s", self.username, decision.symbol, exc)
            return "Selected BUY candidate price unavailable"

    def _persist_ai_decision(
        self,
        contest_id: str,
        decision: TradeDecision,
        candidate_scores: Dict[str, Any],
        prompt_snapshot: Dict[str, Any],
        risk_status: str,
        execution_status: str,
        error_message: str = "",
        raw_llm_response: Optional[Dict[str, Any]] = None,
        quantity: int = 0,
        executed_at: Optional[datetime] = None,
    ) -> None:
        self.decision_store.insert_decision(
            {
                "id": uuid.uuid4(),
                "bot_username": self.username,
                "contest_id": contest_id,
                "decision": decision.decision,
                "symbol": decision.symbol,
                "quantity": quantity,
                "confidence": decision.confidence,
                "strategy": decision.strategy,
                "reason": decision.reason,
                "risks": decision.risks,
                "invalid_if": decision.invalid_if,
                "candidate_scores": candidate_scores,
                "prompt_snapshot": prompt_snapshot,
                "raw_llm_response": raw_llm_response or {},
                "risk_status": risk_status,
                "execution_status": execution_status,
                "error_message": error_message,
                "executed_at": executed_at,
            }
        )


def hold_decision(reason: str) -> TradeDecision:
    return TradeDecision(decision="HOLD", confidence=0, strategy="hold_cash", reason=reason)


def candidate_source_counts(candidates: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for candidate in candidates.values():
        source = str(candidate.get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return counts


def format_candidate_summaries(scored_candidates: Dict[str, Any], limit: int = 3) -> str:
    summaries = [
        selected_candidate_summary(scored_candidates, symbol)
        for symbol in list(scored_candidates.keys())[:limit]
    ]
    return " | ".join(summary for summary in summaries if summary) or "none"


def selected_candidate_summary(scored_candidates: Dict[str, Any], symbol: Optional[str]) -> str:
    if not symbol or symbol not in scored_candidates:
        return "none"
    candidate = scored_candidates[symbol]
    scores = candidate.get("scores") or {}
    headline = candidate.get("catalyst") or ""
    if len(headline) > 90:
        headline = headline[:87] + "..."
    return (
        f"{symbol}"
        f"[src={candidate.get('source')}"
        f" held={bool(candidate.get('holding'))}"
        f" buy={scores.get('buy_score')}"
        f" sell={scores.get('sell_score')}"
        f" final={scores.get('final_score')}"
        f" news={scores.get('effective_news_impact')}"
        f" fresh={scores.get('news_freshness_weight')}"
        f" event={candidate.get('event_type') or '-'}"
        f" catalyst={headline or '-'}]"
    )


def build_candidates(portfolio: Dict[str, Any], trending: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    candidates: Dict[str, Dict[str, Any]] = {}
    for group_name in ("gainers", "losers", "positive_catalysts", "negative_catalysts"):
        for rank, mover in enumerate(trending.get(group_name) or [], start=1):
            symbol = mover.get("symbol")
            price = float(mover.get("price") or 0)
            is_catalyst = group_name in ("positive_catalysts", "negative_catalysts")
            if symbol and (price > 0 or is_catalyst):
                candidates.setdefault(symbol, {
                    "symbol": symbol,
                    "price": price,
                    "change": float(mover.get("change") or 0),
                    "pChange": float(mover.get("pChange") or 0),
                    "source": group_name,
                    "rank": rank,
                    "holding": None,
                })
                if is_catalyst:
                    candidates[symbol]["catalyst"] = mover.get("headline")
                    candidates[symbol]["sentiment_score"] = float(mover.get("sentiment_score") or 0)
                    candidates[symbol]["sentiment_method"] = mover.get("sentiment_method")
                    candidates[symbol]["event_type"] = mover.get("event_type")
                    candidates[symbol]["published_at"] = mover.get("published_at")
                    candidates[symbol]["fetch_time"] = mover.get("fetch_time")

    for holding in portfolio.get("holdings") or []:
        symbol = holding.get("stockSymbol")
        price = float(holding.get("currentPrice") or 0)
        if symbol:
            candidates.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "price": price,
                    "change": 0,
                    "pChange": 0,
                    "source": "holding",
                    "rank": 99,
                },
            )
            candidates[symbol]["holding"] = holding
            if price > 0:
                candidates[symbol]["price"] = price
    return candidates


def score_candidates(contest: Dict[str, Any], portfolio: Dict[str, Any], candidates: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    cash = float(portfolio.get("cashBalance") or 0)
    total_value = float(portfolio.get("totalPortfolioValue") or cash or 1)
    minutes_remaining = contest_minutes_remaining(contest)
    horizon_weight = 1.0 if minutes_remaining <= 60 else 0.75 if minutes_remaining <= 390 else 0.45
    scored: Dict[str, Any] = {}

    for symbol, candidate in candidates.items():
        pct = float(candidate.get("pChange") or 0)
        source = candidate.get("source")
        holding = candidate.get("holding")
        current_value = float((holding or {}).get("currentValue") or 0)
        sentiment_score = float(candidate.get("sentiment_score") or 0.0)
        trading_news_age_minutes = trading_minutes_since_published(
            candidate.get("published_at") or candidate.get("fetch_time")
        )
        news_freshness_weight = market_freshness_weight(trading_news_age_minutes)
        effective_sentiment_score = sentiment_score * news_freshness_weight

        momentum_score = clamp(50 + pct * 5, 0, 100)
        downside_momentum_score = clamp(50 - pct * 5, 0, 100)
        mean_reversion_score = clamp(50 + abs(min(pct, 0)) * 3 - max(pct, 0) * 2, 0, 100)
        news_buy_score = clamp(50 + effective_sentiment_score * 50, 0, 100)
        news_sell_score = clamp(50 - effective_sentiment_score * 50, 0, 100)

        concentration = current_value / total_value if total_value > 0 else 0
        portfolio_fit = clamp(80 - concentration * 120, 0, 100)
        concentration_risk = clamp(concentration * 160, 0, 100)
        cash_fit = 70 if cash > 1000 else 20
        overextension_penalty = max(0, pct - 8) * 3
        volatility_risk_score = clamp(abs(pct) * 6, 0, 100)
        source_bonus = 8 if source == "positive_catalysts" else -8 if source == "negative_catalysts" else 0

        buy_score = clamp(
            momentum_score * (0.35 + horizon_weight * 0.10)
            + news_buy_score * 0.30
            + cash_fit * 0.10
            + portfolio_fit * 0.15
            + source_bonus
            - overextension_penalty,
            0,
            100,
        )
        sell_score = 0.0
        if holding:
            sell_score = clamp(
                downside_momentum_score * 0.30
                + news_sell_score * 0.40
                + volatility_risk_score * 0.15
                + concentration_risk * 0.15,
                0,
                100,
            )
        final_score = max(buy_score, sell_score)

        scored[symbol] = {
            **candidate,
            "recommended_action": "SELL" if sell_score >= buy_score and holding else "BUY",
            "scores": {
                "momentum_score": round(momentum_score, 2),
                "downside_momentum_score": round(downside_momentum_score, 2),
                "mean_reversion_score": round(mean_reversion_score, 2),
                "news_impact_score": round(news_buy_score, 2),
                "news_sell_score": round(news_sell_score, 2),
                "raw_news_impact": round(sentiment_score, 3),
                "effective_news_impact": round(effective_sentiment_score, 3),
                "news_freshness_weight": round(news_freshness_weight, 3),
                "trading_news_age_minutes": round(trading_news_age_minutes, 2),
                "portfolio_fit": round(portfolio_fit, 2),
                "concentration_risk": round(concentration_risk, 2),
                "cash_fit": cash_fit,
                "volatility_risk_score": round(volatility_risk_score, 2),
                "buy_score": round(buy_score, 2),
                "sell_score": round(sell_score, 2),
                "final_score": round(final_score, 2),
            },
        }
    return dict(sorted(scored.items(), key=lambda item: item[1]["scores"]["final_score"], reverse=True)[:8])


def choose_deterministic_decision(scored_candidates: Dict[str, Any], portfolio: Dict[str, Any]) -> TradeDecision:
    sell_candidates = [
        candidate
        for candidate in scored_candidates.values()
        if candidate.get("holding") and candidate["scores"]["sell_score"] >= AI_ANALYST_SELL_THRESHOLD
    ]
    if sell_candidates:
        target = max(sell_candidates, key=lambda item: item["scores"]["sell_score"])
        score = target["scores"]["sell_score"]
        return TradeDecision(
            decision="SELL",
            symbol=target["symbol"],
            quantity_pct_cash=_sell_quantity_pct(target),
            confidence=round(score / 100, 3),
            strategy="deterministic_signal_sell",
            reason=_decision_reason("SELL", target),
            risks=_decision_risks(target),
            invalid_if=["price recovers", "negative catalyst is resolved", "portfolio already rebalanced"],
        )

    cash = float(portfolio.get("cashBalance") or 0)
    buy_candidates = [
        candidate
        for candidate in scored_candidates.values()
        if not candidate.get("holding")
        and cash > 1000
        and candidate["scores"]["buy_score"] >= AI_ANALYST_BUY_THRESHOLD
        and float(candidate["scores"].get("effective_news_impact") or 0) > AI_ANALYST_STRONG_NEGATIVE_NEWS
    ]
    if buy_candidates:
        target = max(buy_candidates, key=lambda item: item["scores"]["buy_score"])
        score = target["scores"]["buy_score"]
        return TradeDecision(
            decision="BUY",
            symbol=target["symbol"],
            quantity_pct_cash=_buy_quantity_pct(target),
            confidence=round(score / 100, 3),
            strategy="deterministic_signal_buy",
            reason=_decision_reason("BUY", target),
            risks=_decision_risks(target),
            invalid_if=["momentum reverses", "positive catalyst is invalidated", "position concentration would rise too high"],
        )

    return hold_decision("No candidate met deterministic buy or sell thresholds")


def trading_minutes_since_published(published_at: Any, now: Optional[datetime] = None) -> float:
    published = parse_optional_datetime(published_at)
    if not published:
        return 0.0
    end = (now or datetime.now(timezone.utc)).astimezone(IST)
    start = published.astimezone(IST)
    if end <= start:
        return 0.0

    minutes = 0.0
    current_day = start.date()
    end_day = end.date()
    while current_day <= end_day:
        if current_day.weekday() < 5:
            session_start = datetime.combine(current_day, MARKET_OPEN_TIME, tzinfo=IST)
            session_end = datetime.combine(current_day, MARKET_CLOSE_TIME, tzinfo=IST)
            overlap_start = max(start, session_start)
            overlap_end = min(end, session_end)
            if overlap_end > overlap_start:
                minutes += (overlap_end - overlap_start).total_seconds() / 60
        current_day += timedelta(days=1)
    return minutes


def market_freshness_weight(trading_minutes: float) -> float:
    if trading_minutes <= 60:
        return 1.0
    if trading_minutes <= 180:
        return 0.80
    if trading_minutes <= 390:
        return 0.55
    if trading_minutes <= 780:
        return 0.30
    return 0.10


def parse_optional_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc)


def contest_minutes_remaining(contest: Dict[str, Any]) -> float:
    try:
        end_time = parse_local_datetime_as_utc(contest.get("endTime"))
        return max((end_time - datetime.now(timezone.utc)).total_seconds() / 60, 0)
    except Exception:
        return 390


def parse_local_datetime_as_utc(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_decision_context(
    contest: Dict[str, Any],
    portfolio: Dict[str, Any],
    market_context: Dict[str, Any],
    scored_candidates: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "contest": {
            "id": contest.get("id"),
            "name": contest.get("name"),
            "startTime": contest.get("startTime"),
            "endTime": contest.get("endTime"),
            "minutesRemaining": round(contest_minutes_remaining(contest), 2),
            "virtualBudget": contest.get("virtualBudget"),
        },
        "portfolio": {
            "cashBalance": portfolio.get("cashBalance"),
            "totalPortfolioValue": portfolio.get("totalPortfolioValue"),
            "holdings": portfolio.get("holdings", []),
        },
        "market": {
            "pulse": market_context.get("pulse", []),
            "candidateCount": len(scored_candidates),
        },
        "candidates": scored_candidates,
        "rules": {
            "allowedDecisions": ["BUY", "SELL", "HOLD"],
            "engine": "deterministic_v1",
            "buyQuantityPctCashMax": 25,
            "sellQuantityPctHoldingMax": 100,
            "minConfidence": AI_BOT_MIN_CONFIDENCE,
            "buyThreshold": AI_ANALYST_BUY_THRESHOLD,
            "sellThreshold": AI_ANALYST_SELL_THRESHOLD,
            "note": "For BUY, quantity_pct_cash means percent of cash. For SELL, it means percent of current holding quantity.",
        },
    }


def _buy_quantity_pct(candidate: Dict[str, Any]) -> float:
    score = candidate["scores"]["buy_score"]
    if score >= 85:
        return 25
    if score >= 75:
        return 18
    return 12


def _sell_quantity_pct(candidate: Dict[str, Any]) -> float:
    score = candidate["scores"]["sell_score"]
    effective_news_impact = float(candidate["scores"].get("effective_news_impact") or 0)
    if score >= 85 or effective_news_impact <= -0.75:
        return 100
    if score >= 75 or effective_news_impact <= -0.60:
        return 60
    return 35


def _decision_reason(action: str, candidate: Dict[str, Any]) -> str:
    scores = candidate.get("scores", {})
    event_type = candidate.get("event_type") or "no classified event"
    headline = candidate.get("catalyst") or "no current catalyst headline"
    if action == "BUY":
        return (
            f"Deterministic BUY: buy_score={scores.get('buy_score')}, "
            f"momentum_score={scores.get('momentum_score')}, "
            f"effective_news_impact={scores.get('effective_news_impact')}, "
            f"news_freshness_weight={scores.get('news_freshness_weight')}, "
            f"event_type={event_type}, catalyst={headline}"
        )
    return (
        f"Deterministic SELL: sell_score={scores.get('sell_score')}, "
        f"downside_momentum_score={scores.get('downside_momentum_score')}, "
        f"effective_news_impact={scores.get('effective_news_impact')}, "
        f"news_freshness_weight={scores.get('news_freshness_weight')}, "
        f"event_type={event_type}, catalyst={headline}"
    )


def _decision_risks(candidate: Dict[str, Any]) -> List[str]:
    risks = []
    effective_news_impact = float(candidate.get("scores", {}).get("effective_news_impact") or 0)
    pct = float(candidate.get("pChange") or 0)
    if abs(pct) >= 8:
        risks.append("large intraday move may reverse")
    if effective_news_impact == 0:
        risks.append("no meaningful news impact signal")
    if candidate.get("event_type") in {"REGULATORY_ACTION", "DEBT_STRESS", "MANAGEMENT_EXIT"}:
        risks.append("headline event may create gap risk")
    if not risks:
        risks.append("signal could be stale or already priced in")
    return risks


def apply_risk_gate(
    decision: TradeDecision,
    portfolio: Dict[str, Any],
    scored_candidates: Dict[str, Any],
) -> Tuple[bool, str, str, int, str]:
    if decision.decision == "HOLD":
        return False, "", "", 0, "Decision is HOLD"
    if decision.confidence < AI_BOT_MIN_CONFIDENCE:
        return False, "", "", 0, "Confidence below minimum threshold"
    if not decision.symbol:
        return False, "", "", 0, "Missing symbol"
    if decision.symbol not in scored_candidates:
        return False, "", "", 0, "Symbol is outside candidate universe"

    candidate = scored_candidates[decision.symbol]
    effective_news_impact = float(candidate["scores"].get("effective_news_impact") or 0)
    cash = float(portfolio.get("cashBalance") or 0)
    total_value = float(portfolio.get("totalPortfolioValue") or cash or 1)
    price = float(candidate.get("price") or 0)
    if price <= 0:
        return False, "", "", 0, "Candidate price is unavailable"

    holdings_by_symbol = {h.get("stockSymbol"): h for h in portfolio.get("holdings") or []}
    existing_holding = holdings_by_symbol.get(decision.symbol)

    if decision.decision == "BUY":
        if effective_news_impact <= AI_ANALYST_STRONG_NEGATIVE_NEWS:
            return False, "", "", 0, "BUY blocked by strong negative news impact"
        if candidate["scores"]["buy_score"] < AI_ANALYST_BUY_THRESHOLD:
            return False, "", "", 0, "BUY score below deterministic threshold"
        pct_cash = min(decision.quantity_pct_cash, 25)
        spend = cash * (pct_cash / 100)
        quantity = int(spend / price)
        if quantity <= 0:
            return False, "", "", 0, "Calculated BUY quantity is zero"
        existing_value = float((existing_holding or {}).get("currentValue") or 0)
        post_trade_concentration = (existing_value + quantity * price) / total_value if total_value > 0 else 1
        if post_trade_concentration > 0.40:
            return False, "", "", 0, "BUY would exceed 40% concentration limit"
        return True, "BUY", decision.symbol, quantity, ""

    if decision.decision == "SELL":
        if not existing_holding:
            return False, "", "", 0, "Cannot SELL symbol that is not held"
        if effective_news_impact >= AI_ANALYST_STRONG_POSITIVE_NEWS and candidate["scores"]["sell_score"] < 85:
            return False, "", "", 0, "SELL blocked by strong positive news impact"
        if candidate["scores"]["sell_score"] < AI_ANALYST_SELL_THRESHOLD:
            return False, "", "", 0, "SELL score below deterministic threshold"
        held_quantity = int(existing_holding.get("quantity") or 0)
        pct_holding = min(max(decision.quantity_pct_cash, 1), 100)
        quantity = int(held_quantity * (pct_holding / 100))
        if quantity <= 0:
            quantity = 1 if held_quantity > 0 else 0
        if quantity <= 0 or quantity > held_quantity:
            return False, "", "", 0, "Calculated SELL quantity is invalid"
        return True, "SELL", decision.symbol, quantity, ""

    return False, "", "", 0, "Unsupported decision"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def fetch_market_context() -> Dict[str, Any]:
    context = {"trending": {"gainers": [], "losers": [], "positive_catalysts": [], "negative_catalysts": []}, "pulse": []}
    try:
        trending_res = requests.get(f"{MARKET_DATA_URL}/api/market-data/trending", timeout=10)
        if trending_res.status_code == 200:
            data = trending_res.json()
            context["trending"]["gainers"] = data.get("gainers", [])
            context["trending"]["losers"] = data.get("losers", [])
    except Exception as exc:
        logger.warning("Could not fetch trending market data: %s", exc)

    try:
        catalyst_res = requests.get(f"{MARKET_DATA_URL}/api/market-data/catalysts/top", timeout=10)
        if catalyst_res.status_code == 200:
            cat_data = catalyst_res.json()
            context["trending"]["positive_catalysts"] = cat_data.get("positive", [])
            context["trending"]["negative_catalysts"] = cat_data.get("negative", [])
    except Exception as exc:
        logger.warning("Could not fetch top catalysts: %s", exc)

    try:
        pulse_res = requests.get(f"{MARKET_DATA_URL}/api/market-data/pulse", timeout=10)
        if pulse_res.status_code == 200:
            context["pulse"] = pulse_res.json()
    except Exception as exc:
        logger.warning("Could not fetch market pulse data: %s", exc)

    logger.info(
        "Market context fetched: gainers=%s losers=%s positive_catalysts=%s negative_catalysts=%s pulse_items=%s",
        len(context["trending"]["gainers"]),
        len(context["trending"]["losers"]),
        len(context["trending"]["positive_catalysts"]),
        len(context["trending"]["negative_catalysts"]),
        len(context["pulse"]),
    )
    return context


def job() -> None:
    logger.info("--- Starting bot execution cycle ---")
    market_context = fetch_market_context()
    for bot in active_bots:
        bot.execute_strategy(market_context)


if __name__ == "__main__":
    logger.info("AI Agent Service starting up...")
    time.sleep(20)

    decision_store = DecisionStore(AI_DATABASE_URL)
    decision_store.initialize()
    active_bots = [BotClient(bot["username"], bot["type"], decision_store) for bot in BOTS]

    scheduler = BlockingScheduler()
    scheduler.add_job(job, "interval", minutes=5)

    try:
        job()
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("AI Agent Service shutting down.")
