Q18516: staleness fail-open in provider factory ownership and updater assumptions when the oracle mid is valid but its spread or staleness sits close to the rejection boundary

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPoolFactory.sol::createPool and smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register` with provider registration parameters passed through the public oracle registration path while the oracle mid is valid but its spread or staleness sits close to the rejection boundary, so that a quote that should be stale or sequencer-invalid is still admitted to a live swap along `factory-created provider -> later public swap or registration relies on provider ownership and approved-factory assumptions`, corrupting provider creator identity, approved factory assumptions, and whether live users can rely on the provider shape they were routed into? Trusted roles remain trusted, but any bug that lets a permissionless caller bind the wrong provider assumptions into a live pool is in scope. Trade exactly at the freshness boundary and see whether the provider returns a valid-looking quote instead of failing closed.

Target
- File/function: smart-contracts-poc/contracts/PriceProviderFactory.sol and smart-contracts-poc/contracts/AnchoredProviderFactory.sol provider ownership model
- Entrypoint: metric-core/contracts/MetricOmmPoolFactory.sol::createPool and smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Attacker controls: provider registration parameters passed through the public oracle registration path
- Exploit idea: Reach `factory-created provider -> later public swap or registration relies on provider ownership and approved-factory assumptions` in a live public flow and show that trade exactly at the freshness boundary and see whether the provider returns a valid-looking quote instead of failing closed. The exact value at risk is provider creator identity, approved factory assumptions, and whether live users can rely on the provider shape they were routed into.
- Invariant to test: No stale, future, or sequencer-invalid quote may reach pool swap math. The concrete assertion should cover provider creator identity, approved factory assumptions, and whether live users can rely on the provider shape they were routed into.
- Expected Immunefi impact: High bad-price execution on production swaps.
- Fast validation: Create pools around provider ownership and approved-factory boundaries and assert later public swaps always read the intended provider semantics.
