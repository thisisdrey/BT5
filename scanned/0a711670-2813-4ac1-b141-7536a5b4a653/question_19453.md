Q19453: registration-side effect bug in provider-backed state views when the provider is in source mode and the source sits near the allowed clamp edge

Question
Can an unprivileged attacker enter through `metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol::quoteLiveExactIn` with live source-mode operation where the source quote sits near the anchor-band edge while the provider is in source mode and the source sits near the allowed clamp edge, so that permissionless registration re-enables or binds more than the intended `(feedId, pool)` relation along `public lens or quote -> provider-backed state read -> integrator or user executes live swap`, corrupting distance from provided price, quoted output, and any live execution that consumes those provider-derived values? Medium-severity provider bugs often show up here first because a public consumer trusts a view that diverges from the live swap path. Use public registration calls to clear blacklist or activate a pool association that later price reads trust too broadly.

Target
- File/function: metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol
- Entrypoint: metric-periphery/contracts/lens/MetricOmmPoolDataProvider.sol::distanceFromProvidedPriceX64 and metric-periphery/contracts/lens/MetricOmmSwapQuoter.sol::quoteLiveExactIn
- Attacker controls: live source-mode operation where the source quote sits near the anchor-band edge
- Exploit idea: Reach `public lens or quote -> provider-backed state read -> integrator or user executes live swap` in a live public flow and show that use public registration calls to clear blacklist or activate a pool association that later price reads trust too broadly. The exact value at risk is distance from provided price, quoted output, and any live execution that consumes those provider-derived values.
- Invariant to test: Permissionless registration must only authorize the exact pool-feed relation paid for by the caller. The concrete assertion should cover distance from provided price, quoted output, and any live execution that consumes those provider-derived values.
- Expected Immunefi impact: High if unauthorized pools or stale blacklist states can influence production reads.
- Fast validation: Compare provider-backed view outputs with the next live swap and flag any deterministic divergence that creates a reproducible loss-making execution.
