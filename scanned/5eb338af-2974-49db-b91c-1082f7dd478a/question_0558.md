# Q0558: Loans funding and disbursement state: role handoff / epoch confusion / exact commitment

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a loan whose investor ownership changes between creation and the attempted funding call while the NFT has been approved, listed, cancelled, or transferred shortly before the funding call and make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions, breaking the rule that the funded amount and disbursed amount should always map to the same remaining commitment for the same loan and leading to Bypass of intended permissions and lifecycle guards around funding or disbursement?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a loan whose investor ownership changes between creation and the attempted funding call
- Exploit idea: make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions
- Invariant to test: the funded amount and disbursed amount should always map to the same remaining commitment for the same loan
- Expected Immunefi impact: Bypass of intended permissions and lifecycle guards around funding or disbursement
- Fast validation: Assert that any failed disbursement or replay attempt reverts without leaving priceable or withdrawable balances behind.
