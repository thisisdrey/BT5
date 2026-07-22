Q17609: band-escape clamp bug in provider token binding at creation when the provider is in source mode and the source sits near the allowed clamp edge

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPoolFactory.sol::createPool` with public swaps that force the pool to read its price provider at a boundary quote while the provider is in source mode and the source sits near the allowed clamp edge, so that the final quote becomes tighter than the documented anchor band under a reachable edge case along `createPool -> validate token/provider pair -> deploy pool -> later pool.swap consumes provider quote`, corrupting provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction? A public pool creator chooses both tokens and provider, so pair-direction mismatches are only prevented by scoped validation. Reach a source or confidence boundary where the pre-clamp quote and final clamp stop enforcing the one-directional safety guarantee.

Target
- File/function: metric-core/contracts/MetricOmmPoolFactory.sol::createPool provider/token validation path
- Entrypoint: metric-core/contracts/MetricOmmPoolFactory.sol::createPool
- Attacker controls: public swaps that force the pool to read its price provider at a boundary quote
- Exploit idea: Reach `createPool -> validate token/provider pair -> deploy pool -> later pool.swap consumes provider quote` in a live public flow and show that reach a source or confidence boundary where the pre-clamp quote and final clamp stop enforcing the one-directional safety guarantee. The exact value at risk is provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction.
- Invariant to test: The final provider quote must never be tighter than the allowed band around the trusted anchor. The concrete assertion should cover provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction.
- Expected Immunefi impact: Critical bad-price execution that lets public traders extract value beyond the permitted uncertainty envelope.
- Fast validation: Create pools with providers that are almost but not exactly the same pair and assert the factory rejects every mismatch before deployment.
