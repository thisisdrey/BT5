Q18058: synthetic ratio direction error in permissionless oracle registration when the provider uses a synthetic ratio between two oracle feeds

Question
Can an unprivileged attacker enter through `smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register` with swap direction and `priceLimitX64` choices around the current oracle mid while the provider uses a synthetic ratio between two oracle feeds, so that two-feed ratio mode uses the right feeds but the wrong direction, spread composition, or rounding convention along `public register -> approved factory check -> isPool(pool) check -> registeredPool mapping update -> later provider price read`, corrupting the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool? Registration is intentionally permissionless, so the binding and blacklist semantics have to stay exact even under adversarial public usage. Trigger a public swap through a synthetically priced pool and see whether the ratio quote moves opposite to the intended pair orientation.

Target
- File/function: smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Entrypoint: smart-contracts-poc/contracts/oracles/providers/OracleBase.sol::register
- Attacker controls: swap direction and `priceLimitX64` choices around the current oracle mid
- Exploit idea: Reach `public register -> approved factory check -> isPool(pool) check -> registeredPool mapping update -> later provider price read` in a live public flow and show that trigger a public swap through a synthetically priced pool and see whether the ratio quote moves opposite to the intended pair orientation. The exact value at risk is the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool.
- Invariant to test: Synthetic provider mode must preserve pair direction and bounded spread exactly as documented. The concrete assertion should cover the `(feedId, pool)` registration binding, blacklist-clearing side effects, and whether later reads attribute to the intended pool.
- Expected Immunefi impact: Critical direct loss if pools trade against an inverted or materially wrong synthetic quote.
- Fast validation: Attempt conflicting public registrations and assert only the intended `(feedId, pool)` pair becomes active and no unrelated pool read is re-enabled.
