# Q0371: Loans funding and disbursement state: standing allowance / wrong investor pull / no stale owner

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a standing ERC20 approval from the current investor plus attacker-controlled NFT transfer timing while a vault, buyer, or downstream counterparty could rely on the resulting balances after funding or disbursement and make `fund` pull from or account for the wrong investor identity for the same loan, breaking the rule that a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch and leading to Bypass of intended permissions and lifecycle guards around funding or disbursement?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a standing ERC20 approval from the current investor plus attacker-controlled NFT transfer timing
- Exploit idea: make `fund` pull from or account for the wrong investor identity for the same loan
- Invariant to test: a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch
- Expected Immunefi impact: Bypass of intended permissions and lifecycle guards around funding or disbursement
- Fast validation: Model a create-transfer-fund-disburse sequence and assert stale ownership or stale lock state never authorizes a later epoch.
