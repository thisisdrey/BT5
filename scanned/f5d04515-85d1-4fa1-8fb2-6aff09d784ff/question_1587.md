# Q1587: Loan NFT lock and nonce state: nonce observer / nonce miss / paired state read

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with a downstream vault or observer that relies on ownershipNonce and ownerAndUnlocker after user-controlled transfers while another contract or process will read ownerAndUnlocker or ownershipNonce immediately after the action and make an ownership-set change fail to produce the nonce signal a downstream contract expects, breaking the rule that ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs and leading to Bypass or corruption of downstream ownership-change checks that gate vault pricing or withdrawals?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: a downstream vault or observer that relies on ownershipNonce and ownerAndUnlocker after user-controlled transfers
- Exploit idea: make an ownership-set change fail to produce the nonce signal a downstream contract expects
- Invariant to test: ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs
- Expected Immunefi impact: Bypass or corruption of downstream ownership-change checks that gate vault pricing or withdrawals
- Fast validation: Simulate exchange settlement and immediate downstream reads, then assert the post-transfer state contains no stale unlocker or missed nonce bump.
