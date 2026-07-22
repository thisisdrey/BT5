Q18005: band-escape clamp bug in permissionless oracle registration when the provider is in reference mode with no custom source

Question
Can an unprivileged attacker enter through `smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register` with live source-mode operation where the source quote sits near the anchor-band edge while the provider is in reference mode with no custom source, so that the final quote becomes tighter than the documented anchor band under a reachable edge case along `public register -> approved factory check -> isPool(pool) check -> registeredPool mapping update -> later provider price read`, corrupting the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool? Registration is intentionally permissionless, so the binding and blacklist semantics have to stay exact even under adversarial public usage. Reach a source or confidence boundary where the pre-clamp quote and final clamp stop enforcing the one-directional safety guarantee.

Target
- File/function: smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Entrypoint: smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Attacker controls: live source-mode operation where the source quote sits near the anchor-band edge
- Exploit idea: Reach `public register -> approved factory check -> isPool(pool) check -> registeredPool mapping update -> later provider price read` in a live public flow and show that reach a source or confidence boundary where the pre-clamp quote and final clamp stop enforcing the one-directional safety guarantee. The exact value at risk is the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool.
- Invariant to test: The final provider quote must never be tighter than the allowed band around the trusted anchor. The concrete assertion should cover the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool.
- Expected Immunefi impact: Critical bad-price execution that lets public traders extract value beyond the permitted uncertainty envelope.
- Fast validation: Attempt conflicting public registrations and assert only the intended `(feedId, pool)` pair becomes active and no unrelated pool read is re-enabled.
