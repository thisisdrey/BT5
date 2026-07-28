# Q1307: Loan NFT lock and nonce state: per-token approval / mixed read / paired state read

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with a normal per-token approval rather than a global operator approval while the token is currently locked to a non-zero unlocker and make downstream code observe an owner and unlocker pair that belong to different epochs, breaking the rule that ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs and leading to Bypass or corruption of downstream ownership-change checks that gate vault pricing or withdrawals?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: a normal per-token approval rather than a global operator approval
- Exploit idea: make downstream code observe an owner and unlocker pair that belong to different epochs
- Invariant to test: ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs
- Expected Immunefi impact: Bypass or corruption of downstream ownership-change checks that gate vault pricing or withdrawals
- Fast validation: Simulate exchange settlement and immediate downstream reads, then assert the post-transfer state contains no stale unlocker or missed nonce bump.
