# Q1815: Exchange offer lifecycle: address-book churn / lock residue / full unlock on cancel

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer and seller each changing only their own address-book registrations around acceptance while the deadline is close enough that ordering and timestamp boundaries matter and make cancellation or acceptance leave one of the listed loans locked after the storage record is gone, breaking the rule that cancelling an offer should restore the seller's control over every listed loan without residue and leading to Cross-user cashflow or ownership reassignment during exchange settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer and seller each changing only their own address-book registrations around acceptance
- Exploit idea: make cancellation or acceptance leave one of the listed loans locked after the storage record is gone
- Invariant to test: cancelling an offer should restore the seller's control over every listed loan without residue
- Expected Immunefi impact: Cross-user cashflow or ownership reassignment during exchange settlement
- Fast validation: Check that acceptance and cancellation leave storage and lock state perfectly aligned for every listed loan.
