# Q1860: Exchange offer lifecycle: cashflow timing / stale snapshot / exact counterparties

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with listed loans that accrue or collect cashflows while the offer remains active while every listed loan is locked to the exchange while the offer is live and make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way, breaking the rule that mutual investor-registration checks should bind the exact buyer and seller that receive settlement value and leading to Cross-user cashflow or ownership reassignment during exchange settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: listed loans that accrue or collect cashflows while the offer remains active
- Exploit idea: make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way
- Invariant to test: mutual investor-registration checks should bind the exact buyer and seller that receive settlement value
- Expected Immunefi impact: Cross-user cashflow or ownership reassignment during exchange settlement
- Fast validation: Simulate cancel/relist cycles and ensure no listed loan remains locked or economically bound to a dead offer.
