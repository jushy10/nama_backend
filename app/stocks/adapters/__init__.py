"""Vendor / infrastructure adapters for the stocks feature.

Each module here implements a port declared by a feature slice and is the only code
that knows a given vendor or storage detail exists (translating its models into our
entities and its failures into our domain exceptions). File names end in
``_adapter.py``. Currently home to the earnings adapters (yfinance live sources, their
DB-cache decorators, and the annual-earnings-backed estimates projection); other
features' adapters still live beside their code and can migrate here over time.

Not every module here is a port adapter: ``yfinance_session.py`` is shared vendor
*infrastructure* the yfinance adapters call — it paces Yahoo requests and retries a
transient crumb 401 once with a fresh cookie/crumb — rather than a port implementation.
"""
