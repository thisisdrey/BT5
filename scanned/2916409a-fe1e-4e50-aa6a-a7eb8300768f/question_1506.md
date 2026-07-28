# Q1506: Loan NFT lock and nonce state: contract holder / nonce miss / epoch cleanliness

## Question
Can an unprivileged current owner, approved address, buyer, or seller using ordinary NFT actions enter through `LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom` with an attacker-controlled contract address as the new loan holder while the token was just unlocked after a prior listing or transfer cycle and make an ownership-set change fail to produce the nonce signal a downstream contract expects, breaking the rule that lock and approval state should belong to one ownership epoch only and never bleed forward and leading to Unintended or unfair reassignment of loan cashflow rights?

## Target
- File/function: contracts/LoansNFT.sol / lock, unlock, _update
- Entrypoint: LoansNFT.lock(address,uint256), unlock(uint256), transferFrom/safeTransferFrom
- Attacker controls: an attacker-controlled contract address as the new loan holder
- Exploit idea: make an ownership-set change fail to produce the nonce signal a downstream contract expects
- Invariant to test: lock and approval state should belong to one ownership epoch only and never bleed forward
- Expected Immunefi impact: Unintended or unfair reassignment of loan cashflow rights
- Fast validation: Fuzz rapid ownership changes and ensure a vault-like observer would always detect the correct epoch boundaries.
