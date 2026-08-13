# Q729: check_utilization_ratio: cross-mode collateral view mismatch [a-borrow-with-dust-sized] [cache-order]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a borrow with dust-sized assets/liabilities affecting utilization rounding so `check_utilization_ratio` evaluates account risk under one mode and settles value under another, violating `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and resulting in `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow with dust-sized assets/liabilities affecting utilization rounding
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
