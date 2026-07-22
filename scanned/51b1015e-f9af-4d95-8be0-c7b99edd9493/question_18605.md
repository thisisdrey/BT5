Q18605: Q64 or decimals mismatch in provider factory ownership and updater assumptions when the provider is in reference mode with no custom source

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPoolFactory.sol::createPool and smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register` with live source-mode operation where the source quote sits near the anchor-band edge while the provider is in reference mode with no custom source, so that oracle 8-decimal values and Q64.64 pool prices stop agreeing under a reachable rounding or decimal edge case along `factory-created provider -> later public swap or registration relies on provider ownership and approved-factory assumptions`, corrupting provider creator identity, approved factory assumptions, and whether live users can rely on the provider shape they were routed into? Trusted roles remain trusted, but any bug that lets a permissionless caller bind the wrong provider assumptions into a live pool is in scope. Use standard tokens and public trade sizes that push the provider conversion right onto a truncation boundary.

Target
- File/function: smart-contracts-poc/contracts/PriceProviderFactory.sol and smart-contracts-poc/contracts/AnchoredProviderFactory.sol provider ownership model
- Entrypoint: metric-core/contracts/MetricOmmPoolFactory.sol::createPool and smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Attacker controls: live source-mode operation where the source quote sits near the anchor-band edge
- Exploit idea: Reach `factory-created provider -> later public swap or registration relies on provider ownership and approved-factory assumptions` in a live public flow and show that use standard tokens and public trade sizes that push the provider conversion right onto a truncation boundary. The exact value at risk is provider creator identity, approved factory assumptions, and whether live users can rely on the provider shape they were routed into.
- Invariant to test: Provider conversions between oracle decimals and Q64.64 must preserve order, monotonicity, and safe rounding. The concrete assertion should cover provider creator identity, approved factory assumptions, and whether live users can rely on the provider shape they were routed into.
- Expected Immunefi impact: Medium/High loss-making swaps or LP value leakage above contest thresholds.
- Fast validation: Create pools around provider ownership and approved-factory boundaries and assert later public swaps always read the intended provider semantics.
