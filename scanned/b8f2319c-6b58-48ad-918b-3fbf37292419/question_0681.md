# Q681: check_utilization_ratio: rounding boundary creates extractable dust [a-borrow-with-dust-sized] [cache-order]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a borrow with dust-sized assets/liabilities affecting utilization rounding to push `check_utilization_ratio` across a rounding edge where protocol totals and user shares no longer match, breaking `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and eventually causing `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow with dust-sized assets/liabilities affecting utilization rounding
- Exploit idea: Search for floor/ceil mismatches between user-facing token amounts and internal share accounting near zero, one-share, or threshold-sized transitions. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Fuzz tiny and boundary amounts around the relevant threshold and assert that repeated cycles cannot increase withdrawable assets or decrease repayable debt. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
