# Q0849: Loans payment clearing and borrower effects: vault-held loan / clearing mismatch / same-loan cash

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with payments into a loan currently owned by a vault that will later rely on NAV-sensitive approvals while no waterfall has yet moved the fresh payment out of `ACC_BORROWER_PAYMENT_CLEARING` and make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes, breaking the rule that `pay` should only increase `ACC_CASH` and `ACC_BORROWER_PAYMENT_CLEARING` for the same loan and never leak across loans and leading to Accounting issue in Loans ledger or Vault?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: payments into a loan currently owned by a vault that will later rely on NAV-sensitive approvals
- Exploit idea: make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes
- Invariant to test: `pay` should only increase `ACC_CASH` and `ACC_BORROWER_PAYMENT_CLEARING` for the same loan and never leak across loans
- Expected Immunefi impact: Accounting issue in Loans ledger or Vault
- Fast validation: Fuzz payment sizes and timestamps around due dates and assert `lastPaymentDate`, clearing, and cash never enter a contradictory state.
