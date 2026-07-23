# Contributing

Contributions are welcome, especially point-in-time data validation, broker execution semantics, SEC/XBRL parsing, replay fixtures for adverse conditions, and independent backtest review.

1. Open an issue describing the behavior, data lineage, and safety impact.
2. Keep research scoring, broker feasibility, and account risk as separate decisions.
3. Add tests for future-data exclusion and every new rejection path.
4. Never include vendor credentials, licensed payloads, personal account data, or real operator tokens.
5. Run `python -m pytest`, `python -m ruff check .`, and `python -m ruff format --check .`.

Changes that relax manual approval, live-routing interlocks, daily loss limits, audit coverage, or the no-losing-short-add invariant require explicit maintainer and independent risk review. Demo data must remain conspicuously labeled as synthetic.

By contributing, you agree that your contribution is licensed under Apache-2.0.
