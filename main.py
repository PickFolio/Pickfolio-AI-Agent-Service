import json
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError
from urllib.parse import parse_qsl, urlparse

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("AIAgent")

AUTH_URL = os.getenv("AUTH_SERVICE_URL", "http://auth-service:8080")
CONTEST_URL = os.getenv("CONTEST_SERVICE_URL", "http://contest-service:8081")
MARKET_DATA_URL = os.getenv("MARKET_DATA_SERVICE_URL", "http://market-data-service:8082")

AI_DATABASE_URL = os.getenv("AI_DATABASE_URL", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
AI_BOT_EXECUTE_TRADES = os.getenv("AI_BOT_EXECUTE_TRADES", "true").lower() == "true"
AI_BOT_DECISION_INTERVAL_MINUTES = int(os.getenv("AI_BOT_DECISION_INTERVAL_MINUTES", "15"))
AI_BOT_MIN_CONFIDENCE = float(os.getenv("AI_BOT_MIN_CONFIDENCE", "0.60"))

BOTS = [
    {"username": "warren_bot", "type": "VALUE"},
    {"username": "chad_bot", "type": "WSB_YOLO"},
    {"username": "quant_bot", "type": "MOMENTUM"},
    {"username": "analyst_bot", "type": "AI_ANALYST"},
]
PASSWORD_SUFFIX = "_secret_123!"


class LlmTradeDecision(BaseModel):
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

        if not trending.get("gainers") and not trending.get("losers"):
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

        scored_candidates = score_candidates(contest, portfolio, candidates)
        prompt_snapshot = build_prompt_snapshot(contest, portfolio, market_context, scored_candidates)
        decision, raw_response, llm_error = request_llm_decision(prompt_snapshot)
        if llm_error:
            self._persist_ai_decision(contest_id, decision, scored_candidates, prompt_snapshot, "REJECTED", "SKIPPED", llm_error, raw_response)
            return

        risk_ok, txn_type, symbol, quantity, risk_error = apply_risk_gate(decision, portfolio, scored_candidates)
        if not risk_ok:
            self._persist_ai_decision(contest_id, decision, scored_candidates, prompt_snapshot, "REJECTED", "SKIPPED", risk_error, raw_response)
            logger.info("%s AI decision rejected for contest %s: %s", self.username, contest_id, risk_error)
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

        self._persist_ai_decision(
            contest_id,
            decision,
            scored_candidates,
            prompt_snapshot,
            "APPROVED",
            execution_status,
            error_message,
            raw_response,
            quantity=quantity,
            executed_at=executed_at,
        )

    def _persist_ai_decision(
        self,
        contest_id: str,
        decision: LlmTradeDecision,
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


def hold_decision(reason: str) -> LlmTradeDecision:
    return LlmTradeDecision(decision="HOLD", confidence=0, strategy="hold_cash", reason=reason)


def build_candidates(portfolio: Dict[str, Any], trending: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    candidates: Dict[str, Dict[str, Any]] = {}
    for group_name in ("gainers", "losers"):
        for rank, mover in enumerate(trending.get(group_name) or [], start=1):
            symbol = mover.get("symbol")
            price = float(mover.get("price") or 0)
            if symbol and price > 0:
                candidates[symbol] = {
                    "symbol": symbol,
                    "price": price,
                    "change": float(mover.get("change") or 0),
                    "pChange": float(mover.get("pChange") or 0),
                    "source": group_name,
                    "rank": rank,
                    "holding": None,
                }

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

        if source == "gainers":
            momentum = clamp(50 + pct * 5, 0, 100)
            mean_reversion = clamp(35 - pct * 2, 0, 100)
        elif source == "losers":
            momentum = clamp(45 + pct * 3, 0, 100)
            mean_reversion = clamp(50 + abs(pct) * 3, 0, 100)
        else:
            momentum = clamp(50 + pct * 4, 0, 100)
            mean_reversion = 45

        concentration = current_value / total_value if total_value > 0 else 0
        portfolio_fit = clamp(80 - concentration * 120, 0, 100)
        cash_fit = 70 if cash > 1000 else 20
        overextension_penalty = max(0, abs(pct) - 8) * 3
        quality = clamp((momentum * horizon_weight) + (mean_reversion * (1 - horizon_weight)) * 0.5 + portfolio_fit * 0.2 + cash_fit * 0.1 - overextension_penalty, 0, 100)

        scored[symbol] = {
            **candidate,
            "scores": {
                "momentum": round(momentum, 2),
                "mean_reversion": round(mean_reversion, 2),
                "portfolio_fit": round(portfolio_fit, 2),
                "cash_fit": cash_fit,
                "trade_quality": round(quality, 2),
            },
        }
    return dict(sorted(scored.items(), key=lambda item: item[1]["scores"]["trade_quality"], reverse=True)[:8])


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


def build_prompt_snapshot(
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
            "buyQuantityPctCashMax": 25,
            "sellQuantityPctHoldingMax": 100,
            "minConfidence": AI_BOT_MIN_CONFIDENCE,
            "note": "For BUY, quantity_pct_cash means percent of cash. For SELL, it means percent of current holding quantity.",
        },
    }


def request_llm_decision(prompt_snapshot: Dict[str, Any]) -> Tuple[LlmTradeDecision, Dict[str, Any], str]:
    if not OPENROUTER_API_KEY:
        return hold_decision("OPENROUTER_API_KEY is not configured"), {}, "OPENROUTER_API_KEY is not configured"

    system_prompt = (
        "You are Pickfolio's AI_ANALYST trading bot. Decide one action for a fantasy stock contest. "
        "Use only the provided candidates and portfolio. Return strict JSON only. "
        "Prefer HOLD when evidence is weak. Never invent symbols."
    )
    user_prompt = json.dumps(prompt_snapshot, separators=(",", ":"), default=str)
    try:
        res = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://pickfolio.app",
                "X-Title": "Pickfolio AI Agent Service",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
            },
            timeout=20,
        )
        raw = safe_json(res)
        if res.status_code != 200:
            return hold_decision("LLM provider returned an error"), raw, f"LLM provider error {res.status_code}: {res.text[:300]}"

        content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = parse_json_content(content)
        return LlmTradeDecision.model_validate(parsed), raw, ""
    except (ValidationError, ValueError) as exc:
        return hold_decision("Invalid LLM response"), {"error": str(exc)}, f"Invalid LLM response: {exc}"
    except Exception as exc:
        return hold_decision("LLM request failed"), {"error": str(exc)}, f"LLM request failed: {exc}"


def safe_json(response: requests.Response) -> Dict[str, Any]:
    try:
        return response.json()
    except Exception:
        return {"text": response.text}


def parse_json_content(content: str) -> Dict[str, Any]:
    stripped = content.strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.DOTALL)
    if match:
        stripped = match.group(1).strip()
    return json.loads(stripped)


def apply_risk_gate(
    decision: LlmTradeDecision,
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
    cash = float(portfolio.get("cashBalance") or 0)
    total_value = float(portfolio.get("totalPortfolioValue") or cash or 1)
    price = float(candidate.get("price") or 0)
    if price <= 0:
        return False, "", "", 0, "Candidate price is unavailable"

    holdings_by_symbol = {h.get("stockSymbol"): h for h in portfolio.get("holdings") or []}
    existing_holding = holdings_by_symbol.get(decision.symbol)

    if decision.decision == "BUY":
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
    context = {"trending": {"gainers": [], "losers": []}, "pulse": []}
    try:
        trending_res = requests.get(f"{MARKET_DATA_URL}/api/market-data/trending", timeout=10)
        if trending_res.status_code == 200:
            context["trending"] = trending_res.json()
    except Exception as exc:
        logger.warning("Could not fetch trending market data: %s", exc)

    try:
        pulse_res = requests.get(f"{MARKET_DATA_URL}/api/market-data/pulse", timeout=10)
        if pulse_res.status_code == 200:
            context["pulse"] = pulse_res.json()
    except Exception as exc:
        logger.warning("Could not fetch market pulse data: %s", exc)

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
