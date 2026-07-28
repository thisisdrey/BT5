# Q1189: Investor withdrawal routing: mixed balances / double claim / per-loan recipient

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with a batch where some loans have only principal, some only interest, and some zero withdrawable value while a sale offer was recently accepted or cancelled before the batch executes and make one payable balance claimable in two ownership or lock epochs, breaking the rule that every loan in investorWithdraw should pay only the recipient implied by that same loan's current owner and lock state and leading to Unintended or unfair fund distribution across investors, buyers, or sellers?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: a batch where some loans have only principal, some only interest, and some zero withdrawable value
- Exploit idea: make one payable balance claimable in two ownership or lock epochs
- Invariant to test: every loan in investorWithdraw should pay only the recipient implied by that same loan's current owner and lock state
- Expected Immunefi impact: Unintended or unfair fund distribution across investors, buyers, or sellers
- Fast validation: Fuzz batched loan ordering and payable compositions and assert the first loan's cached recipient never bleeds into another loan improperly.
