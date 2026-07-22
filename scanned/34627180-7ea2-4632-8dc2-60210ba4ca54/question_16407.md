Q16407: band-escape clamp bug in anchored provider synthetic ratio when the provider is in reference mode with no custom source

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with mutable-provider pools that later rely on the same provider assumptions during live swaps while the provider is in reference mode with no custom source, so that the final quote becomes tighter than the documented anchor band under a reachable edge case along `pool.swap -> AnchoredPriceProvider two-feed read -> ratio computation -> band clamp -> swap math`, corrupting base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical? Synthetic ratio mode is valid production behavior, so any direction, division, or spread-composition bug here is squarely in scope. Reach a source or confidence boundary where the pre-clamp quote and final clamp stop enforcing the one-directional safety guarantee.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol::_readLeg and ratio-mode quote path
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: mutable-provider pools that later rely on the same provider assumptions during live swaps
- Exploit idea: Reach `pool.swap -> AnchoredPriceProvider two-feed read -> ratio computation -> band clamp -> swap math` in a live public flow and show that reach a source or confidence boundary where the pre-clamp quote and final clamp stop enforcing the one-directional safety guarantee. The exact value at risk is base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical.
- Invariant to test: The final provider quote must never be tighter than the allowed band around the trusted anchor. The concrete assertion should cover base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical.
- Expected Immunefi impact: Critical bad-price execution that lets public traders extract value beyond the permitted uncertainty envelope.
- Fast validation: Compare single-feed and two-feed provider variants under the same public swap assumptions and assert the ratio path preserves pair direction and bounded spread.
