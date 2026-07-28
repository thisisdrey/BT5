# Q0501: Loans funding and disbursement state: lock cycle / double-fund path / single funding

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a normal list-lock-unlock cycle around the same loan before funding or disbursement while a vault, buyer, or downstream counterparty could rely on the resulting balances after funding or disbursement and make commitment or `alreadyFunded` checks observe an inconsistent ledger state that allows replayed funding or funding after the economic state advanced, breaking the rule that `fund` should only succeed once per loan and only for the current NFT owner of that same loan and leading to Unintended or unfair fund distribution between investor, borrower, and originator paths?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a normal list-lock-unlock cycle around the same loan before funding or disbursement
- Exploit idea: make commitment or `alreadyFunded` checks observe an inconsistent ledger state that allows replayed funding or funding after the economic state advanced
- Invariant to test: `fund` should only succeed once per loan and only for the current NFT owner of that same loan
- Expected Immunefi impact: Unintended or unfair fund distribution between investor, borrower, and originator paths
- Fast validation: Forge test ownership changes, lock cycles, and standing allowances around `fund`, then assert only the current NFT owner can move capital exactly once.
