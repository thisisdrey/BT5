Q18521: spread sentinel misuse in provider factory ownership and updater assumptions when the provider is in reference mode with no custom source

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPoolFactory.sol::createPool and smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register` with public swaps that force the pool to read its price provider at a boundary quote while the provider is in reference mode with no custom source, so that a sentinel, zero, or max-spread value is decoded as a usable quote instead of a halt condition along `factory-created provider -> later public swap or registration relies on provider ownership and approved-factory assumptions`, corrupting provider creator identity, approved factory assumptions, and whether live users can rely on the provider shape they were routed into? Trusted roles remain trusted, but any bug that lets a permissionless caller bind the wrong provider assumptions into a live pool is in scope. Wait for a reachable spread boundary and see whether the provider produces bid/ask values that should have been rejected.

Target
- File/function: smart-contracts-poc/contracts/PriceProviderFactory.sol and smart-contracts-poc/contracts/AnchoredProviderFactory.sol provider ownership model
- Entrypoint: metric-core/contracts/MetricOmmPoolFactory.sol::createPool and smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Attacker controls: public swaps that force the pool to read its price provider at a boundary quote
- Exploit idea: Reach `factory-created provider -> later public swap or registration relies on provider ownership and approved-factory assumptions` in a live public flow and show that wait for a reachable spread boundary and see whether the provider produces bid/ask values that should have been rejected. The exact value at risk is provider creator identity, approved factory assumptions, and whether live users can rely on the provider shape they were routed into.
- Invariant to test: Sentinel or excessive spread states must halt quoting before the pool receives an executable bid/ask pair. The concrete assertion should cover provider creator identity, approved factory assumptions, and whether live users can rely on the provider shape they were routed into.
- Expected Immunefi impact: High if a broken oracle state still drives real swaps.
- Fast validation: Create pools around provider ownership and approved-factory boundaries and assert later public swaps always read the intended provider semantics.
