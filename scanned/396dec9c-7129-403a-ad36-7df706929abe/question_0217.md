# Q217: get_liability_shares: remaining-accounts rebinding of the priced asset path [a-borrow-after-a-cache] [cache-order]

## Question
Can an unprivileged attacker supply a borrow after a cache refresh mismatch between the bank and the account health cache to `lending_account_borrow` so that `get_liability_shares` binds the wrong priced asset, bank, or vault path during validation, violating `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and leading to `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow after a cache refresh mismatch between the bank and the account health cache
- Exploit idea: Attempt to swap, omit, or reorder remaining accounts so a valid-looking path executes against the wrong economic context without failing ownership/group checks. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Construct two active banks and intentionally permute remaining accounts, then assert the instruction cannot succeed against the wrong bank or oracle context. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
