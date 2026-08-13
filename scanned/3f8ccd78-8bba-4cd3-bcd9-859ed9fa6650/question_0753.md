# Q753: check_utilization_ratio: repeatable cycle amplifies tiny accounting drift [a-borrow-at-the-exact] [cache-order]

## Question
Can an unprivileged attacker repeat `lending_account_borrow` under a borrow at the exact utilization limit boundary so `check_utilization_ratio` leaks value through a cycle that is individually small but cumulatively breaks `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and leads to `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow at the exact utilization limit boundary
- Exploit idea: Look for a per-call mismatch that can be looped cheaply without relying on heavy traffic, especially deposit/withdraw, borrow/repay, or accrue/settle cycles. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Run a deterministic loop of the controlled sequence and assert the attacker cannot monotonically increase assets, reduce liabilities, or move protocol totals. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
