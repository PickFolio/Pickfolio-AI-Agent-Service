# AI Agent Service (pickfolio-ai-agent-service)

This is a dedicated Python microservice responsible for orchestrating AI bot opponents within the Pickfolio platform. It runs as a background process, autonomously participating in live public contests to ensure human players always have competitive benchmarks.

## Architecture

The AI Agent Service is completely decoupled from the core Java backend. It operates exactly like a real human user:
1. **Authentication:** It logs into the `Auth Service` via standard REST endpoints and receives a JWT.
2. **Context Gathering:** A scheduler (using `APScheduler`) wakes the bots up every 5 minutes during active hours. They fetch their active contests and current portfolio balances from the `Contest Service`.
3. **Market Analysis:** The service fetches global trending data (top gainers and losers) from the `Market Data Service` once per cycle and distributes it to the bots.
4. **Execution:** If a bot's strategy dictates a trade, it sends a `POST` request to the `Contest Service`'s standard transaction endpoint (`/api/contests/{contestId}/transactions`). 

*Security Note: Because bots use the standard user REST APIs, they are naturally bound by the exact same validation rules as humans (insufficient funds, market hours, valid symbols).*

## V1 Strategies (Rules-Based / Algorithmic)

Currently, the bots do not use LLMs. They rely on hardcoded financial heuristics to ensure fast, predictable testing of the pipeline.

### 1. WarrenBot (Persona: `VALUE`)
*   **Strategy:** "Buy the dip."
*   **Logic:** Monitors the top daily losers. If it has > ₹10,000 in cash, it picks a random stock from the losers list and invests 20% of its available cash. It is a strict holder and does not sell intraday.

### 2. QuantBot (Persona: `MOMENTUM`)
*   **Strategy:** "Ride the wave."
*   **Logic:** Monitors the top daily gainers. If it has > ₹5,000 in cash, it randomly selects one of the top 3 highest gainers of the day and buys it in chunks, utilizing 40% of its available cash per trade.

### 3. ChadBot (Persona: `WSB_YOLO`)
*   **Strategy:** "Maximum chaos and volatility."
*   **Logic:** 
    *   *Panic Mode:* Every cycle, there is a 15% probability that it will panic and issue a `SELL` order for 100% of its current holdings.
    *   *All-In Mode:* When holding cash, it ignores diversification. It targets the absolute #1 highest daily gainer and dumps 95% of its entire cash balance into that single stock.

## Roadmap: V2 (LLM Integration)

The architecture is explicitly designed to support a seamless transition to true AI decision-making. 

In V2, the `execute_strategy()` function will be updated to:
1. Serialize the bot's current portfolio and live market data into a JSON prompt.
2. Send the prompt to an external LLM (e.g., OpenAI GPT-4o-mini or a local open-source model) along with system instructions defining the persona.
3. Parse the LLM's JSON response containing the desired `stockSymbol`, `transactionType`, and `quantity` to execute the trade.
