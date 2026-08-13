# Q2918: lending_account_withdraw: rounding boundary creates extractable dust [a-same-slot-deposit-then] [cycle]

## Question
Can an unprivileged attacker use `lending_account_withdraw` with a same-slot deposit then withdraw sequence around dust balances to push `lending_account_withdraw` across a rounding edge where protocol totals and user shares no longer match, breaking `withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency` and eventually causing `Critical: direct theft or creation of bad debt via over-withdrawal`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/withdraw.rs` / `lending_account_withdraw`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a same-slot deposit then withdraw sequence around dust balances
- Exploit idea: Search for floor/ceil mismatches between user-facing token amounts and internal share accounting near zero, one-share, or threshold-sized transitions. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency
- Expected Immunefi impact: Critical: direct theft or creation of bad debt via over-withdrawal
- Fast validation: Fuzz tiny and boundary amounts around the relevant threshold and assert that repeated cycles cannot increase withdrawable assets or decrease repayable debt. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
