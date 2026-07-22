Q18217: Q64 or decimals mismatch in permissionless oracle registration when the provider uses a synthetic ratio between two oracle feeds

Question
Can an unprivileged attacker enter through `smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register` with public swaps that force the pool to read its price provider at a boundary quote while the provider uses a synthetic ratio between two oracle feeds, so that oracle 8-decimal values and Q64.64 pool prices stop agreeing under a reachable rounding or decimal edge case along `public register -> approved factory check -> isPool(pool) check -> registeredPool mapping update -> later provider price read`, corrupting the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool? Registration is intentionally permissionless, so the binding and blacklist semantics have to stay exact even under adversarial public usage. Use standard tokens and public trade sizes that push the provider conversion right onto a truncation boundary.

Target
- File/function: smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Entrypoint: smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Attacker controls: public swaps that force the pool to read its price provider at a boundary quote
- Exploit idea: Reach `public register -> approved factory check -> isPool(pool) check -> registeredPool mapping update -> later provider price read` in a live public flow and show that use standard tokens and public trade sizes that push the provider conversion right onto a truncation boundary. The exact value at risk is the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool.
- Invariant to test: Provider conversions between oracle decimals and Q64.64 must preserve order, monotonicity, and safe rounding. The concrete assertion should cover the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool.
- Expected Immunefi impact: Medium/High loss-making swaps or LP value leakage above contest thresholds.
- Fast validation: Attempt conflicting public registrations and assert only the intended `(feedId, pool)` pair becomes active and no unrelated pool read is re-enabled.
