# Q0588: Loans funding and disbursement state: batch neighbor / phase skip / same-loan accounting

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with one target loan plus a nearby loan in a different lifecycle phase that the attacker also controls while the loan is still `Created` with positive borrower principal receivable and zero investor principal payable and make disbursement succeed after an unprivileged sequence that should have left the loan unfundable or undisbursable, breaking the rule that funding or disbursing one loan should never create withdrawable or priceable balances attributable to another loan or investor and leading to Bypass of intended permissions and lifecycle guards around funding or disbursement?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: one target loan plus a nearby loan in a different lifecycle phase that the attacker also controls
- Exploit idea: make disbursement succeed after an unprivileged sequence that should have left the loan unfundable or undisbursable
- Invariant to test: funding or disbursing one loan should never create withdrawable or priceable balances attributable to another loan or investor
- Expected Immunefi impact: Bypass of intended permissions and lifecycle guards around funding or disbursement
- Fast validation: Assert that any failed disbursement or replay attempt reverts without leaving priceable or withdrawable balances behind.
