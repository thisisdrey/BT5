# Q2954: lending_account_withdraw: state updated before the transfer outcome is final [a-user-with-several-active] [cycle]

## Question
Can an unprivileged attacker make `lending_account_withdraw` reach `lending_account_withdraw` with a user with several active balances and one recently closed slot such that accounting mutates before the real token/value transfer is conclusively enforced, breaking `withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency` and causing `Critical: direct theft or creation of bad debt via over-withdrawal`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/withdraw.rs` / `lending_account_withdraw`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a user with several active balances and one recently closed slot
- Exploit idea: Check whether partial state mutation can survive a later transfer/accounting edge and leave the user with value or debt inconsistent with actual token movement. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency
- Expected Immunefi impact: Critical: direct theft or creation of bad debt via over-withdrawal
- Fast validation: Inject the controlled token/account conditions and assert that any downstream failure rolls back all shares, caches, and flags atomically. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
