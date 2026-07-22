Q17058: registration-side effect bug in protected provider read path when the provider uses a synthetic ratio between two oracle feeds

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with swap direction and `priceLimitX64` choices around the current oracle mid while the provider uses a synthetic ratio between two oracle feeds, so that permissionless registration re-enables or binds more than the intended `(feedId, pool)` relation along `pool.swap -> ProtectedPriceProvider.getBidAndAskPrice -> oracle.price(feedId, pool) -> price guard and confidence shaping`, corrupting `MAX_TIME_DELTA`, price-guard bounds, confidence-shaped bid/ask, and the final quote used for settlement? This is the canonical non-anchored provider path for live swaps, so public timing against stale or wide-spread edges matters. Use public registration calls to clear blacklist or activate a pool association that later price reads trust too broadly.

Target
- File/function: smart-contracts-poc/contracts/ProtectedPriceProvider.sol::getBidAndAskPrice and _computeBidAsk
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: swap direction and `priceLimitX64` choices around the current oracle mid
- Exploit idea: Reach `pool.swap -> ProtectedPriceProvider.getBidAndAskPrice -> oracle.price(feedId, pool) -> price guard and confidence shaping` in a live public flow and show that use public registration calls to clear blacklist or activate a pool association that later price reads trust too broadly. The exact value at risk is `MAX_TIME_DELTA`, price-guard bounds, confidence-shaped bid/ask, and the final quote used for settlement.
- Invariant to test: Permissionless registration must only authorize the exact pool-feed relation paid for by the caller. The concrete assertion should cover `MAX_TIME_DELTA`, price-guard bounds, confidence-shaped bid/ask, and the final quote used for settlement.
- Expected Immunefi impact: High if unauthorized pools or stale blacklist states can influence production reads.
- Fast validation: Stress swaps around time, spread, and price-guard boundaries and assert every rejected quote fails closed before the pool can settle a trade.
