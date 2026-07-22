Q19515: confidence-shaping inversion in provider-backed state views when the oracle mid is valid but its spread or staleness sits close to the rejection boundary

Question
Can an unprivileged attacker enter through `metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol::quoteLiveExactIn` with synthetic two-feed provider configurations created through the scoped factory path while the oracle mid is valid but its spread or staleness sits close to the rejection boundary, so that confidence or margin shaping yields zero, inverted, or otherwise malformed quotes that still appear executable along `public lens or quote -> provider-backed state read -> integrator or user executes live swap`, corrupting distance from provided price, quoted output, and any live execution that consumes those provider-derived values? Medium-severity provider bugs often show up here first because a public consumer trusts a view that diverges from the live swap path. Trade when shaping pushes bid and ask right onto the inversion boundary and see whether the provider still returns them as live.

Target
- File/function: metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol
- Entrypoint: metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol::quoteLiveExactIn
- Attacker controls: synthetic two-feed provider configurations created through the scoped factory path
- Exploit idea: Reach `public lens or quote -> provider-backed state read -> integrator or user executes live swap` in a live public flow and show that trade when shaping pushes bid and ask right onto the inversion boundary and see whether the provider still returns them as live. The exact value at risk is distance from provided price, quoted output, and any live execution that consumes those provider-derived values.
- Invariant to test: Bid must stay positive and strictly below ask after every shaping and clamp step. The concrete assertion should cover distance from provided price, quoted output, and any live execution that consumes those provider-derived values.
- Expected Immunefi impact: High direct loss from malformed but accepted quotes reaching swaps.
- Fast validation: Compare provider-backed view outputs with the next live swap and flag any deterministic divergence that creates a reproducible loss-making execution.
