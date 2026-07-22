Q17991: rejection-boundary discontinuity in provider token binding at creation when the pool uses a 6/18 token pair and non-zero min-margin or confidence settings

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPoolFactory.sol::createPool` with mutable-provider pools that later rely on the same provider assumptions during live swaps while the pool uses a 6/18 token pair and non-zero min-margin or confidence settings, so that crossing a documented spread, staleness, or guard threshold yields a discontinuity the live swap path handles incorrectly along `createPool -> validate token/provider pair -> deploy pool -> later pool.swap consumes provider quote`, corrupting provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction? A public pool creator chooses both tokens and provider, so pair-direction mismatches are only prevented by scoped validation. Trade exactly at the transition where the provider should switch from live quote to halt condition.

Target
- File/function: metric-core/contracts/MetricOmmPoolFactory.sol::createPool provider/token validation path
- Entrypoint: metric-core/contracts/MetricOmmPoolFactory.sol::createPool
- Attacker controls: mutable-provider pools that later rely on the same provider assumptions during live swaps
- Exploit idea: Reach `createPool -> validate token/provider pair -> deploy pool -> later pool.swap consumes provider quote` in a live public flow and show that trade exactly at the transition where the provider should switch from live quote to halt condition. The exact value at risk is provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction.
- Invariant to test: Every rejection boundary must transition cleanly and fail closed under public access. The concrete assertion should cover provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction.
- Expected Immunefi impact: High bad-price execution or broken swap functionality at quote boundaries.
- Fast validation: Create pools with providers that are almost but not exactly the same pair and assert the factory rejects every mismatch before deployment.
