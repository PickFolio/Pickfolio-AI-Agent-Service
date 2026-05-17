import os
import time
import logging
import requests
import random
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AIAgent")

AUTH_URL = os.getenv("AUTH_SERVICE_URL", "http://auth-service:8080")
CONTEST_URL = os.getenv("CONTEST_SERVICE_URL", "http://contest-service:8081")
MARKET_DATA_URL = os.getenv("MARKET_DATA_SERVICE_URL", "http://market-data-service:8082")

BOTS = [
    {"username": "warren_bot", "type": "VALUE"},
    {"username": "chad_bot", "type": "WSB_YOLO"},
    {"username": "quant_bot", "type": "MOMENTUM"}
]
PASSWORD_SUFFIX = "_secret_123!"

class BotClient:
    def __init__(self, username, persona_type):
        self.username = username
        self.password = f"{username}{PASSWORD_SUFFIX}"
        self.persona_type = persona_type
        self.token = None
        self.session = requests.Session()

    def login(self):
        try:
            res = self.session.post(f"{AUTH_URL}/api/auth/login", json={
                "username": self.username,
                "password": self.password,
                "deviceInfo": "AI_Agent_Service"
            }, timeout=10)
            if res.status_code == 200:
                self.token = res.json().get("accessToken")
                self.session.headers.update({"Authorization": f"Bearer {self.token}"})
                logger.info(f"{self.username} logged in successfully.")
            else:
                logger.error(f"{self.username} failed to login: {res.text}")
        except Exception as e:
            logger.error(f"{self.username} login error: {e}")

    def execute_strategy(self, trending_data):
        if not self.token:
            self.login()
            if not self.token: return

        try:
            res = self.session.get(f"{CONTEST_URL}/api/contests/my-contests", timeout=10)
            if res.status_code == 401:  # Token expired
                self.token = None
                return
            if res.status_code != 200: return
            
            contests = res.json()
            live_contests = [c for c in contests if c.get("status") == "LIVE"]
            
            if not live_contests:
                logger.info(f"{self.username} has no live contests currently.")
                return

            for contest in live_contests:
                self._play_contest(contest, trending_data)
        except Exception as e:
            logger.error(f"{self.username} strategy execution error: {e}")

    def _play_contest(self, contest, trending_data):
        contest_id = contest["id"]
        res = self.session.get(f"{CONTEST_URL}/api/contests/{contest_id}/portfolio", timeout=10)
        if res.status_code != 200: return
        portfolio = res.json()

        cash = portfolio.get("cashBalance", 0)
        holdings = portfolio.get("holdings", [])

        # Prevent trading if Market Data Service hasn't fully populated trending data yet
        if not trending_data.get("gainers") and not trending_data.get("losers"):
            return

        # Simple V1 Strategies based on persona
        if self.persona_type == "VALUE":
            self._value_strategy(contest_id, cash, holdings, trending_data)
        elif self.persona_type == "WSB_YOLO":
            self._yolo_strategy(contest_id, cash, holdings, trending_data)
        elif self.persona_type == "MOMENTUM":
            self._momentum_strategy(contest_id, cash, holdings, trending_data)

    def _execute_trade(self, contest_id, txn_type, symbol, quantity):
        if quantity <= 0: return
        try:
            res = self.session.post(f"{CONTEST_URL}/api/contests/{contest_id}/transactions", json={
                "transactionType": txn_type,
                "stockSymbol": symbol,
                "quantity": quantity
            }, timeout=10)
            if res.status_code == 200:
                logger.info(f"{self.username} executed {txn_type} {quantity} shares of {symbol} in contest {contest_id}")
            else:
                logger.warning(f"{self.username} trade failed: {res.text}")
        except Exception as e:
            logger.error(f"{self.username} trade error: {e}")

    def _value_strategy(self, contest_id, cash, holdings, trending):
        # Value Bot: Buys "losers" slowly, looking for a bounce back. Holds what it has.
        if cash > 10000 and trending.get("losers"):
            target = random.choice(trending["losers"])
            price = target.get("price", 100)
            if price > 0:
                qty = int((cash * 0.2) / price) # Invest 20% of available cash
                self._execute_trade(contest_id, "BUY", target["symbol"], qty)

    def _yolo_strategy(self, contest_id, cash, holdings, trending):
        # YOLO Bot: Randomly panics and sells everything, then goes all-in on the top gainer.
        if holdings and random.random() < 0.15: # 15% chance to panic sell
            logger.info(f"{self.username} is panic selling!")
            for h in holdings:
                self._execute_trade(contest_id, "SELL", h["stockSymbol"], h["quantity"])
            return # Wait for next cycle to buy
        
        if cash > 1000 and trending.get("gainers"):
            target = trending["gainers"][0] # Go for the absolute top gainer
            price = target.get("price", 100)
            if price > 0:
                qty = int((cash * 0.95) / price) # 95% all in
                self._execute_trade(contest_id, "BUY", target["symbol"], qty)

    def _momentum_strategy(self, contest_id, cash, holdings, trending):
        # Momentum Bot: Buys gainers in chunks to ride the wave.
        if cash > 5000 and trending.get("gainers"):
            target = random.choice(trending["gainers"][:3]) # Pick from top 3
            price = target.get("price", 100)
            if price > 0:
                qty = int((cash * 0.4) / price) # 40% chunks
                self._execute_trade(contest_id, "BUY", target["symbol"], qty)


def job():
    logger.info("--- Starting bot execution cycle ---")
    # Fetch global market data once per cycle to save network calls
    trending_data = {"gainers": [], "losers": []}
    try:
        res = requests.get(f"{MARKET_DATA_URL}/api/market-data/trending", timeout=10)
        if res.status_code == 200:
            trending_data = res.json()
    except Exception as e:
        logger.warning(f"Could not fetch trending market data: {e}")

    for bot in active_bots:
        bot.execute_strategy(trending_data)

if __name__ == "__main__":
    logger.info("AI Agent Service starting up...")
    # Delay to allow Java Auth and Contest services to boot fully in docker-compose
    time.sleep(20) 
    
    active_bots = [BotClient(b["username"], b["type"]) for b in BOTS]
    
    scheduler = BlockingScheduler()
    # Run every 5 minutes. (In production, you'd match this to market open hours)
    scheduler.add_job(job, 'interval', minutes=5)
    
    try:
        # Run one cycle immediately on boot
        job()
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("AI Agent Service shutting down.")
