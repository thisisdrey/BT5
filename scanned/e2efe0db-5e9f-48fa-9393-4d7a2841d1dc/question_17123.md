Q17123: provider-backed view divergence in protected provider read path when the provider is in reference mode with no custom source

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with synthetic two-feed provider configurations created through the scoped factory path while the provider is in reference mode with no custom source, so that provider-backed lens or quoter output diverges deterministically from the live provider path used by the swap along `pool.swap -> ProtectedPriceProvider.getBidAndAskPrice -> oracle.price(feedId, pool) -> price guard and confidence shaping`, corrupting `MAX_TIME_DELTA`, price-guard bounds, confidence-shaped bid/ask, and the final quote used for settlement? This is the canonical non-anchored provider path for live swaps, so public timing against stale or wide-spread edges matters. Consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch.

Target
- File/function: smart-contracts-poc/contracts/ProtectedPriceProvider.sol::getBidAndAskPrice and _computeBidAsk
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: synthetic two-feed provider configurations created through the scoped factory path
- Exploit idea: Reach `pool.swap -> ProtectedPriceProvider.getBidAndAskPrice -> oracle.price(feedId, pool) -> price guard and confidence shaping` in a live public flow and show that consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch. The exact value at risk is `MAX_TIME_DELTA`, price-guard bounds, confidence-shaped bid/ask, and the final quote used for settlement.
- Invariant to test: Provider-backed user or integrator views must not diverge from the live swap semantics enough to cause deterministic losses. The concrete assertion should cover `MAX_TIME_DELTA`, price-guard bounds, confidence-shaped bid/ask, and the final quote used for settlement.
- Expected Immunefi impact: Medium predictable loss-making execution from trusted views or quotes.
- Fast validation: Stress swaps around time, spread, and price-guard boundaries and assert every rejected quote fails closed before the pool can settle a trade.
