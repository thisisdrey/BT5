# Q0834: Loans payment clearing and borrower effects: vault-held loan / clearing mismatch / entitlement isolation

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with payments into a loan currently owned by a vault that will later rely on NAV-sensitive approvals while the loan is `Active` with non-zero principal or interest receivables and make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes, breaking the rule that a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments and leading to Accounting issue in Loans ledger or Vault?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: payments into a loan currently owned by a vault that will later rely on NAV-sensitive approvals
- Exploit idea: make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes
- Invariant to test: a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments
- Expected Immunefi impact: Accounting issue in Loans ledger or Vault
- Fast validation: Forge test repeated borrower payments before any waterfall and assert cash, clearing, and later valuation remain consistent.
