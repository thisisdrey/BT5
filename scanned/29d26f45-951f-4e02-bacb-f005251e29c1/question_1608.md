# Q1608: Exchange offer lifecycle: seller inputs / lock residue / exact counterparties

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with seller-controlled loanIds, buyer, price, and deadline while every listed loan is locked to the exchange while the offer is live and make cancellation or acceptance leave one of the listed loans locked after the storage record is gone, breaking the rule that mutual investor-registration checks should bind the exact buyer and seller that receive settlement value and leading to Loans NFT being stuck or left under a stale exchange lock?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: seller-controlled loanIds, buyer, price, and deadline
- Exploit idea: make cancellation or acceptance leave one of the listed loans locked after the storage record is gone
- Invariant to test: mutual investor-registration checks should bind the exact buyer and seller that receive settlement value
- Expected Immunefi impact: Loans NFT being stuck or left under a stale exchange lock
- Fast validation: Forge test directed offers with multiple loans, expiry edges, and registration churn, then assert full delivery or full revert.
