# Q0765: Loans payment clearing and borrower effects: tiny-large mix / cross-user wedge / same-loan cash

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with alternating tiny and large payment amounts before any servicing batch runs while a vault manager could update NAV or approve deposits/redemptions soon after the borrower payment and create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes, breaking the rule that `pay` should only increase `ACC_CASH` and `ACC_BORROWER_PAYMENT_CLEARING` for the same loan and never leak across loans and leading to Cross-user exploit window against NAV-sensitive vault operations?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: alternating tiny and large payment amounts before any servicing batch runs
- Exploit idea: create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes
- Invariant to test: `pay` should only increase `ACC_CASH` and `ACC_BORROWER_PAYMENT_CLEARING` for the same loan and never leak across loans
- Expected Immunefi impact: Cross-user exploit window against NAV-sensitive vault operations
- Fast validation: Check that repeated ordinary payments cannot create a durable wedge between on-chain cash and user-withdrawable or priceable balances.
