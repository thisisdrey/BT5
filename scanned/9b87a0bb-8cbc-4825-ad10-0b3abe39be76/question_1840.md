# Q1840: Exchange offer lifecycle: address-book churn / bundle inconsistency / exact counterparties

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer and seller each changing only their own address-book registrations around acceptance while the offer contains multiple loans whose economic state can diverge while storage stays static and make bundle settlement deliver or account for some loans differently from the rest without a full revert, breaking the rule that mutual investor-registration checks should bind the exact buyer and seller that receive settlement value and leading to Cross-user cashflow or ownership reassignment during exchange settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer and seller each changing only their own address-book registrations around acceptance
- Exploit idea: make bundle settlement deliver or account for some loans differently from the rest without a full revert
- Invariant to test: mutual investor-registration checks should bind the exact buyer and seller that receive settlement value
- Expected Immunefi impact: Cross-user cashflow or ownership reassignment during exchange settlement
- Fast validation: Simulate cancel/relist cycles and ensure no listed loan remains locked or economically bound to a dead offer.
