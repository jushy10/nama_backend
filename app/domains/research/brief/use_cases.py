from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.domains.research.brief.entities import (
    BriefHeadline,
    BriefIndexMove,
    BriefMover,
    BriefSectorMove,
    MarketBrief,
    MarketBriefContext,
)
from app.domains.research.brief.interfaces import MarketBriefAdapter
from app.domains.research.brief.interfaces import MarketBriefRepositoryAdapter
from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.markets.heatmap.entities import HeatMap, HeatMapScope
from app.domains.markets.heatmap.use_cases import GetStockHeatMap
from app.domains.markets.boards.entities import MarketIndexPerformance, SectorPerformance
from app.domains.markets.boards.use_cases import GetMarketOverview, GetSectorPerformance
from app.domains.coverage.news.interfaces import NewsRepositoryAdapter

logger = logging.getLogger(__name__)


class GetDailyBrief:
    def __init__(self, repository: MarketBriefRepositoryAdapter) -> None:
        self._repository = repository

    def execute(self, brief_date: date | None = None) -> MarketBrief | None:
        if brief_date is None:
            return self._repository.latest()
        return self._repository.get(brief_date)


@dataclass(frozen=True)
class MarketBriefSyncReport:
    generated: bool
    brief_date: date


class GenerateDailyBrief:
    # Only headlines this recent count as a catalyst for "today's" moves — a stale
    # top-of-feed article isn't why a stock moved on the day. Generous enough (a few days)
    # to carry Friday/weekend news into a Monday brief.
    _NEWS_MAX_AGE_DAYS = 3

    def __init__(
        self,
        overview: GetMarketOverview,
        sectors: GetSectorPerformance,
        heatmap: GetStockHeatMap,
        provider: MarketBriefAdapter,
        repository: MarketBriefRepositoryAdapter,
        *,
        news: NewsRepositoryAdapter | None = None,
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
