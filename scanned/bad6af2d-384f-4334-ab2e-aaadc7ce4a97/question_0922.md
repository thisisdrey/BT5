# Q0922: Loans payment clearing and borrower effects: multi-epoch pay / date inconsistency / entitlement isolation

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with multiple borrower payments straddling a due-date or withdrawal boundary while no waterfall has yet moved the fresh payment out of `ACC_BORROWER_PAYMENT_CLEARING` and make repeated `pay` calls leave `lastPaymentDate` and economic balances in an order that later logic misinterprets, breaking the rule that a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments and leading to Accounting issue in Loans ledger or Vault?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: multiple borrower payments straddling a due-date or withdrawal boundary
- Exploit idea: make repeated `pay` calls leave `lastPaymentDate` and economic balances in an order that later logic misinterprets
- Invariant to test: a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments
- Expected Immunefi impact: Accounting issue in Loans ledger or Vault
- Fast validation: Fuzz payment sizes and timestamps around due dates and assert `lastPaymentDate`, clearing, and cash never enter a contradictory state.
