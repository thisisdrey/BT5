# Q1039: Investor withdrawal routing: ownership churn / stuck route / batch isolation

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with NFT ownership changes across attacker-controlled addresses between withdrawal attempts while the first loan in the batch is unlocked and fixes the recipient as the current investor and make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary, breaking the rule that batch authorization and recipient caching should never merge entitlements across loans that only appear similar and leading to Unintended or unfair fund distribution across investors, buyers, or sellers?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: NFT ownership changes across attacker-controlled addresses between withdrawal attempts
- Exploit idea: make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary
- Invariant to test: batch authorization and recipient caching should never merge entitlements across loans that only appear similar
- Expected Immunefi impact: Unintended or unfair fund distribution across investors, buyers, or sellers
- Fast validation: Fuzz batched loan ordering and payable compositions and assert the first loan's cached recipient never bleeds into another loan improperly.
