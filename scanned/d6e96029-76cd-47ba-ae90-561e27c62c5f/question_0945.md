# Q0945: Loans payment clearing and borrower effects: multi-epoch pay / clearing mismatch / same-loan cash

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with multiple borrower payments straddling a due-date or withdrawal boundary while a vault manager could update NAV or approve deposits/redemptions soon after the borrower payment and make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes, breaking the rule that `pay` should only increase `ACC_CASH` and `ACC_BORROWER_PAYMENT_CLEARING` for the same loan and never leak across loans and leading to Cross-user exploit window against NAV-sensitive vault operations?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: multiple borrower payments straddling a due-date or withdrawal boundary
- Exploit idea: make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes
- Invariant to test: `pay` should only increase `ACC_CASH` and `ACC_BORROWER_PAYMENT_CLEARING` for the same loan and never leak across loans
- Expected Immunefi impact: Cross-user exploit window against NAV-sensitive vault operations
- Fast validation: Check that repeated ordinary payments cannot create a durable wedge between on-chain cash and user-withdrawable or priceable balances.
