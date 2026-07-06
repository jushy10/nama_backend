"""The ETF sub-slice.

Owns the searchable top-ETF set: the curated US exchange-traded funds Yahoo ranks as the
"top ETFs", screened out of band and stored in the slice's own ``etfs`` table so the app
*knows* the big funds rather than only whatever tickers it's been asked about. Built on the
same skeleton as the stock ``universe`` slice — a live screen + a read/search side — but
deliberately thinner and with one key difference: it owns its **own** table (an ETF is not a
company, so it must not become a ``stocks`` anchor row that would leak funds into the stock
universe search), and an ETF carries no sector/industry, earnings-derived growth or index
membership, so there are no classification menus or membership filters — just AUM, expense
ratio and year-to-date return.
"""
