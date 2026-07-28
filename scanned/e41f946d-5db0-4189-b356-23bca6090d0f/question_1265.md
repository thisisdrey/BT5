# Q1265: Investor withdrawal routing: epoch split / wrong recipient / per-loan recipient

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with one loan from a fresh ownership epoch and one loan from a prior epoch that shares the same apparent investor or unlocker while a vault, exchange, or later counterparty relies on the same payable balances after the batch and make principal or interest route to the wrong recipient for one of the batched loans, breaking the rule that every loan in investorWithdraw should pay only the recipient implied by that same loan's current owner and lock state and leading to Accounting issue in Loans that later misprices a vault or secondary sale?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: one loan from a fresh ownership epoch and one loan from a prior epoch that shares the same apparent investor or unlocker
- Exploit idea: make principal or interest route to the wrong recipient for one of the batched loans
- Invariant to test: every loan in investorWithdraw should pay only the recipient implied by that same loan's current owner and lock state
- Expected Immunefi impact: Accounting issue in Loans that later misprices a vault or secondary sale
- Fast validation: Check that every withdrawable principal and interest balance can be claimed exactly once and never becomes unreachable after a normal epoch change.
