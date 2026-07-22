Q18335: provider-backed view divergence in permissionless oracle registration when the provider is in source mode and the source sits near the allowed clamp edge

Question
Can an unprivileged attacker enter through `smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register` with mutable-provider pools that later rely on the same provider assumptions during live swaps while the provider is in source mode and the source sits near the allowed clamp edge, so that provider-backed lens or quoter output diverges deterministically from the live provider path used by the swap along `public register -> approved factory check -> isPool(pool) check -> registeredPool mapping update -> later provider price read`, corrupting the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool? Registration is intentionally permissionless, so the binding and blacklist semantics have to stay exact even under adversarial public usage. Consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch.

Target
- File/function: smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Entrypoint: smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Attacker controls: mutable-provider pools that later rely on the same provider assumptions during live swaps
- Exploit idea: Reach `public register -> approved factory check -> isPool(pool) check -> registeredPool mapping update -> later provider price read` in a live public flow and show that consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch. The exact value at risk is the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool.
- Invariant to test: Provider-backed user or integrator views must not diverge from the live swap semantics enough to cause deterministic losses. The concrete assertion should cover the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool.
- Expected Immunefi impact: Medium predictable loss-making execution from trusted views or quotes.
- Fast validation: Attempt conflicting public registrations and assert only the intended `(feedId, pool)` pair becomes active and no unrelated pool read is re-enabled.
