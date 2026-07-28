# Q1409: Loan NFT lock and nonce state: buyer settlement / nonce miss / nonce fidelity

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with an exchange-driven transfer by the active unlocker into a buyer-controlled address while the token starts unlocked with a live owner and possibly a per-token approval and make an ownership-set change fail to produce the nonce signal a downstream contract expects, breaking the rule that every ownership-set change should bump ownershipNonce for the affected addresses exactly once and leading to Unintended or unfair reassignment of loan cashflow rights?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: an exchange-driven transfer by the active unlocker into a buyer-controlled address
- Exploit idea: make an ownership-set change fail to produce the nonce signal a downstream contract expects
- Invariant to test: every ownership-set change should bump ownershipNonce for the affected addresses exactly once
- Expected Immunefi impact: Unintended or unfair reassignment of loan cashflow rights
- Fast validation: Fuzz rapid ownership changes and ensure a vault-like observer would always detect the correct epoch boundaries.
