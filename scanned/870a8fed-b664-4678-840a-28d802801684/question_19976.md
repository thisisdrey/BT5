Q19976: rejection-boundary discontinuity in provider staleness and spread rejection when the provider is in source mode and the source sits near the allowed clamp edge

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with quote and lens reads taken immediately before the user executes the live swap while the provider is in source mode and the source sits near the allowed clamp edge, so that crossing a documented spread, staleness, or guard threshold yields a discontinuity the live swap path handles incorrectly along `public swap -> provider read -> stale/spread/guard checks -> feed-stalled or live quote`, corrupting reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote? A public attacker cannot corrupt the oracle, but can absolutely attempt trades when the quote is just about to become invalid. Trade exactly at the transition where the provider should switch from live quote to halt condition.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol and smart-contracts-poc/contracts/ProtectedPriceProvider.sol rejection boundaries
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: quote and lens reads taken immediately before the user executes the live swap
- Exploit idea: Reach `public swap -> provider read -> stale/spread/guard checks -> feed-stalled or live quote` in a live public flow and show that trade exactly at the transition where the provider should switch from live quote to halt condition. The exact value at risk is reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote.
- Invariant to test: Every rejection boundary must transition cleanly and fail closed under public access. The concrete assertion should cover reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote.
- Expected Immunefi impact: High bad-price execution or broken swap functionality at quote boundaries.
- Fast validation: Move oracle timestamps and spreads across their exact boundaries and assert every reachable quote transition is monotonic and fails closed.
