# Q1552: Loan NFT lock and nonce state: nonce observer / transition gap / safe transition

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with a downstream vault or observer that relies on ownershipNonce and ownerAndUnlocker after user-controlled transfers while the token starts unlocked with a live owner and possibly a per-token approval and make a normal lock/unlock/transfer transition create a gap where no actor can safely use the token or its cashflow rights, breaking the rule that ordinary lock, unlock, and transfer transitions should not create a durable stuck state for the token or its cashflow routing and leading to Unintended or unfair reassignment of loan cashflow rights?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: a downstream vault or observer that relies on ownershipNonce and ownerAndUnlocker after user-controlled transfers
- Exploit idea: make a normal lock/unlock/transfer transition create a gap where no actor can safely use the token or its cashflow rights
- Invariant to test: ordinary lock, unlock, and transfer transitions should not create a durable stuck state for the token or its cashflow routing
- Expected Immunefi impact: Unintended or unfair reassignment of loan cashflow rights
- Fast validation: Fuzz rapid ownership changes and ensure a vault-like observer would always detect the correct epoch boundaries.
