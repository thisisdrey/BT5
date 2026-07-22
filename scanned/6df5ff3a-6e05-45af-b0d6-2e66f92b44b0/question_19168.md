Q19168: rejection-boundary discontinuity in confidence and margin shaping when the provider is in reference mode with no custom source

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with quote and lens reads taken immediately before the user executes the live swap while the provider is in reference mode with no custom source, so that crossing a documented spread, staleness, or guard threshold yields a discontinuity the live swap path handles incorrectly along `public swap -> provider read -> confidence adjustment and margin-step transform -> final bid/ask check`, corrupting `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable? The user only needs a public swap when the shaped quote is near zero, inverted, or right on the clamp boundary. Trade exactly at the transition where the provider should switch from live quote to halt condition.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol and ProtectedPriceProvider.sol confidence or margin-step shaping
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: quote and lens reads taken immediately before the user executes the live swap
- Exploit idea: Reach `public swap -> provider read -> confidence adjustment and margin-step transform -> final bid/ask check` in a live public flow and show that trade exactly at the transition where the provider should switch from live quote to halt condition. The exact value at risk is `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable.
- Invariant to test: Every rejection boundary must transition cleanly and fail closed under public access. The concrete assertion should cover `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable.
- Expected Immunefi impact: High bad-price execution or broken swap functionality at quote boundaries.
- Fast validation: Force confidence and margin-step shaping to every edge case and assert the final bid/ask still respects the documented one-directional safety guarantee.
