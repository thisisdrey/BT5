# Q0471: Loans funding and disbursement state: lock cycle / double-fund path / no stale owner

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a normal list-lock-unlock cycle around the same loan before funding or disbursement while status-like fields or off-chain expectations changed nearby but the economic balances still define the real state and make commitment or `alreadyFunded` checks observe an inconsistent ledger state that allows replayed funding or funding after the economic state advanced, breaking the rule that a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch and leading to Unintended or unfair fund distribution between investor, borrower, and originator paths?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a normal list-lock-unlock cycle around the same loan before funding or disbursement
- Exploit idea: make commitment or `alreadyFunded` checks observe an inconsistent ledger state that allows replayed funding or funding after the economic state advanced
- Invariant to test: a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch
- Expected Immunefi impact: Unintended or unfair fund distribution between investor, borrower, and originator paths
- Fast validation: Forge test ownership changes, lock cycles, and standing allowances around `fund`, then assert only the current NFT owner can move capital exactly once.
