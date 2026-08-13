# Q658: check_utilization_ratio: same-bank aliasing across mutable balance updates [a-borrow-at-the-exact] [cycle]

## Question
Can an unprivileged attacker call `lending_account_borrow` with a borrow at the exact utilization limit boundary so that `check_utilization_ratio` mutates the same logical bank exposure through aliased or reused balance state, violating `borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits` and causing `High: creation of unsafe bank state or later bad debt`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `check_utilization_ratio`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow at the exact utilization limit boundary
- Exploit idea: Try to make a single user action touch one economic exposure twice through reused balance slots, duplicate remaining accounts, or stale active-balance bookkeeping. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow acceptance must never depend on a stale or manipulable utilization view that allows debt beyond bank safety limits
- Expected Immunefi impact: High: creation of unsafe bank state or later bad debt
- Fast validation: Craft a test that reuses the same bank/account relationship in the controlled way and compare pre/post totals, shares, and user equity for double application. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
