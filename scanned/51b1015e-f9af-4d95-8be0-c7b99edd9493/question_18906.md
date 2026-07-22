Q18906: staleness fail-open in confidence and margin shaping when the pool uses a 6/18 token pair and non-zero min-margin or confidence settings

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with swap direction and `priceLimitX64` choices around the current oracle mid while the pool uses a 6/18 token pair and non-zero min-margin or confidence settings, so that a quote that should be stale or sequencer-invalid is still admitted to a live swap along `public swap -> provider read -> confidence adjustment and margin-step transform -> final bid/ask check`, corrupting `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable? The user only needs a public swap when the shaped quote is near zero, inverted, or right on the clamp boundary. Trade exactly at the freshness boundary and see whether the provider returns a valid-looking quote instead of failing closed.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol and ProtectedPriceProvider.sol confidence or margin-step shaping
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: swap direction and `priceLimitX64` choices around the current oracle mid
- Exploit idea: Reach `public swap -> provider read -> confidence adjustment and margin-step transform -> final bid/ask check` in a live public flow and show that trade exactly at the freshness boundary and see whether the provider returns a valid-looking quote instead of failing closed. The exact value at risk is `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable.
- Invariant to test: No stale, future, or sequencer-invalid quote may reach pool swap math. The concrete assertion should cover `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable.
- Expected Immunefi impact: High bad-price execution on production swaps.
- Fast validation: Force confidence and margin-step shaping to every edge case and assert the final bid/ask still respects the documented one-directional safety guarantee.
