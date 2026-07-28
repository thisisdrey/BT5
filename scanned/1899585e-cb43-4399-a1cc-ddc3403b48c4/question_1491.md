# Q1491: Loan NFT lock and nonce state: contract holder / nonce miss / paired state read

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with an attacker-controlled contract address as the new loan holder while the token is currently locked to a non-zero unlocker and make an ownership-set change fail to produce the nonce signal a downstream contract expects, breaking the rule that ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs and leading to Unintended or unfair reassignment of loan cashflow rights?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: an attacker-controlled contract address as the new loan holder
- Exploit idea: make an ownership-set change fail to produce the nonce signal a downstream contract expects
- Invariant to test: ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs
- Expected Immunefi impact: Unintended or unfair reassignment of loan cashflow rights
- Fast validation: Forge test per-token approvals, lock/unlock cycles, and transfers, then assert nonce, owner, and unlocker views stay in one epoch.
