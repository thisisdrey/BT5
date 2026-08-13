# Q671: check_utilization_ratio: same-bank aliasing across mutable balance updates [an-account-whose-other-positions] [cache-order]

## Question
Can an unprivileged attacker call `lending_account_borrow` with an account whose other positions make health barely pass before utilization is checked so that `check_utilization_ratio` mutates the same logical bank exposure through aliased or reused balance state, violating `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and causing `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: an account whose other positions make health barely pass before utilization is checked
- Exploit idea: Try to make a single user action touch one economic exposure twice through reused balance slots, duplicate remaining accounts, or stale active-balance bookkeeping. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Craft a test that reuses the same bank/account relationship in the controlled way and compare pre/post totals, shares, and user equity for double application. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
