# Q0641: Loans payment clearing and borrower effects: exact amount / clearing mismatch / same-loan cash

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with exact payment sizes and timestamps across repeated borrower-controlled pay calls while the loan is `Active` with non-zero principal or interest receivables and make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes, breaking the rule that `pay` should only increase `ACC_CASH` and `ACC_BORROWER_PAYMENT_CLEARING` for the same loan and never leak across loans and leading to Accounting issue in Loans ledger or Vault?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: exact payment sizes and timestamps across repeated borrower-controlled pay calls
- Exploit idea: make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes
- Invariant to test: `pay` should only increase `ACC_CASH` and `ACC_BORROWER_PAYMENT_CLEARING` for the same loan and never leak across loans
- Expected Immunefi impact: Accounting issue in Loans ledger or Vault
- Fast validation: Forge test repeated borrower payments before any waterfall and assert cash, clearing, and later valuation remain consistent.
