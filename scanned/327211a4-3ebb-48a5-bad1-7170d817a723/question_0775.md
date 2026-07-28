# Q0775: Loans payment clearing and borrower effects: charged-off pay / timing gap / cash-to-clearing consistency

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with payments into a loan that is `ChargedOff` but still accepts borrower payments while the loan is `Active` with non-zero principal or interest receivables and create a borrower-controlled timing gap where real cash exists on-chain but pricing or withdrawal paths still see the wrong balances, breaking the rule that the cash held after `pay` should always have a consistent representation in borrower payment clearing for that same loan and leading to Unintended or unfair fund distribution between current and future investors or shareholders?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: payments into a loan that is `ChargedOff` but still accepts borrower payments
- Exploit idea: create a borrower-controlled timing gap where real cash exists on-chain but pricing or withdrawal paths still see the wrong balances
- Invariant to test: the cash held after `pay` should always have a consistent representation in borrower payment clearing for that same loan
- Expected Immunefi impact: Unintended or unfair fund distribution between current and future investors or shareholders
- Fast validation: Check that repeated ordinary payments cannot create a durable wedge between on-chain cash and user-withdrawable or priceable balances.
