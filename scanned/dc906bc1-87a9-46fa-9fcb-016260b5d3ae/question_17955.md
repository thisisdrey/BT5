Q17955: provider-backed view divergence in provider token binding at creation when the oracle mid is valid but its spread or staleness sits close to the rejection boundary

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPoolFactory.sol::createPool` with synthetic two-feed provider configurations created through the scoped factory path while the oracle mid is valid but its spread or staleness sits close to the rejection boundary, so that provider-backed lens or quoter output diverges deterministically from the live provider path used by the swap along `createPool -> validate token/provider pair -> deploy pool -> later pool.swap consumes provider quote`, corrupting provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction? A public pool creator chooses both tokens and provider, so pair-direction mismatches are only prevented by scoped validation. Consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch.

Target
- File/function: metric-core/contracts/MetricOmmPoolFactory.sol::createPool provider/token validation path
- Entrypoint: metric-core/contracts/MetricOmmPoolFactory.sol::createPool
- Attacker controls: synthetic two-feed provider configurations created through the scoped factory path
- Exploit idea: Reach `createPool -> validate token/provider pair -> deploy pool -> later pool.swap consumes provider quote` in a live public flow and show that consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch. The exact value at risk is provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction.
- Invariant to test: Provider-backed user or integrator views must not diverge from the live swap semantics enough to cause deterministic losses. The concrete assertion should cover provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction.
- Expected Immunefi impact: Medium predictable loss-making execution from trusted views or quotes.
- Fast validation: Create pools with providers that are almost but not exactly the same pair and assert the factory rejects every mismatch before deployment.
