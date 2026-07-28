# Q0520: Loans funding and disbursement state: role handoff / double-fund path / same-loan accounting

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a loan whose investor ownership changes between creation and the attempted funding call while the loan is still `Created` with positive borrower principal receivable and zero investor principal payable and make commitment or `alreadyFunded` checks observe an inconsistent ledger state that allows replayed funding or funding after the economic state advanced, breaking the rule that funding or disbursing one loan should never create withdrawable or priceable balances attributable to another loan or investor and leading to Accounting issue in Loans that later affects withdrawals or vault pricing?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a loan whose investor ownership changes between creation and the attempted funding call
- Exploit idea: make commitment or `alreadyFunded` checks observe an inconsistent ledger state that allows replayed funding or funding after the economic state advanced
- Invariant to test: funding or disbursing one loan should never create withdrawable or priceable balances attributable to another loan or investor
- Expected Immunefi impact: Accounting issue in Loans that later affects withdrawals or vault pricing
- Fast validation: Model a create-transfer-fund-disburse sequence and assert stale ownership or stale lock state never authorizes a later epoch.
