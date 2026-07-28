# Q0568: Loans funding and disbursement state: role handoff / double-fund path / same-loan accounting

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a loan whose investor ownership changes between creation and the attempted funding call while a vault, buyer, or downstream counterparty could rely on the resulting balances after funding or disbursement and make commitment or `alreadyFunded` checks observe an inconsistent ledger state that allows replayed funding or funding after the economic state advanced, breaking the rule that funding or disbursing one loan should never create withdrawable or priceable balances attributable to another loan or investor and leading to Unintended or unfair fund distribution between investor, borrower, and originator paths?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a loan whose investor ownership changes between creation and the attempted funding call
- Exploit idea: make commitment or `alreadyFunded` checks observe an inconsistent ledger state that allows replayed funding or funding after the economic state advanced
- Invariant to test: funding or disbursing one loan should never create withdrawable or priceable balances attributable to another loan or investor
- Expected Immunefi impact: Unintended or unfair fund distribution between investor, borrower, and originator paths
- Fast validation: Forge test ownership changes, lock cycles, and standing allowances around `fund`, then assert only the current NFT owner can move capital exactly once.
