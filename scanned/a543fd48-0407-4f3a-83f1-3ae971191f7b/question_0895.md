# Q0895: Loans payment clearing and borrower effects: vault-held loan / cross-user wedge / cash-to-clearing consistency

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with payments into a loan currently owned by a vault that will later rely on NAV-sensitive approvals while a vault manager could update NAV or approve deposits/redemptions soon after the borrower payment and create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes, breaking the rule that the cash held after `pay` should always have a consistent representation in borrower payment clearing for that same loan and leading to Cross-user exploit window against NAV-sensitive vault operations?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: payments into a loan currently owned by a vault that will later rely on NAV-sensitive approvals
- Exploit idea: create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes
- Invariant to test: the cash held after `pay` should always have a consistent representation in borrower payment clearing for that same loan
- Expected Immunefi impact: Cross-user exploit window against NAV-sensitive vault operations
- Fast validation: Check that repeated ordinary payments cannot create a durable wedge between on-chain cash and user-withdrawable or priceable balances.
