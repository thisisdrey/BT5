# Q1464: Loan NFT lock and nonce state: buyer settlement / epoch residue / safe transition

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with an exchange-driven transfer by the active unlocker into a buyer-controlled address while another contract or process will read ownerAndUnlocker or ownershipNonce immediately after the action and make a lock or approval residue survive into the next ownership epoch, breaking the rule that ordinary lock, unlock, and transfer transitions should not create a durable stuck state for the token or its cashflow routing and leading to Bypass or corruption of downstream ownership-change checks that gate vault pricing or withdrawals?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: an exchange-driven transfer by the active unlocker into a buyer-controlled address
- Exploit idea: make a lock or approval residue survive into the next ownership epoch
- Invariant to test: ordinary lock, unlock, and transfer transitions should not create a durable stuck state for the token or its cashflow routing
- Expected Immunefi impact: Bypass or corruption of downstream ownership-change checks that gate vault pricing or withdrawals
- Fast validation: Simulate exchange settlement and immediate downstream reads, then assert the post-transfer state contains no stale unlocker or missed nonce bump.
