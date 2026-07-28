# Q0652: Loans payment clearing and borrower effects: exact amount / date inconsistency / no exploitable pricing gap

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with exact payment sizes and timestamps across repeated borrower-controlled pay calls while the loan is `Active` with non-zero principal or interest receivables and make repeated `pay` calls leave `lastPaymentDate` and economic balances in an order that later logic misinterprets, breaking the rule that a borrower-controlled payment timing sequence should not let another unprivileged user exploit a vault approval or withdrawal against stale economic state and leading to Unintended or unfair fund distribution between current and future investors or shareholders?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: exact payment sizes and timestamps across repeated borrower-controlled pay calls
- Exploit idea: make repeated `pay` calls leave `lastPaymentDate` and economic balances in an order that later logic misinterprets
- Invariant to test: a borrower-controlled payment timing sequence should not let another unprivileged user exploit a vault approval or withdrawal against stale economic state
- Expected Immunefi impact: Unintended or unfair fund distribution between current and future investors or shareholders
- Fast validation: Check that repeated ordinary payments cannot create a durable wedge between on-chain cash and user-withdrawable or priceable balances.
