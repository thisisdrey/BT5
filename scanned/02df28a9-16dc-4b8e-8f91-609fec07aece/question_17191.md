Q17191: rejection-boundary discontinuity in protected provider read path when the pool uses a 6/18 token pair and non-zero min-margin or confidence settings

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with mutable-provider pools that later rely on the same provider assumptions during live swaps while the pool uses a 6/18 token pair and non-zero min-margin or confidence settings, so that crossing a documented spread, staleness, or guard threshold yields a discontinuity the live swap path handles incorrectly along `pool.swap -> ProtectedPriceProvider.getBidAndAskPrice -> oracle.price(feedId, pool) -> price guard and confidence shaping`, corrupting `MAX_TIME_DELTA`, price-guard bounds, confidence-shaped bid/ask, and the final quote used for settlement? This is the canonical non-anchored provider path for live swaps, so public timing against stale or wide-spread edges matters. Trade exactly at the transition where the provider should switch from live quote to halt condition.

Target
- File/function: smart-contracts-poc/contracts/ProtectedPriceProvider.sol::getBidAndAskPrice and _computeBidAsk
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: mutable-provider pools that later rely on the same provider assumptions during live swaps
- Exploit idea: Reach `pool.swap -> ProtectedPriceProvider.getBidAndAskPrice -> oracle.price(feedId, pool) -> price guard and confidence shaping` in a live public flow and show that trade exactly at the transition where the provider should switch from live quote to halt condition. The exact value at risk is `MAX_TIME_DELTA`, price-guard bounds, confidence-shaped bid/ask, and the final quote used for settlement.
- Invariant to test: Every rejection boundary must transition cleanly and fail closed under public access. The concrete assertion should cover `MAX_TIME_DELTA`, price-guard bounds, confidence-shaped bid/ask, and the final quote used for settlement.
- Expected Immunefi impact: High bad-price execution or broken swap functionality at quote boundaries.
- Fast validation: Stress swaps around time, spread, and price-guard boundaries and assert every rejected quote fails closed before the pool can settle a trade.
