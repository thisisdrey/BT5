# Q0327: Loans funding and disbursement state: standing allowance / double-fund path / no stale owner

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a standing ERC20 approval from the current investor plus attacker-controlled NFT transfer timing while the loan is still `Created` with positive borrower principal receivable and zero investor principal payable and make commitment or `alreadyFunded` checks observe an inconsistent ledger state that allows replayed funding or funding after the economic state advanced, breaking the rule that a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch and leading to Accounting issue in Loans that later affects withdrawals or vault pricing?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a standing ERC20 approval from the current investor plus attacker-controlled NFT transfer timing
- Exploit idea: make commitment or `alreadyFunded` checks observe an inconsistent ledger state that allows replayed funding or funding after the economic state advanced
- Invariant to test: a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch
- Expected Immunefi impact: Accounting issue in Loans that later affects withdrawals or vault pricing
- Fast validation: Model a create-transfer-fund-disburse sequence and assert stale ownership or stale lock state never authorizes a later epoch.
