# Q0589: Loans funding and disbursement state: batch neighbor / epoch confusion / single funding

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with one target loan plus a nearby loan in a different lifecycle phase that the attacker also controls while the loan is still `Created` with positive borrower principal receivable and zero investor principal payable and make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions, breaking the rule that `fund` should only succeed once per loan and only for the current NFT owner of that same loan and leading to Accounting issue in Loans that later affects withdrawals or vault pricing?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: one target loan plus a nearby loan in a different lifecycle phase that the attacker also controls
- Exploit idea: make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions
- Invariant to test: `fund` should only succeed once per loan and only for the current NFT owner of that same loan
- Expected Immunefi impact: Accounting issue in Loans that later affects withdrawals or vault pricing
- Fast validation: Model a create-transfer-fund-disburse sequence and assert stale ownership or stale lock state never authorizes a later epoch.
