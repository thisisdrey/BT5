Q19401: Q64 or decimals mismatch in provider-backed state views when the provider is in reference mode with no custom source

Question
Can an unprivileged attacker enter through `metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol::quoteLiveExactIn` with public swaps that force the pool to read its price provider at a boundary quote while the provider is in reference mode with no custom source, so that oracle 8-decimal values and Q64.64 pool prices stop agreeing under a reachable rounding or decimal edge case along `public lens or quote -> provider-backed state read -> integrator or user executes live swap`, corrupting distance from provided price, quoted output, and any live execution that consumes those provider-derived values? Medium-severity provider bugs often show up here first because a public consumer trusts a view that diverges from the live swap path. Use standard tokens and public trade sizes that push the provider conversion right onto a truncation boundary.

Target
- File/function: metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol
- Entrypoint: metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol::quoteLiveExactIn
- Attacker controls: public swaps that force the pool to read its price provider at a boundary quote
- Exploit idea: Reach `public lens or quote -> provider-backed state read -> integrator or user executes live swap` in a live public flow and show that use standard tokens and public trade sizes that push the provider conversion right onto a truncation boundary. The exact value at risk is distance from provided price, quoted output, and any live execution that consumes those provider-derived values.
- Invariant to test: Provider conversions between oracle decimals and Q64.64 must preserve order, monotonicity, and safe rounding. The concrete assertion should cover distance from provided price, quoted output, and any live execution that consumes those provider-derived values.
- Expected Immunefi impact: Medium/High loss-making swaps or LP value leakage above contest thresholds.
- Fast validation: Compare provider-backed view outputs with the next live swap and flag any deterministic divergence that creates a reproducible loss-making execution.
