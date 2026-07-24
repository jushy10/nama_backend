from pydantic import BaseModel, ConfigDict, Field

from app.domains.shared.entities import StockPerformance


class StockPerformanceResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    one_week: float | None = Field(default=None, alias="1w")
    one_month: float | None = Field(default=None, alias="1m")
    three_month: float | None = Field(default=None, alias="3m")
    six_month: float | None = Field(default=None, alias="6m")
    ytd: float | None = Field(default=None, alias="ytd")
    one_year: float | None = Field(default=None, alias="1y")

    @classmethod
    def from_performance(
        cls, perf: StockPerformance | None
    ) -> "StockPerformanceResponse | None":
        # Trailing windows are best-effort enrichment everywhere they appear, so the
        # presenter accepts the None and keeps each caller a single expression.
        if perf is None:
            return None
        return cls(
            one_week=perf.one_week,
            one_month=perf.one_month,
            three_month=perf.three_month,
            six_month=perf.six_month,
            ytd=perf.ytd,
            one_year=perf.one_year,
        )
