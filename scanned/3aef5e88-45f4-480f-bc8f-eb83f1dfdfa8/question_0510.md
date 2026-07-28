# Q0510: Loans funding and disbursement state: lock cycle / epoch confusion / exact commitment

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a normal list-lock-unlock cycle around the same loan before funding or disbursement while a vault, buyer, or downstream counterparty could rely on the resulting balances after funding or disbursement and make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions, breaking the rule that the funded amount and disbursed amount should always map to the same remaining commitment for the same loan and leading to Bypass of intended permissions and lifecycle guards around funding or disbursement?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a normal list-lock-unlock cycle around the same loan before funding or disbursement
- Exploit idea: make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions
- Invariant to test: the funded amount and disbursed amount should always map to the same remaining commitment for the same loan
- Expected Immunefi impact: Bypass of intended permissions and lifecycle guards around funding or disbursement
- Fast validation: Model a create-transfer-fund-disburse sequence and assert stale ownership or stale lock state never authorizes a later epoch.
