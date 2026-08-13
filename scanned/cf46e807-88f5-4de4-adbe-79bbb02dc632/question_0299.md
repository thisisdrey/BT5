# Q299: get_liability_shares: cache refresh ordering permits stale acceptance [a-borrow-when-another-balance] [cache-order]

## Question
Can an unprivileged attacker call `lending_account_borrow` with a borrow when another balance on the account is about to become inactive by dust rounding so `get_liability_shares` accepts a state transition using stale cache values before refresh or recomputation, violating `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and causing `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow when another balance on the account is about to become inactive by dust rounding
- Exploit idea: Audit whether the instruction depends on cached bank/account state that can lag behind the exact balances or prices settled later in the same path. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Prepare mismatched cache and live state, invoke the instruction, and assert acceptance never occurs unless recomputed state would still satisfy the rules. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
