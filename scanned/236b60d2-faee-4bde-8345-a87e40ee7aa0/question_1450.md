# Q1450: Loan NFT lock and nonce state: buyer settlement / mixed read / epoch cleanliness

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with an exchange-driven transfer by the active unlocker into a buyer-controlled address while the token was just unlocked after a prior listing or transfer cycle and make downstream code observe an owner and unlocker pair that belong to different epochs, breaking the rule that lock and approval state should belong to one ownership epoch only and never bleed forward and leading to Cross-user accounting or pricing errors triggered by stale NFT state?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: an exchange-driven transfer by the active unlocker into a buyer-controlled address
- Exploit idea: make downstream code observe an owner and unlocker pair that belong to different epochs
- Invariant to test: lock and approval state should belong to one ownership epoch only and never bleed forward
- Expected Immunefi impact: Cross-user accounting or pricing errors triggered by stale NFT state
- Fast validation: Simulate exchange settlement and immediate downstream reads, then assert the post-transfer state contains no stale unlocker or missed nonce bump.
