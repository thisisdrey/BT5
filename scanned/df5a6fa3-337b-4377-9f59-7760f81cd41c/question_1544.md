# Q1544: Loan NFT lock and nonce state: nonce observer / epoch residue / safe transition

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with a downstream vault or observer that relies on ownershipNonce and ownerAndUnlocker after user-controlled transfers while the token starts unlocked with a live owner and possibly a per-token approval and make a lock or approval residue survive into the next ownership epoch, breaking the rule that ordinary lock, unlock, and transfer transitions should not create a durable stuck state for the token or its cashflow routing and leading to Loans NFT being stuck or its cashflow rights becoming unavailable?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: a downstream vault or observer that relies on ownershipNonce and ownerAndUnlocker after user-controlled transfers
- Exploit idea: make a lock or approval residue survive into the next ownership epoch
- Invariant to test: ordinary lock, unlock, and transfer transitions should not create a durable stuck state for the token or its cashflow routing
- Expected Immunefi impact: Loans NFT being stuck or its cashflow rights becoming unavailable
- Fast validation: Forge test per-token approvals, lock/unlock cycles, and transfers, then assert nonce, owner, and unlocker views stay in one epoch.
