# Q1824: Exchange offer lifecycle: address-book churn / bundle inconsistency / exact counterparties

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer and seller each changing only their own address-book registrations around acceptance while the deadline is close enough that ordering and timestamp boundaries matter and make bundle settlement deliver or account for some loans differently from the rest without a full revert, breaking the rule that mutual investor-registration checks should bind the exact buyer and seller that receive settlement value and leading to Bypass of intended permissioning for who may settle a directed offer?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer and seller each changing only their own address-book registrations around acceptance
- Exploit idea: make bundle settlement deliver or account for some loans differently from the rest without a full revert
- Invariant to test: mutual investor-registration checks should bind the exact buyer and seller that receive settlement value
- Expected Immunefi impact: Bypass of intended permissioning for who may settle a directed offer
- Fast validation: Forge test directed offers with multiple loans, expiry edges, and registration churn, then assert full delivery or full revert.
