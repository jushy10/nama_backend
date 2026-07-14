"""Application use cases for the daily market brief.

Two actions, both pure orchestration over the ports so they run offline in tests against
hand-written fakes and know nothing of Alpaca, Bedrock, HTTP, or SQLAlchemy:

- ``GetDailyBrief`` — the read path. Returns a stored brief (a given date, or the latest)
  straight from the repository. DB-only: a read never gathers boards or calls the model.
- ``GenerateDailyBrief`` — the out-of-band daily generation. Gathers the existing
  whole-market reads (the index board, the sector board, and the heat map's per-stock day
  moves — each best-effort, DB-backed where the underlying read is), hands the assembled
  snapshot to the model, and upserts today's row **only if the result is complete**. Invoked
  by the cron endpoint.

The generation reuses the reads the app already has rather than re-deriving them: the two
Alpaca boards (``GetMarketOverview`` / ``GetSectorPerformance``) and the heat map
(``GetStockHeatMap`` — a DB read of the screened universe + one best-effort batched quote).
Every leg is wrapped so one source being down degrades the brief rather than sinking it, and
a snapshot with no headline board at all skips the model call outright.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.stocks.brief.entities import (
    BriefHeadline,
    BriefIndexMove,
    BriefMover,
    BriefSectorMove,
    MarketBrief,
    MarketBriefContext,
)
from app.stocks.brief.ports import MarketBriefProvider
from app.stocks.brief.repository import MarketBriefRepository
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.heatmap.entities import HeatMap, HeatMapScope
from app.stocks.heatmap.use_cases import GetStockHeatMap
from app.stocks.market.entities import MarketIndexPerformance, SectorPerformance
from app.stocks.market.use_cases import GetMarketOverview, GetSectorPerformance
from app.stocks.news.repository import NewsRepository

logger = logging.getLogger(__name__)


class GetDailyBrief:
    """Use case: read a stored daily brief — a specific date, or the latest.

    DB-only and best-effort: an absent date (no brief written that day) is a clean ``None``
    the endpoint maps to a 404, never a regeneration or a vendor call."""

    def __init__(self, repository: MarketBriefRepository) -> None:
        self._repository = repository

    def execute(self, brief_date: date | None = None) -> MarketBrief | None:
        """The brief for ``brief_date``, or — when it's ``None`` — the most recent brief."""
        if brief_date is None:
            return self._repository.latest()
        return self._repository.get(brief_date)


@dataclass(frozen=True)
class MarketBriefSyncReport:
    """The outcome of one generation run: whether a brief was written and for which date.

    ``generated`` is ``False`` when the run gathered no usable market data (nothing to write)
    or the model returned an incomplete result (not worth storing); ``brief_date`` is the day
    the run targeted."""

    generated: bool
    brief_date: date


class GenerateDailyBrief:
    """Gather the day's whole-market reads, write a brief from them, and store it.

    Constructor-injected with the reads it composes (the two boards + the heat map), the
    model port, and the store. ``scope`` picks the universe the movers/breadth are read over
    (the S&P 500 by default — the broad market); ``movers`` caps each of the gainer/loser
    lists. A ``today`` clock keeps the dated row deterministic in tests.
    """

    # Only headlines this recent count as a catalyst for "today's" moves — a stale
    # top-of-feed article isn't why a stock moved on the day. Generous enough (a few days)
    # to carry Friday/weekend news into a Monday brief.
    _NEWS_MAX_AGE_DAYS = 3

    def __init__(
        self,
        overview: GetMarketOverview,
        sectors: GetSectorPerformance,
        heatmap: GetStockHeatMap,
        provider: MarketBriefProvider,
        repository: MarketBriefRepository,
        *,
        news: NewsRepository | None = None,
        scope: HeatMapScope = HeatMapScope.SP500,
        movers: int = 5,
        headlines: int = 8,
        today=None,
    ) -> None:
        self._overview = overview
        self._sectors = sectors
        self._heatmap = heatmap
        self._provider = provider
        self._repository = repository
        # Best-effort news reader (DB-only). None → the brief carries no catalyst headlines.
        self._news = news
        self._scope = scope
        self._movers = movers
        self._headlines = headlines
        self._today = today or (lambda: datetime.now(timezone.utc).date())

    def execute(self, brief_date: date | None = None) -> MarketBrief | None:
        """Generate and store today's brief (or ``brief_date``'s), returning it — or ``None``
        when there was nothing to write.

        Best-effort throughout: the context gather never raises (each leg degrades to empty),
        and a model failure is caught and reported as "not generated" rather than crashing the
        run. A brief is stored only when it's ``is_complete``, so a hollow model result is never
        frozen into the store."""
        target = brief_date or self._today()
        context = self._gather()
        if not context.has_data:
            logger.warning("market-brief: no market data gathered; skipping %s", target)
            return None
        try:
            brief = self._provider.generate(context, target)
        except (StockNotFound, StockDataUnavailable) as exc:
            logger.warning("market-brief: model call failed for %s: %s", target, exc)
            return None
        if not brief.is_complete:
            logger.warning("market-brief: model returned an incomplete brief for %s", target)
            return None
        self._repository.upsert(brief)
        return brief

    def _gather(self) -> MarketBriefContext:
        """Assemble the market snapshot from the existing reads — each best-effort.

        The index and sector boards are live Alpaca (a handful of proxy ETFs); the movers and
        breadth come from the heat map (a DB read of the screened universe + one best-effort
        batched day-change quote). Any leg that's unavailable degrades to empty rather than
        sinking the gather."""
        indexes = tuple(_index_move(i) for i in self._read(self._overview.execute))
        sectors = tuple(_sector_move(s) for s in self._read(self._sectors.execute))
        heatmap = self._read(lambda: self._heatmap.execute(self._scope))
        gainers, losers, advancers, decliners, quoted = _movers_and_breadth(
            heatmap, self._movers
        )
        headlines = self._headlines_for(gainers + losers)
        return MarketBriefContext(
            indexes=indexes,
            sectors=sectors,
            gainers=gainers,
            losers=losers,
            advancers=advancers,
            decliners=decliners,
            quoted=quoted,
            headlines=headlines,
        )

    def _headlines_for(
        self, movers: tuple[BriefMover, ...]
    ) -> tuple[BriefHeadline, ...]:
        """The day's movers' most recent catalyst headlines, read **DB-only** from the news
        store — the "why" behind the moves the model can cite.

        For each mover, reads its stored news (never a live fetch — the store is kept warm by
        the daily news sync) and keeps the newest article *if it's recent enough* to be a
        catalyst for today's move. Deduped across movers by headline (the same wire story is
        often filed under several tickers), freshest first, capped. Best-effort throughout: no
        news reader, or any per-ticker read hiccup, simply drops that catalyst — headlines are
        enrichment and never sink the brief."""
        if self._news is None or not movers:
            return ()
        cutoff = self._today() - timedelta(days=self._NEWS_MAX_AGE_DAYS)
        collected: list[BriefHeadline] = []
        seen: set[str] = set()
        for mover in movers:
            try:
                stored = self._news.get(mover.ticker)
            except Exception:  # a per-ticker read hiccup drops only that catalyst
                continue
            if stored is None or stored.is_empty:
                continue
            article = stored.articles[0]  # newest first
            if article.published_at.date() < cutoff:
                continue  # stale — not a catalyst for today's move
            key = article.title.strip().casefold()
            if not key or key in seen:
                continue  # the same story filed under multiple movers
            seen.add(key)
            collected.append(
                BriefHeadline(
                    ticker=mover.ticker,
                    title=article.title,
                    publisher=article.publisher,
                    published_at=article.published_at,
                )
            )
        collected.sort(
            key=lambda h: h.published_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return tuple(collected[: self._headlines])

    @staticmethod
    def _read(call):
        """Run one gather leg, degrading a "no data" / "source down" to an empty result so a
        single unavailable source never sinks the whole brief."""
        try:
            return call() or ()
        except (StockNotFound, StockDataUnavailable):
            return ()


def _index_move(index: MarketIndexPerformance) -> BriefIndexMove:
    perf = index.performance
    return BriefIndexMove(
        name=index.name,
        symbol=index.symbol,
        change_percent=index.change_percent,
        one_week=perf.one_week if perf else None,
        one_month=perf.one_month if perf else None,
        one_year=perf.one_year if perf else None,
    )


def _sector_move(sector: SectorPerformance) -> BriefSectorMove:
    return BriefSectorMove(
        sector=sector.sector,
        symbol=sector.symbol,
        change_percent=sector.change_percent,
    )


def _movers_and_breadth(
    heatmap: HeatMap | tuple, movers: int
) -> tuple[tuple[BriefMover, ...], tuple[BriefMover, ...], int, int, int]:
    """Flatten the heat map's tiles into the day's biggest movers + the market breadth.

    Walks the sector → industry → cell tree, keeping every tile that has a live day-change
    (its ``sector`` comes from the parent group), then ranks them: the top ``movers`` up-moves
    (``gainers``, largest first) and the ``movers`` down-moves (``losers``, most-negative
    first). Breadth counts how many quoted tiles rose vs fell. An empty / absent map yields
    empty lists and zero counts."""
    if not isinstance(heatmap, HeatMap):
        return (), (), 0, 0, 0
    rows: list[BriefMover] = []
    for sector in heatmap.sectors:
        for industry in sector.industries:
            for cell in industry.cells:
                if cell.change_percent is None:
                    continue
                rows.append(
                    BriefMover(
                        ticker=cell.ticker,
                        name=cell.name,
                        sector=sector.sector,
                        change_percent=cell.change_percent,
                    )
                )
    advancers = sum(1 for r in rows if r.change_percent > 0)
    decliners = sum(1 for r in rows if r.change_percent < 0)
    by_change = sorted(rows, key=lambda r: r.change_percent, reverse=True)
    gainers = tuple(r for r in by_change[:movers] if r.change_percent > 0)
    losers = tuple(
        r for r in reversed(by_change[-movers:]) if r.change_percent < 0
    )
    return gainers, losers, advancers, decliners, len(rows)
