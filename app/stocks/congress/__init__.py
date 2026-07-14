"""The Congressional stock-trades slice — the buys and sells US Representatives and Senators
disclose under the STOCK Act.

A stock's (and the market's) recent congressional trading: which member traded it, which chamber
they sit in, whether they bought or sold, the disclosed dollar *range* (Congress reports bands,
never exact amounts), and when the trade happened vs. when it was disclosed. It's the political
sibling of the insider-transactions slice — the same "who with conviction is buying or selling
this" question, from a different public register.

Layered like every other slice (entities / ports / repository / db_repository / models /
use_cases / schemas, with the HTTP endpoints in ``app/stocks/endpoints/``). The live source is a
**keyless** community dataset (the House / Senate "stock watcher" JSON feeds), reached only from
``adapters/stock_watcher_congress_adapter.py`` — the sole vendor-aware module. The read path is
**DB-only** (served straight from ``stock_congress_trades``); a **weekly cron**
(``SyncCongressTrades``) fetches the whole feed once and distributes it across the anchor, so a
user request never waits on the multi-megabyte download. The same DB-only-read / out-of-band-cron
division of labour the insider-transactions slice uses.
"""
