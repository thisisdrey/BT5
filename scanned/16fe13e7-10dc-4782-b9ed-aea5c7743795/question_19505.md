Q19505: confidence-shaping inversion in provider-backed state views when the pool uses a 6/18 token pair and non-zero min-margin or confidence settings

Question
Can an unprivileged attacker enter through `metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol::quoteLiveExactIn` with public swaps that force the pool to read its price provider at a boundary quote while the pool uses a 6/18 token pair and non-zero min-margin or confidence settings, so that confidence or margin shaping yields zero, inverted, or otherwise malformed quotes that still appear executable along `public lens or quote -> provider-backed state read -> integrator or user executes live swap`, corrupting distance from provided price, quoted output, and any live execution that consumes those provider-derived values? Medium-severity provider bugs often show up here first because a public consumer trusts a view that diverges from the live swap path. Trade when shaping pushes bid and ask right onto the inversion boundary and see whether the provider still returns them as live.

Target
- File/function: metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol
- Entrypoint: metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol::quoteLiveExactIn
- Attacker controls: public swaps that force the pool to read its price provider at a boundary quote
- Exploit idea: Reach `public lens or quote -> provider-backed state read -> integrator or user executes live swap` in a live public flow and show that trade when shaping pushes bid and ask right onto the inversion boundary and see whether the provider still returns them as live. The exact value at risk is distance from provided price, quoted output, and any live execution that consumes those provider-derived values.
- Invariant to test: Bid must stay positive and strictly below ask after every shaping and clamp step. The concrete assertion should cover distance from provided price, quoted output, and any live execution that consumes those provider-derived values.
- Expected Immunefi impact: High direct loss from malformed but accepted quotes reaching swaps.
- Fast validation: Compare provider-backed view outputs with the next live swap and flag any deterministic divergence that creates a reproducible loss-making execution.
