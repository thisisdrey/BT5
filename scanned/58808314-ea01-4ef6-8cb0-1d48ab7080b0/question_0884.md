# Q0884: Loans payment clearing and borrower effects: vault-held loan / clearing mismatch / no exploitable pricing gap

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with payments into a loan currently owned by a vault that will later rely on NAV-sensitive approvals while a vault manager could update NAV or approve deposits/redemptions soon after the borrower payment and make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes, breaking the rule that a borrower-controlled payment timing sequence should not let another unprivileged user exploit a vault approval or withdrawal against stale economic state and leading to Unintended or unfair fund distribution between current and future investors or shareholders?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: payments into a loan currently owned by a vault that will later rely on NAV-sensitive approvals
- Exploit idea: make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes
- Invariant to test: a borrower-controlled payment timing sequence should not let another unprivileged user exploit a vault approval or withdrawal against stale economic state
- Expected Immunefi impact: Unintended or unfair fund distribution between current and future investors or shareholders
- Fast validation: Model a vault-held loan, have the borrower pay just before NAV-sensitive approvals, and assert no stale-price claim becomes possible.
