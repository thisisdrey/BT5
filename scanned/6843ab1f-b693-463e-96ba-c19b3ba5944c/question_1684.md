# Q1684: Exchange offer lifecycle: accept timing / stale snapshot / exact counterparties

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer-controlled acceptance timing near expiry or around other state changes while the deadline is close enough that ordering and timestamp boundaries matter and make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way, breaking the rule that mutual investor-registration checks should bind the exact buyer and seller that receive settlement value and leading to Unintended or unfair fund distribution in secondary sale settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer-controlled acceptance timing near expiry or around other state changes
- Exploit idea: make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way
- Invariant to test: mutual investor-registration checks should bind the exact buyer and seller that receive settlement value
- Expected Immunefi impact: Unintended or unfair fund distribution in secondary sale settlement
- Fast validation: Simulate cancel/relist cycles and ensure no listed loan remains locked or economically bound to a dead offer.
