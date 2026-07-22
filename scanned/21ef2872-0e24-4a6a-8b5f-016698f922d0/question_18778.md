Q18778: rejection-boundary discontinuity in provider factory ownership and updater assumptions when the provider uses a synthetic ratio between two oracle feeds

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPoolFactory.sol::createPool and smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register` with swap direction and `priceLimitX64` choices around the current oracle mid while the provider uses a synthetic ratio between two oracle feeds, so that crossing a documented spread, staleness, or guard threshold yields a discontinuity the live swap path handles incorrectly along `factory-created provider -> later public swap or registration relies on provider ownership and approved-factory assumptions`, corrupting provider creator identity, approved factory assumptions, and whether live users can rely on the provider shape they were routed into? Trusted roles remain trusted, but any bug that lets a permissionless caller bind the wrong provider assumptions into a live pool is in scope. Trade exactly at the transition where the provider should switch from live quote to halt condition.

Target
- File/function: smart-contracts-poc/contracts/PriceProviderFactory.sol and smart-contracts-poc/contracts/AnchoredProviderFactory.sol provider ownership model
- Entrypoint: metric-core/contracts/MetricOmmPoolFactory.sol::createPool and smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Attacker controls: swap direction and `priceLimitX64` choices around the current oracle mid
- Exploit idea: Reach `factory-created provider -> later public swap or registration relies on provider ownership and approved-factory assumptions` in a live public flow and show that trade exactly at the transition where the provider should switch from live quote to halt condition. The exact value at risk is provider creator identity, approved factory assumptions, and whether live users can rely on the provider shape they were routed into.
- Invariant to test: Every rejection boundary must transition cleanly and fail closed under public access. The concrete assertion should cover provider creator identity, approved factory assumptions, and whether live users can rely on the provider shape they were routed into.
- Expected Immunefi impact: High bad-price execution or broken swap functionality at quote boundaries.
- Fast validation: Create pools around provider ownership and approved-factory boundaries and assert later public swaps always read the intended provider semantics.
