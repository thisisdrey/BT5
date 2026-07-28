# Q1793: Exchange offer lifecycle: address-book churn / stale snapshot / one live offer state

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer and seller each changing only their own address-book registrations around acceptance while every listed loan is locked to the exchange while the offer is live and make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way, breaking the rule that an active offer should have exactly one live storage record and one matching lock state for every listed loan and leading to Cross-user cashflow or ownership reassignment during exchange settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer and seller each changing only their own address-book registrations around acceptance
- Exploit idea: make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way
- Invariant to test: an active offer should have exactly one live storage record and one matching lock state for every listed loan
- Expected Immunefi impact: Cross-user cashflow or ownership reassignment during exchange settlement
- Fast validation: Simulate cancel/relist cycles and ensure no listed loan remains locked or economically bound to a dead offer.
