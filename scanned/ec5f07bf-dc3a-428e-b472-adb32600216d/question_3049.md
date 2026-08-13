# Q3049: lending_account_borrow: share minting vs health check desync [a-borrow-immediately-after-permissionless] [cache-order]

## Question
Can an unprivileged attacker enter through `lending_account_borrow` and make `lending_account_borrow` observe a borrow immediately after permissionless cache or interest maintenance so that share minting/burning and health enforcement are evaluated from inconsistent state, breaking `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and leading to `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow immediately after permissionless cache or interest maintenance
- Exploit idea: Drive pre-state checks and post-state share changes through a boundary case so the instruction accepts a state transition that should fail once all balances are recomputed consistently. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Build an integration test around `lending_account_borrow` with the controlled state, then assert that accepted execution leaves post-instruction health negative or value moved beyond the allowed amount. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
