# Q0725: Loans payment clearing and borrower effects: tiny-large mix / timing gap / same-loan cash

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with alternating tiny and large payment amounts before any servicing batch runs while no waterfall has yet moved the fresh payment out of `ACC_BORROWER_PAYMENT_CLEARING` and create a borrower-controlled timing gap where real cash exists on-chain but pricing or withdrawal paths still see the wrong balances, breaking the rule that `pay` should only increase `ACC_CASH` and `ACC_BORROWER_PAYMENT_CLEARING` for the same loan and never leak across loans and leading to Cross-user exploit window against NAV-sensitive vault operations?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: alternating tiny and large payment amounts before any servicing batch runs
- Exploit idea: create a borrower-controlled timing gap where real cash exists on-chain but pricing or withdrawal paths still see the wrong balances
- Invariant to test: `pay` should only increase `ACC_CASH` and `ACC_BORROWER_PAYMENT_CLEARING` for the same loan and never leak across loans
- Expected Immunefi impact: Cross-user exploit window against NAV-sensitive vault operations
- Fast validation: Check that repeated ordinary payments cannot create a durable wedge between on-chain cash and user-withdrawable or priceable balances.
