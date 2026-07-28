# Q1813: Exchange offer lifecycle: address-book churn / lock residue / one live offer state

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer and seller each changing only their own address-book registrations around acceptance while the deadline is close enough that ordering and timestamp boundaries matter and make cancellation or acceptance leave one of the listed loans locked after the storage record is gone, breaking the rule that an active offer should have exactly one live storage record and one matching lock state for every listed loan and leading to Unintended or unfair fund distribution in secondary sale settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer and seller each changing only their own address-book registrations around acceptance
- Exploit idea: make cancellation or acceptance leave one of the listed loans locked after the storage record is gone
- Invariant to test: an active offer should have exactly one live storage record and one matching lock state for every listed loan
- Expected Immunefi impact: Unintended or unfair fund distribution in secondary sale settlement
- Fast validation: Simulate cancel/relist cycles and ensure no listed loan remains locked or economically bound to a dead offer.
