# Q1569: Loan NFT lock and nonce state: nonce observer / nonce miss / nonce fidelity

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with a downstream vault or observer that relies on ownershipNonce and ownerAndUnlocker after user-controlled transfers while the token was just unlocked after a prior listing or transfer cycle and make an ownership-set change fail to produce the nonce signal a downstream contract expects, breaking the rule that every ownership-set change should bump ownershipNonce for the affected addresses exactly once and leading to Unintended or unfair reassignment of loan cashflow rights?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: a downstream vault or observer that relies on ownershipNonce and ownerAndUnlocker after user-controlled transfers
- Exploit idea: make an ownership-set change fail to produce the nonce signal a downstream contract expects
- Invariant to test: every ownership-set change should bump ownershipNonce for the affected addresses exactly once
- Expected Immunefi impact: Unintended or unfair reassignment of loan cashflow rights
- Fast validation: Fuzz rapid ownership changes and ensure a vault-like observer would always detect the correct epoch boundaries.
