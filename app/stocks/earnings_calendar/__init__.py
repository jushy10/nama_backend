"""The market-wide earnings-calendar slice.

A table-less read: *which* companies are scheduled to report earnings on *which* upcoming
days, aggregated across the whole screened universe from the scheduled dates the
quarterly-earnings slice already stores (``stock_quarterly_earnings``), grouped by day and
joined to each company's name + sector. No table and no cron of its own — it's a projection
of data other slices' syncs already wrote, so a read is one indexed DB query, never a vendor
call.
"""
