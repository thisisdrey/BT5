# Q1327: Loan NFT lock and nonce state: per-token approval / transition gap / paired state read

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with a normal per-token approval rather than a global operator approval while the token was just unlocked after a prior listing or transfer cycle and make a normal lock/unlock/transfer transition create a gap where no actor can safely use the token or its cashflow rights, breaking the rule that ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs and leading to Cross-user accounting or pricing errors triggered by stale NFT state?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: a normal per-token approval rather than a global operator approval
- Exploit idea: make a normal lock/unlock/transfer transition create a gap where no actor can safely use the token or its cashflow rights
- Invariant to test: ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs
- Expected Immunefi impact: Cross-user accounting or pricing errors triggered by stale NFT state
- Fast validation: Simulate exchange settlement and immediate downstream reads, then assert the post-transfer state contains no stale unlocker or missed nonce bump.
