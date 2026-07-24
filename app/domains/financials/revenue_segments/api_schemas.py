from datetime import date

from pydantic import BaseModel

from app.domains.financials.revenue_segments.entities import (
    RevenueSegment,
    RevenueSegmentation,
)


class RevenueSegmentResponse(BaseModel):
    fiscal_year: int
    period_end: date | None
    axis: str
    member: str
    label: str
    value: float

    @classmethod
    def from_segment(cls, segment: RevenueSegment) -> "RevenueSegmentResponse":
        return cls(
            fiscal_year=segment.fiscal_year,
            period_end=segment.period_end,
            axis=segment.axis.value,
            member=segment.member,
            label=segment.label,
            value=segment.value,
        )


class RevenueSegmentationResponse(BaseModel):
    symbol: str
    count: int
    fiscal_years: list[int]
    latest_fiscal_year: int | None
    segments: list[RevenueSegmentResponse]

    @classmethod
    def from_segmentation(
        cls, segmentation: RevenueSegmentation
    ) -> "RevenueSegmentationResponse":
        return cls(
            symbol=segmentation.symbol,
            count=len(segmentation.segments),
            fiscal_years=list(segmentation.fiscal_years),
            latest_fiscal_year=segmentation.latest_fiscal_year,
            segments=[
                RevenueSegmentResponse.from_segment(s) for s in segmentation.segments
            ],
        )
