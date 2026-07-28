# Q0880: Loans payment clearing and borrower effects: vault-held loan / cross-user wedge / no exploitable pricing gap

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with payments into a loan currently owned by a vault that will later rely on NAV-sensitive approvals while an investor or vault cashflow collection could run soon after the borrower payment and create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes, breaking the rule that a borrower-controlled payment timing sequence should not let another unprivileged user exploit a vault approval or withdrawal against stale economic state and leading to Cross-user exploit window against NAV-sensitive vault operations?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: payments into a loan currently owned by a vault that will later rely on NAV-sensitive approvals
- Exploit idea: create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes
- Invariant to test: a borrower-controlled payment timing sequence should not let another unprivileged user exploit a vault approval or withdrawal against stale economic state
- Expected Immunefi impact: Cross-user exploit window against NAV-sensitive vault operations
- Fast validation: Model a vault-held loan, have the borrower pay just before NAV-sensitive approvals, and assert no stale-price claim becomes possible.
