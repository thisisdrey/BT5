# Q1479: Loan NFT lock and nonce state: contract holder / epoch residue / paired state read

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with an attacker-controlled contract address as the new loan holder while the token starts unlocked with a live owner and possibly a per-token approval and make a lock or approval residue survive into the next ownership epoch, breaking the rule that ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs and leading to Unintended or unfair reassignment of loan cashflow rights?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: an attacker-controlled contract address as the new loan holder
- Exploit idea: make a lock or approval residue survive into the next ownership epoch
- Invariant to test: ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs
- Expected Immunefi impact: Unintended or unfair reassignment of loan cashflow rights
- Fast validation: Fuzz rapid ownership changes and ensure a vault-like observer would always detect the correct epoch boundaries.
