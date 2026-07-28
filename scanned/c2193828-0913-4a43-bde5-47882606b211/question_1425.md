# Q1425: Loan NFT lock and nonce state: buyer settlement / nonce miss / nonce fidelity

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with an exchange-driven transfer by the active unlocker into a buyer-controlled address while the token is currently locked to a non-zero unlocker and make an ownership-set change fail to produce the nonce signal a downstream contract expects, breaking the rule that every ownership-set change should bump ownershipNonce for the affected addresses exactly once and leading to Cross-user accounting or pricing errors triggered by stale NFT state?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: an exchange-driven transfer by the active unlocker into a buyer-controlled address
- Exploit idea: make an ownership-set change fail to produce the nonce signal a downstream contract expects
- Invariant to test: every ownership-set change should bump ownershipNonce for the affected addresses exactly once
- Expected Immunefi impact: Cross-user accounting or pricing errors triggered by stale NFT state
- Fast validation: Check that no normal lock or transfer sequence leaves a token or its associated withdrawal rights permanently stranded.
