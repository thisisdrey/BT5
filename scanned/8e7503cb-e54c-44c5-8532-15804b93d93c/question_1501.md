# Q1501: Loan NFT lock and nonce state: contract holder / transition gap / nonce fidelity

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with an attacker-controlled contract address as the new loan holder while the token is currently locked to a non-zero unlocker and make a normal lock/unlock/transfer transition create a gap where no actor can safely use the token or its cashflow rights, breaking the rule that every ownership-set change should bump ownershipNonce for the affected addresses exactly once and leading to Cross-user accounting or pricing errors triggered by stale NFT state?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: an attacker-controlled contract address as the new loan holder
- Exploit idea: make a normal lock/unlock/transfer transition create a gap where no actor can safely use the token or its cashflow rights
- Invariant to test: every ownership-set change should bump ownershipNonce for the affected addresses exactly once
- Expected Immunefi impact: Cross-user accounting or pricing errors triggered by stale NFT state
- Fast validation: Check that no normal lock or transfer sequence leaves a token or its associated withdrawal rights permanently stranded.
