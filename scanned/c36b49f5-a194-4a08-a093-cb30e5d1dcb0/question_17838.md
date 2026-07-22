Q17838: Q64 or decimals mismatch in provider token binding at creation when the oracle mid is valid but its spread or staleness sits close to the rejection boundary

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPoolFactory.sol::createPool` with mixed-decimal token pairs whose provider converts between 8-decimal oracle values and Q64.64 prices while the oracle mid is valid but its spread or staleness sits close to the rejection boundary, so that oracle 8-decimal values and Q64.64 pool prices stop agreeing under a reachable rounding or decimal edge case along `createPool -> validate token/provider pair -> deploy pool -> later pool.swap consumes provider quote`, corrupting provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction? A public pool creator chooses both tokens and provider, so pair-direction mismatches are only prevented by scoped validation. Use standard tokens and public trade sizes that push the provider conversion right onto a truncation boundary.

Target
- File/function: metric-core/contracts/MetricOmmPoolFactory.sol::createPool provider/token validation path
- Entrypoint: metric-core/contracts/MetricOmmPoolFactory.sol::createPool
- Attacker controls: mixed-decimal token pairs whose provider converts between 8-decimal oracle values and Q64.64 prices
- Exploit idea: Reach `createPool -> validate token/provider pair -> deploy pool -> later pool.swap consumes provider quote` in a live public flow and show that use standard tokens and public trade sizes that push the provider conversion right onto a truncation boundary. The exact value at risk is provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction.
- Invariant to test: Provider conversions between oracle decimals and Q64.64 must preserve order, monotonicity, and safe rounding. The concrete assertion should cover provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction.
- Expected Immunefi impact: Medium/High loss-making swaps or LP value leakage above contest thresholds.
- Fast validation: Create pools with providers that are almost but not exactly the same pair and assert the factory rejects every mismatch before deployment.
