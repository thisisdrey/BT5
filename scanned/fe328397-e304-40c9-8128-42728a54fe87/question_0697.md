# Q0697: Loans payment clearing and borrower effects: exact amount / date inconsistency / same-loan cash

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with exact payment sizes and timestamps across repeated borrower-controlled pay calls while a vault manager could update NAV or approve deposits/redemptions soon after the borrower payment and make repeated `pay` calls leave `lastPaymentDate` and economic balances in an order that later logic misinterprets, breaking the rule that `pay` should only increase `ACC_CASH` and `ACC_BORROWER_PAYMENT_CLEARING` for the same loan and never leak across loans and leading to Unintended or unfair fund distribution between current and future investors or shareholders?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: exact payment sizes and timestamps across repeated borrower-controlled pay calls
- Exploit idea: make repeated `pay` calls leave `lastPaymentDate` and economic balances in an order that later logic misinterprets
- Invariant to test: `pay` should only increase `ACC_CASH` and `ACC_BORROWER_PAYMENT_CLEARING` for the same loan and never leak across loans
- Expected Immunefi impact: Unintended or unfair fund distribution between current and future investors or shareholders
- Fast validation: Model a vault-held loan, have the borrower pay just before NAV-sensitive approvals, and assert no stale-price claim becomes possible.
