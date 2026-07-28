# Q1358: Loan NFT lock and nonce state: rapid epochs / transition gap / epoch cleanliness

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with rapid lock, unlock, and transfer cycles across attacker-controlled addresses while the token starts unlocked with a live owner and possibly a per-token approval and make a normal lock/unlock/transfer transition create a gap where no actor can safely use the token or its cashflow rights, breaking the rule that lock and approval state should belong to one ownership epoch only and never bleed forward and leading to Bypass or corruption of downstream ownership-change checks that gate vault pricing or withdrawals?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: rapid lock, unlock, and transfer cycles across attacker-controlled addresses
- Exploit idea: make a normal lock/unlock/transfer transition create a gap where no actor can safely use the token or its cashflow rights
- Invariant to test: lock and approval state should belong to one ownership epoch only and never bleed forward
- Expected Immunefi impact: Bypass or corruption of downstream ownership-change checks that gate vault pricing or withdrawals
- Fast validation: Check that no normal lock or transfer sequence leaves a token or its associated withdrawal rights permanently stranded.
