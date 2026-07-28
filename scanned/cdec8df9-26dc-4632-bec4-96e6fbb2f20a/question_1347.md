# Q1347: Loan NFT lock and nonce state: rapid epochs / nonce miss / paired state read

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with rapid lock, unlock, and transfer cycles across attacker-controlled addresses while the token starts unlocked with a live owner and possibly a per-token approval and make an ownership-set change fail to produce the nonce signal a downstream contract expects, breaking the rule that ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs and leading to Cross-user accounting or pricing errors triggered by stale NFT state?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: rapid lock, unlock, and transfer cycles across attacker-controlled addresses
- Exploit idea: make an ownership-set change fail to produce the nonce signal a downstream contract expects
- Invariant to test: ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs
- Expected Immunefi impact: Cross-user accounting or pricing errors triggered by stale NFT state
- Fast validation: Simulate exchange settlement and immediate downstream reads, then assert the post-transfer state contains no stale unlocker or missed nonce bump.
