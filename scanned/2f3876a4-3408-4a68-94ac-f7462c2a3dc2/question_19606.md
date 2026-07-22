Q19606: band-escape clamp bug in provider staleness and spread rejection when the provider is in reference mode with no custom source

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with mixed-decimal token pairs whose provider converts between 8-decimal oracle values and Q64.64 prices while the provider is in reference mode with no custom source, so that the final quote becomes tighter than the documented anchor band under a reachable edge case along `public swap -> provider read -> stale/spread/guard checks -> feed-stalled or live quote`, corrupting reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote? A public attacker cannot corrupt the oracle, but can absolutely attempt trades when the quote is just about to become invalid. Reach a source or confidence boundary where the pre-clamp quote and final clamp stop enforcing the one-directional safety guarantee.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol and smart-contracts-poc/contracts/ProtectedPriceProvider.sol rejection boundaries
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: mixed-decimal token pairs whose provider converts between 8-decimal oracle values and Q64.64 prices
- Exploit idea: Reach `public swap -> provider read -> stale/spread/guard checks -> feed-stalled or live quote` in a live public flow and show that reach a source or confidence boundary where the pre-clamp quote and final clamp stop enforcing the one-directional safety guarantee. The exact value at risk is reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote.
- Invariant to test: The final provider quote must never be tighter than the allowed band around the trusted anchor. The concrete assertion should cover reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote.
- Expected Immunefi impact: Critical bad-price execution that lets public traders extract value beyond the permitted uncertainty envelope.
- Fast validation: Move oracle timestamps and spreads across their exact boundaries and assert every reachable quote transition is monotonic and fails closed.
