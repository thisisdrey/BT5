# Q0556: Loans funding and disbursement state: role handoff / phase skip / same-loan accounting

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a loan whose investor ownership changes between creation and the attempted funding call while the NFT has been approved, listed, cancelled, or transferred shortly before the funding call and make disbursement succeed after an unprivileged sequence that should have left the loan unfundable or undisbursable, breaking the rule that funding or disbursing one loan should never create withdrawable or priceable balances attributable to another loan or investor and leading to Unintended or unfair fund distribution between investor, borrower, and originator paths?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a loan whose investor ownership changes between creation and the attempted funding call
- Exploit idea: make disbursement succeed after an unprivileged sequence that should have left the loan unfundable or undisbursable
- Invariant to test: funding or disbursing one loan should never create withdrawable or priceable balances attributable to another loan or investor
- Expected Immunefi impact: Unintended or unfair fund distribution between investor, borrower, and originator paths
- Fast validation: Fuzz commitment-sized amounts and lifecycle orderings, then assert funding and disbursement preserve the exact same commitment ledger.
