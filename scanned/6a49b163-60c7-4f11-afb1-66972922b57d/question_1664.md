# Q1664: Exchange offer lifecycle: seller inputs / bundle inconsistency / exact counterparties

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with seller-controlled loanIds, buyer, price, and deadline while one side is a vault or later counterparty that relies on precise lock cleanup and delivery semantics and make bundle settlement deliver or account for some loans differently from the rest without a full revert, breaking the rule that mutual investor-registration checks should bind the exact buyer and seller that receive settlement value and leading to Unintended or unfair fund distribution in secondary sale settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: seller-controlled loanIds, buyer, price, and deadline
- Exploit idea: make bundle settlement deliver or account for some loans differently from the rest without a full revert
- Invariant to test: mutual investor-registration checks should bind the exact buyer and seller that receive settlement value
- Expected Immunefi impact: Unintended or unfair fund distribution in secondary sale settlement
- Fast validation: Simulate cancel/relist cycles and ensure no listed loan remains locked or economically bound to a dead offer.
