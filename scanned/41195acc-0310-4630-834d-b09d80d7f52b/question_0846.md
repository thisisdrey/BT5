# Q0846: Loans payment clearing and borrower effects: vault-held loan / cross-user wedge / entitlement isolation

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with payments into a loan currently owned by a vault that will later rely on NAV-sensitive approvals while the loan is `Active` with non-zero principal or interest receivables and create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes, breaking the rule that a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments and leading to Cross-user exploit window against NAV-sensitive vault operations?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: payments into a loan currently owned by a vault that will later rely on NAV-sensitive approvals
- Exploit idea: create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes
- Invariant to test: a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments
- Expected Immunefi impact: Cross-user exploit window against NAV-sensitive vault operations
- Fast validation: Model a vault-held loan, have the borrower pay just before NAV-sensitive approvals, and assert no stale-price claim becomes possible.
