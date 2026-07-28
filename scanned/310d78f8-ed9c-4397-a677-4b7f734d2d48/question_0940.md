# Q0940: Loans payment clearing and borrower effects: multi-epoch pay / date inconsistency / no exploitable pricing gap

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with multiple borrower payments straddling a due-date or withdrawal boundary while an investor or vault cashflow collection could run soon after the borrower payment and make repeated `pay` calls leave `lastPaymentDate` and economic balances in an order that later logic misinterprets, breaking the rule that a borrower-controlled payment timing sequence should not let another unprivileged user exploit a vault approval or withdrawal against stale economic state and leading to Cross-user exploit window against NAV-sensitive vault operations?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: multiple borrower payments straddling a due-date or withdrawal boundary
- Exploit idea: make repeated `pay` calls leave `lastPaymentDate` and economic balances in an order that later logic misinterprets
- Invariant to test: a borrower-controlled payment timing sequence should not let another unprivileged user exploit a vault approval or withdrawal against stale economic state
- Expected Immunefi impact: Cross-user exploit window against NAV-sensitive vault operations
- Fast validation: Model a vault-held loan, have the borrower pay just before NAV-sensitive approvals, and assert no stale-price claim becomes possible.
