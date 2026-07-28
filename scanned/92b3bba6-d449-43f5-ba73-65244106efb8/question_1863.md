# Q1863: Exchange offer lifecycle: cashflow timing / lock residue / full unlock on cancel

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with listed loans that accrue or collect cashflows while the offer remains active while every listed loan is locked to the exchange while the offer is live and make cancellation or acceptance leave one of the listed loans locked after the storage record is gone, breaking the rule that cancelling an offer should restore the seller's control over every listed loan without residue and leading to Cross-user cashflow or ownership reassignment during exchange settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: listed loans that accrue or collect cashflows while the offer remains active
- Exploit idea: make cancellation or acceptance leave one of the listed loans locked after the storage record is gone
- Invariant to test: cancelling an offer should restore the seller's control over every listed loan without residue
- Expected Immunefi impact: Cross-user cashflow or ownership reassignment during exchange settlement
- Fast validation: Simulate cancel/relist cycles and ensure no listed loan remains locked or economically bound to a dead offer.
