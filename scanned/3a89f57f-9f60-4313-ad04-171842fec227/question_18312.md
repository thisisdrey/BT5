Q18312: confidence-shaping inversion in permissionless oracle registration when the pool uses a 6/18 token pair and non-zero min-margin or confidence settings

Question
Can an unprivileged attacker enter through `smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register` with quote and lens reads taken immediately before the user executes the live swap while the pool uses a 6/18 token pair and non-zero min-margin or confidence settings, so that confidence or margin shaping yields zero, inverted, or otherwise malformed quotes that still appear executable along `public register -> approved factory check -> isPool(pool) check -> registeredPool mapping update -> later provider price read`, corrupting the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool? Registration is intentionally permissionless, so the binding and blacklist semantics have to stay exact even under adversarial public usage. Trade when shaping pushes bid and ask right onto the inversion boundary and see whether the provider still returns them as live.

Target
- File/function: smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Entrypoint: smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Attacker controls: quote and lens reads taken immediately before the user executes the live swap
- Exploit idea: Reach `public register -> approved factory check -> isPool(pool) check -> registeredPool mapping update -> later provider price read` in a live public flow and show that trade when shaping pushes bid and ask right onto the inversion boundary and see whether the provider still returns them as live. The exact value at risk is the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool.
- Invariant to test: Bid must stay positive and strictly below ask after every shaping and clamp step. The concrete assertion should cover the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool.
- Expected Immunefi impact: High direct loss from malformed but accepted quotes reaching swaps.
- Fast validation: Attempt conflicting public registrations and assert only the intended `(feedId, pool)` pair becomes active and no unrelated pool read is re-enabled.
