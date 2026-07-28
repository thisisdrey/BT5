# Q1466: Loan NFT lock and nonce state: buyer settlement / mixed read / epoch cleanliness

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with an exchange-driven transfer by the active unlocker into a buyer-controlled address while another contract or process will read ownerAndUnlocker or ownershipNonce immediately after the action and make downstream code observe an owner and unlocker pair that belong to different epochs, breaking the rule that lock and approval state should belong to one ownership epoch only and never bleed forward and leading to Loans NFT being stuck or its cashflow rights becoming unavailable?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: an exchange-driven transfer by the active unlocker into a buyer-controlled address
- Exploit idea: make downstream code observe an owner and unlocker pair that belong to different epochs
- Invariant to test: lock and approval state should belong to one ownership epoch only and never bleed forward
- Expected Immunefi impact: Loans NFT being stuck or its cashflow rights becoming unavailable
- Fast validation: Fuzz rapid ownership changes and ensure a vault-like observer would always detect the correct epoch boundaries.
