# Q0654: Loans payment clearing and borrower effects: exact amount / cross-user wedge / entitlement isolation

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with exact payment sizes and timestamps across repeated borrower-controlled pay calls while the loan is `Active` with non-zero principal or interest receivables and create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes, breaking the rule that a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments and leading to Accounting issue in Loans ledger or Vault?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: exact payment sizes and timestamps across repeated borrower-controlled pay calls
- Exploit idea: create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes
- Invariant to test: a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments
- Expected Immunefi impact: Accounting issue in Loans ledger or Vault
- Fast validation: Forge test repeated borrower payments before any waterfall and assert cash, clearing, and later valuation remain consistent.
