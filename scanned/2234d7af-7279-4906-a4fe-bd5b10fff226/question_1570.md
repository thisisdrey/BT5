# Q1570: Loan NFT lock and nonce state: nonce observer / nonce miss / epoch cleanliness

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with a downstream vault or observer that relies on ownershipNonce and ownerAndUnlocker after user-controlled transfers while the token was just unlocked after a prior listing or transfer cycle and make an ownership-set change fail to produce the nonce signal a downstream contract expects, breaking the rule that lock and approval state should belong to one ownership epoch only and never bleed forward and leading to Cross-user accounting or pricing errors triggered by stale NFT state?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: a downstream vault or observer that relies on ownershipNonce and ownerAndUnlocker after user-controlled transfers
- Exploit idea: make an ownership-set change fail to produce the nonce signal a downstream contract expects
- Invariant to test: lock and approval state should belong to one ownership epoch only and never bleed forward
- Expected Immunefi impact: Cross-user accounting or pricing errors triggered by stale NFT state
- Fast validation: Simulate exchange settlement and immediate downstream reads, then assert the post-transfer state contains no stale unlocker or missed nonce bump.
