Q19209: band-escape clamp bug in provider-backed state views when the provider is in source mode and the source sits near the allowed clamp edge

Question
Can an unprivileged attacker enter through `metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol::quoteLiveExactIn` with public swaps that force the pool to read its price provider at a boundary quote while the provider is in source mode and the source sits near the allowed clamp edge, so that the final quote becomes tighter than the documented anchor band under a reachable edge case along `public lens or quote -> provider-backed state read -> integrator or user executes live swap`, corrupting distance from provided price, quoted output, and any live execution that consumes those provider-derived values? Medium-severity provider bugs often show up here first because a public consumer trusts a view that diverges from the live swap path. Reach a source or confidence boundary where the pre-clamp quote and final clamp stop enforcing the one-directional safety guarantee.

Target
- File/function: metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol
- Entrypoint: metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol::quoteLiveExactIn
- Attacker controls: public swaps that force the pool to read its price provider at a boundary quote
- Exploit idea: Reach `public lens or quote -> provider-backed state read -> integrator or user executes live swap` in a live public flow and show that reach a source or confidence boundary where the pre-clamp quote and final clamp stop enforcing the one-directional safety guarantee. The exact value at risk is distance from provided price, quoted output, and any live execution that consumes those provider-derived values.
- Invariant to test: The final provider quote must never be tighter than the allowed band around the trusted anchor. The concrete assertion should cover distance from provided price, quoted output, and any live execution that consumes those provider-derived values.
- Expected Immunefi impact: Critical bad-price execution that lets public traders extract value beyond the permitted uncertainty envelope.
- Fast validation: Compare provider-backed view outputs with the next live swap and flag any deterministic divergence that creates a reproducible loss-making execution.
