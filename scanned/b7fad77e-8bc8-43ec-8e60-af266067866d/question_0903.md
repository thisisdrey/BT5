# Q0903: Loans payment clearing and borrower effects: multi-epoch pay / timing gap / cash-to-clearing consistency

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with multiple borrower payments straddling a due-date or withdrawal boundary while the loan is `Active` with non-zero principal or interest receivables and create a borrower-controlled timing gap where real cash exists on-chain but pricing or withdrawal paths still see the wrong balances, breaking the rule that the cash held after `pay` should always have a consistent representation in borrower payment clearing for that same loan and leading to Cross-user exploit window against NAV-sensitive vault operations?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: multiple borrower payments straddling a due-date or withdrawal boundary
- Exploit idea: create a borrower-controlled timing gap where real cash exists on-chain but pricing or withdrawal paths still see the wrong balances
- Invariant to test: the cash held after `pay` should always have a consistent representation in borrower payment clearing for that same loan
- Expected Immunefi impact: Cross-user exploit window against NAV-sensitive vault operations
- Fast validation: Model a vault-held loan, have the borrower pay just before NAV-sensitive approvals, and assert no stale-price claim becomes possible.
