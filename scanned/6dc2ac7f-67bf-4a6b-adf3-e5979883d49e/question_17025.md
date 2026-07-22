Q17025: Q64 or decimals mismatch in protected provider read path when the pool uses a 6/18 token pair and non-zero min-margin or confidence settings

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with public swaps that force the pool to read its price provider at a boundary quote while the pool uses a 6/18 token pair and non-zero min-margin or confidence settings, so that oracle 8-decimal values and Q64.64 pool prices stop agreeing under a reachable rounding or decimal edge case along `pool.swap -> ProtectedPriceProvider.getBidAndAskPrice -> oracle.price(feedId, pool) -> price guard and confidence shaping`, corrupting `MAX_TIME_DELTA`, price-guard bounds, confidence-shaped bid/ask, and the final quote used for settlement? This is the canonical non-anchored provider path for live swaps, so public timing against stale or wide-spread edges matters. Use standard tokens and public trade sizes that push the provider conversion right onto a truncation boundary.

Target
- File/function: smart-contracts-poc/contracts/ProtectedPriceProvider.sol::getBidAndAskPrice and _computeBidAsk
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: public swaps that force the pool to read its price provider at a boundary quote
- Exploit idea: Reach `pool.swap -> ProtectedPriceProvider.getBidAndAskPrice -> oracle.price(feedId, pool) -> price guard and confidence shaping` in a live public flow and show that use standard tokens and public trade sizes that push the provider conversion right onto a truncation boundary. The exact value at risk is `MAX_TIME_DELTA`, price-guard bounds, confidence-shaped bid/ask, and the final quote used for settlement.
- Invariant to test: Provider conversions between oracle decimals and Q64.64 must preserve order, monotonicity, and safe rounding. The concrete assertion should cover `MAX_TIME_DELTA`, price-guard bounds, confidence-shaped bid/ask, and the final quote used for settlement.
- Expected Immunefi impact: Medium/High loss-making swaps or LP value leakage above contest thresholds.
- Fast validation: Stress swaps around time, spread, and price-guard boundaries and assert every rejected quote fails closed before the pool can settle a trade.
