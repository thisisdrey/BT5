# Q1301: Loan NFT lock and nonce state: per-token approval / epoch residue / nonce fidelity

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with a normal per-token approval rather than a global operator approval while the token is currently locked to a non-zero unlocker and make a lock or approval residue survive into the next ownership epoch, breaking the rule that every ownership-set change should bump ownershipNonce for the affected addresses exactly once and leading to Unintended or unfair reassignment of loan cashflow rights?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: a normal per-token approval rather than a global operator approval
- Exploit idea: make a lock or approval residue survive into the next ownership epoch
- Invariant to test: every ownership-set change should bump ownershipNonce for the affected addresses exactly once
- Expected Immunefi impact: Unintended or unfair reassignment of loan cashflow rights
- Fast validation: Forge test per-token approvals, lock/unlock cycles, and transfers, then assert nonce, owner, and unlocker views stay in one epoch.
