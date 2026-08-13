# Q3077: lending_account_borrow: rounding boundary creates extractable dust [a-same-slot-repay-then] [cache-order]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a same-slot repay then borrow sequence around dust debt to push `lending_account_borrow` across a rounding edge where protocol totals and user shares no longer match, breaking `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and eventually causing `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a same-slot repay then borrow sequence around dust debt
- Exploit idea: Search for floor/ceil mismatches between user-facing token amounts and internal share accounting near zero, one-share, or threshold-sized transitions. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Fuzz tiny and boundary amounts around the relevant threshold and assert that repeated cycles cannot increase withdrawable assets or decrease repayable debt. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
