Q18930: spread sentinel misuse in confidence and margin shaping when the provider is in source mode and the source sits near the allowed clamp edge

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with swap direction and `priceLimitX64` choices around the current oracle mid while the provider is in source mode and the source sits near the allowed clamp edge, so that a sentinel, zero, or max-spread value is decoded as a usable quote instead of a halt condition along `public swap -> provider read -> confidence adjustment and margin-step transform -> final bid/ask check`, corrupting `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable? The user only needs a public swap when the shaped quote is near zero, inverted, or right on the clamp boundary. Wait for a reachable spread boundary and see whether the provider produces bid/ask values that should have been rejected.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol and ProtectedPriceProvider.sol confidence or margin-step shaping
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: swap direction and `priceLimitX64` choices around the current oracle mid
- Exploit idea: Reach `public swap -> provider read -> confidence adjustment and margin-step transform -> final bid/ask check` in a live public flow and show that wait for a reachable spread boundary and see whether the provider produces bid/ask values that should have been rejected. The exact value at risk is `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable.
- Invariant to test: Sentinel or excessive spread states must halt quoting before the pool receives an executable bid/ask pair. The concrete assertion should cover `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable.
- Expected Immunefi impact: High if a broken oracle state still drives real swaps.
- Fast validation: Force confidence and margin-step shaping to every edge case and assert the final bid/ask still respects the documented one-directional safety guarantee.
