# Q2945: lending_account_withdraw: state updated before the transfer outcome is final [a-withdraw-amount-at-the] [cache-order]

## Question
Can an unprivileged attacker make `lending_account_withdraw` reach `lending_account_withdraw` with a withdraw amount at the last-share boundary such that accounting mutates before the real token/value transfer is conclusively enforced, breaking `withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency` and causing `Critical: direct theft or creation of bad debt via over-withdrawal`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/withdraw.rs` / `lending_account_withdraw`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw amount at the last-share boundary
- Exploit idea: Check whether partial state mutation can survive a later transfer/accounting edge and leave the user with value or debt inconsistent with actual token movement. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency
- Expected Immunefi impact: Critical: direct theft or creation of bad debt via over-withdrawal
- Fast validation: Inject the controlled token/account conditions and assert that any downstream failure rolls back all shares, caches, and flags atomically. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
