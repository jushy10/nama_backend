"""The daily-market-brief slice.

A once-a-day, AI-written plain-language read of how the whole US market is moving —
the headline indices, the sector rotation, and the day's biggest movers — stored one
row per calendar date so each brief is a durable, dated artifact (compounding SEO) rather
than a per-request regeneration. Generated out of band by a daily cron
(``GenerateDailyBrief``) and served DB-only (``GetDailyBrief``); the read path never calls
a vendor or the model.
"""
