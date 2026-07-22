Q17653: synthetic ratio direction error in provider token binding at creation when the provider is in source mode and the source sits near the allowed clamp edge

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPoolFactory.sol::createPool` with live source-mode operation where the source quote sits near the anchor-band edge while the provider is in source mode and the source sits near the allowed clamp edge, so that two-feed ratio mode uses the right feeds but the wrong direction, spread composition, or rounding convention along `createPool -> validate token/provider pair -> deploy pool -> later pool.swap consumes provider quote`, corrupting provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction? A public pool creator chooses both tokens and provider, so pair-direction mismatches are only prevented by scoped validation. Trigger a public swap through a synthetically priced pool and see whether the ratio quote moves opposite to the intended pair orientation.

Target
- File/function: metric-core/contracts/MetricOmmPoolFactory.sol::createPool provider/token validation path
- Entrypoint: metric-core/contracts/MetricOmmPoolFactory.sol::createPool
- Attacker controls: live source-mode operation where the source quote sits near the anchor-band edge
- Exploit idea: Reach `createPool -> validate token/provider pair -> deploy pool -> later pool.swap consumes provider quote` in a live public flow and show that trigger a public swap through a synthetically priced pool and see whether the ratio quote moves opposite to the intended pair orientation. The exact value at risk is provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction.
- Invariant to test: Synthetic provider mode must preserve pair direction and bounded spread exactly as documented. The concrete assertion should cover provider token0/token1, pool token0/token1, and whether the deployed pool later reads the right pair direction.
- Expected Immunefi impact: Critical direct loss if pools trade against an inverted or materially wrong synthetic quote.
- Fast validation: Create pools with providers that are almost but not exactly the same pair and assert the factory rejects every mismatch before deployment.
