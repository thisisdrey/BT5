Q19067: registration-side effect bug in confidence and margin shaping when the pool uses a 6/18 token pair and non-zero min-margin or confidence settings

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with synthetic two-feed provider configurations created through the scoped factory path while the pool uses a 6/18 token pair and non-zero min-margin or confidence settings, so that permissionless registration re-enables or binds more than the intended `(feedId, pool)` relation along `public swap -> provider read -> confidence adjustment and margin-step transform -> final bid/ask check`, corrupting `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable? The user only needs a public swap when the shaped quote is near zero, inverted, or right on the clamp boundary. Use public registration calls to clear blacklist or activate a pool association that later price reads trust too broadly.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol and ProtectedPriceProvider.sol confidence or margin-step shaping
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: synthetic two-feed provider configurations created through the scoped factory path
- Exploit idea: Reach `public swap -> provider read -> confidence adjustment and margin-step transform -> final bid/ask check` in a live public flow and show that use public registration calls to clear blacklist or activate a pool association that later price reads trust too broadly. The exact value at risk is `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable.
- Invariant to test: Permissionless registration must only authorize the exact pool-feed relation paid for by the caller. The concrete assertion should cover `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable.
- Expected Immunefi impact: High if unauthorized pools or stale blacklist states can influence production reads.
- Fast validation: Force confidence and margin-step shaping to every edge case and assert the final bid/ask still respects the documented one-directional safety guarantee.
