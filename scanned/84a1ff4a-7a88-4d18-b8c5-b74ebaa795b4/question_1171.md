# Q1171: Investor withdrawal routing: mixed balances / wrong recipient / batch isolation

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with a batch where some loans have only principal, some only interest, and some zero withdrawable value while the first loan in the batch is locked and fixes the recipient as the active unlocker and make principal or interest route to the wrong recipient for one of the batched loans, breaking the rule that batch authorization and recipient caching should never merge entitlements across loans that only appear similar and leading to Unintended or unfair fund distribution across investors, buyers, or sellers?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: a batch where some loans have only principal, some only interest, and some zero withdrawable value
- Exploit idea: make principal or interest route to the wrong recipient for one of the batched loans
- Invariant to test: batch authorization and recipient caching should never merge entitlements across loans that only appear similar
- Expected Immunefi impact: Unintended or unfair fund distribution across investors, buyers, or sellers
- Fast validation: Forge test mixed locked/unlocked batches, duplicate ids, and ownership transfers, then assert each loan pays exactly one valid recipient once.
