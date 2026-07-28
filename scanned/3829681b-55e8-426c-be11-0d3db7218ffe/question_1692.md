# Q1692: Exchange offer lifecycle: accept timing / misbound whitelist / exact counterparties

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer-controlled acceptance timing near expiry or around other state changes while the deadline is close enough that ordering and timestamp boundaries matter and make the mutual-registration checks bind the wrong effective buyer or seller at settlement time, breaking the rule that mutual investor-registration checks should bind the exact buyer and seller that receive settlement value and leading to Cross-user cashflow or ownership reassignment during exchange settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer-controlled acceptance timing near expiry or around other state changes
- Exploit idea: make the mutual-registration checks bind the wrong effective buyer or seller at settlement time
- Invariant to test: mutual investor-registration checks should bind the exact buyer and seller that receive settlement value
- Expected Immunefi impact: Cross-user cashflow or ownership reassignment during exchange settlement
- Fast validation: Check that acceptance and cancellation leave storage and lock state perfectly aligned for every listed loan.
