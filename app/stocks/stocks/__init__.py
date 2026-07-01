"""The shared ``stocks`` anchor.

A tiny slice of its own: the ``stocks`` table every per-feature table hangs off of,
owned by no single feature. Just the model + a get-or-create helper for now (see
``models.py``). Feature slices (estimates, …) import ``StockRecord`` /
``get_or_create_stock`` from here and add their own child tables beside it.
"""
