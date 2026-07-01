"""Vendor / infrastructure adapters for the stocks feature.

Each module here implements a port declared by a feature slice and is the only code
that knows a given vendor or storage detail exists (translating its models into our
entities and its failures into our domain exceptions). File names end in
``_adapter.py``. Currently home to the analyst-estimates and earnings-timeline
adapters (the yfinance live sources and their DB-cache decorators); other features'
adapters still live beside their code and can migrate here over time.
"""
