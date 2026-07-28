# Q1457: Loan NFT lock and nonce state: buyer settlement / nonce miss / nonce fidelity

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with an exchange-driven transfer by the active unlocker into a buyer-controlled address while another contract or process will read ownerAndUnlocker or ownershipNonce immediately after the action and make an ownership-set change fail to produce the nonce signal a downstream contract expects, breaking the rule that every ownership-set change should bump ownershipNonce for the affected addresses exactly once and leading to Bypass or corruption of downstream ownership-change checks that gate vault pricing or withdrawals?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: an exchange-driven transfer by the active unlocker into a buyer-controlled address
- Exploit idea: make an ownership-set change fail to produce the nonce signal a downstream contract expects
- Invariant to test: every ownership-set change should bump ownershipNonce for the affected addresses exactly once
- Expected Immunefi impact: Bypass or corruption of downstream ownership-change checks that gate vault pricing or withdrawals
- Fast validation: Simulate exchange settlement and immediate downstream reads, then assert the post-transfer state contains no stale unlocker or missed nonce bump.
