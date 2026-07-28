# Q0521: Loans funding and disbursement state: role handoff / phase skip / single funding

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a loan whose investor ownership changes between creation and the attempted funding call while the loan is still `Created` with positive borrower principal receivable and zero investor principal payable and make disbursement succeed after an unprivileged sequence that should have left the loan unfundable or undisbursable, breaking the rule that `fund` should only succeed once per loan and only for the current NFT owner of that same loan and leading to Bypass of intended permissions and lifecycle guards around funding or disbursement?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a loan whose investor ownership changes between creation and the attempted funding call
- Exploit idea: make disbursement succeed after an unprivileged sequence that should have left the loan unfundable or undisbursable
- Invariant to test: `fund` should only succeed once per loan and only for the current NFT owner of that same loan
- Expected Immunefi impact: Bypass of intended permissions and lifecycle guards around funding or disbursement
- Fast validation: Assert that any failed disbursement or replay attempt reverts without leaving priceable or withdrawable balances behind.
