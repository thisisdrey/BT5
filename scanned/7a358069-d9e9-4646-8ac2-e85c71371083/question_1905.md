# Q1905: Exchange offer lifecycle: cashflow timing / stale snapshot / one live offer state

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with listed loans that accrue or collect cashflows while the offer remains active while one side is a vault or later counterparty that relies on precise lock cleanup and delivery semantics and make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way, breaking the rule that an active offer should have exactly one live storage record and one matching lock state for every listed loan and leading to Cross-user cashflow or ownership reassignment during exchange settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: listed loans that accrue or collect cashflows while the offer remains active
- Exploit idea: make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way
- Invariant to test: an active offer should have exactly one live storage record and one matching lock state for every listed loan
- Expected Immunefi impact: Cross-user cashflow or ownership reassignment during exchange settlement
- Fast validation: Check that acceptance and cancellation leave storage and lock state perfectly aligned for every listed loan.
