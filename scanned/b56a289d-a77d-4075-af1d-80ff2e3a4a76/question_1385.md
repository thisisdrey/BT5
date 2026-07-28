# Q1385: Loan NFT lock and nonce state: rapid epochs / mixed read / nonce fidelity

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with rapid lock, unlock, and transfer cycles across attacker-controlled addresses while the token was just unlocked after a prior listing or transfer cycle and make downstream code observe an owner and unlocker pair that belong to different epochs, breaking the rule that every ownership-set change should bump ownershipNonce for the affected addresses exactly once and leading to Bypass or corruption of downstream ownership-change checks that gate vault pricing or withdrawals?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: rapid lock, unlock, and transfer cycles across attacker-controlled addresses
- Exploit idea: make downstream code observe an owner and unlocker pair that belong to different epochs
- Invariant to test: every ownership-set change should bump ownershipNonce for the affected addresses exactly once
- Expected Immunefi impact: Bypass or corruption of downstream ownership-change checks that gate vault pricing or withdrawals
- Fast validation: Check that no normal lock or transfer sequence leaves a token or its associated withdrawal rights permanently stranded.
