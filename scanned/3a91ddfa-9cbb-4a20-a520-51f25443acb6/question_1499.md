# Q1499: Loan NFT lock and nonce state: contract holder / mixed read / paired state read

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with an attacker-controlled contract address as the new loan holder while the token is currently locked to a non-zero unlocker and make downstream code observe an owner and unlocker pair that belong to different epochs, breaking the rule that ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs and leading to Loans NFT being stuck or its cashflow rights becoming unavailable?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: an attacker-controlled contract address as the new loan holder
- Exploit idea: make downstream code observe an owner and unlocker pair that belong to different epochs
- Invariant to test: ownerAndUnlocker should never expose a mixed owner/unlocker view across epochs
- Expected Immunefi impact: Loans NFT being stuck or its cashflow rights becoming unavailable
- Fast validation: Fuzz rapid ownership changes and ensure a vault-like observer would always detect the correct epoch boundaries.
