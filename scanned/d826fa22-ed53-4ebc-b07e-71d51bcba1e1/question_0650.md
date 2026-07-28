# Q0650: Loans payment clearing and borrower effects: exact amount / date inconsistency / entitlement isolation

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with exact payment sizes and timestamps across repeated borrower-controlled pay calls while the loan is `Active` with non-zero principal or interest receivables and make repeated `pay` calls leave `lastPaymentDate` and economic balances in an order that later logic misinterprets, breaking the rule that a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments and leading to Cross-user exploit window against NAV-sensitive vault operations?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: exact payment sizes and timestamps across repeated borrower-controlled pay calls
- Exploit idea: make repeated `pay` calls leave `lastPaymentDate` and economic balances in an order that later logic misinterprets
- Invariant to test: a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments
- Expected Immunefi impact: Cross-user exploit window against NAV-sensitive vault operations
- Fast validation: Model a vault-held loan, have the borrower pay just before NAV-sensitive approvals, and assert no stale-price claim becomes possible.
