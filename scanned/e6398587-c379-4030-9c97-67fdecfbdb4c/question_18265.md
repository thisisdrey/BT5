Q18265: registration-side effect bug in permissionless oracle registration when the pool uses a 6/18 token pair and non-zero min-margin or confidence settings

Question
Can an unprivileged attacker enter through `smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register` with public swaps that force the pool to read its price provider at a boundary quote while the pool uses a 6/18 token pair and non-zero min-margin or confidence settings, so that permissionless registration re-enables or binds more than the intended `(feedId, pool)` relation along `public register -> approved factory check -> isPool(pool) check -> registeredPool mapping update -> later provider price read`, corrupting the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool? Registration is intentionally permissionless, so the binding and blacklist semantics have to stay exact even under adversarial public usage. Use public registration calls to clear blacklist or activate a pool association that later price reads trust too broadly.

Target
- File/function: smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Entrypoint: smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Attacker controls: public swaps that force the pool to read its price provider at a boundary quote
- Exploit idea: Reach `public register -> approved factory check -> isPool(pool) check -> registeredPool mapping update -> later provider price read` in a live public flow and show that use public registration calls to clear blacklist or activate a pool association that later price reads trust too broadly. The exact value at risk is the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool.
- Invariant to test: Permissionless registration must only authorize the exact pool-feed relation paid for by the caller. The concrete assertion should cover the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool.
- Expected Immunefi impact: High if unauthorized pools or stale blacklist states can influence production reads.
- Fast validation: Attempt conflicting public registrations and assert only the intended `(feedId, pool)` pair becomes active and no unrelated pool read is re-enabled.
