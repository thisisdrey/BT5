# Q2993: lending_account_withdraw: repeatable cycle amplifies tiny accounting drift [a-withdraw-amount-at-the] [cache-order]

## Question
Can an unprivileged attacker repeat `lending_account_withdraw` under a withdraw amount at the last-share boundary so `lending_account_withdraw` leaks value through a cycle that is individually small but cumulatively breaks `withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency` and leads to `Critical: direct theft or creation of bad debt via over-withdrawal`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/withdraw.rs` / `lending_account_withdraw`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw amount at the last-share boundary
- Exploit idea: Look for a per-call mismatch that can be looped cheaply without relying on heavy traffic, especially deposit/withdraw, borrow/repay, or accrue/settle cycles. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency
- Expected Immunefi impact: Critical: direct theft or creation of bad debt via over-withdrawal
- Fast validation: Run a deterministic loop of the controlled sequence and assert the attacker cannot monotonically increase assets, reduce liabilities, or move protocol totals. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
