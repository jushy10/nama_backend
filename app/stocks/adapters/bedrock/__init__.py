"""Claude-on-Amazon-Bedrock adapters for the stocks feature.

Every module here implements one of the slice's AI-analysis ports with Claude on
Amazon Bedrock (via the Anthropic SDK) and is the only code that knows Bedrock
exists — translating the gathered figures into a compact prompt + forced tool call
and the model's structured result into our entities, and any Bedrock/SDK failure
into ``StockDataUnavailable``. Grouped here because they share that scaffolding
(lazy SDK import, forced-tool structured output, retry-once on an empty required
list) while their prompts differ per asset/scope: the per-stock buy/hold/sell read
(``analysis_adapter``), its ETF sibling (``etf_analysis_adapter``), the earnings
summary (``earnings_analysis_adapter``), the market-sector read
(``sector_analysis_adapter``), and the whole-market summary
(``market_summary_adapter``).

Auth is the runtime's job, not ours: Bedrock authenticates through the process's
AWS credentials (the ECS task role in prod), so — unlike every other vendor in the
slice — there is no API key to read or pass. The ``bedrock`` extra is optional and
imported lazily, so the app and the offline tests import without it; the wiring
turns a missing extra into a 503.
"""
