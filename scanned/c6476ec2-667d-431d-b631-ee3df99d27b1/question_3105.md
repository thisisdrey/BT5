# Q3105: lending_account_borrow: state updated before the transfer outcome is final [a-borrow-amount-at-the] [cache-order]

## Question
Can an unprivileged attacker make `lending_account_borrow` reach `lending_account_borrow` with a borrow amount at the exact borrow-cap boundary such that accounting mutates before the real token/value transfer is conclusively enforced, breaking `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and causing `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow amount at the exact borrow-cap boundary
- Exploit idea: Check whether partial state mutation can survive a later transfer/accounting edge and leave the user with value or debt inconsistent with actual token movement. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Inject the controlled token/account conditions and assert that any downstream failure rolls back all shares, caches, and flags atomically. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
