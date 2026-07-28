# Q1627: Exchange offer lifecycle: seller inputs / misbound whitelist / full unlock on cancel

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with seller-controlled loanIds, buyer, price, and deadline while the deadline is close enough that ordering and timestamp boundaries matter and make the mutual-registration checks bind the wrong effective buyer or seller at settlement time, breaking the rule that cancelling an offer should restore the seller's control over every listed loan without residue and leading to Unintended or unfair fund distribution in secondary sale settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: seller-controlled loanIds, buyer, price, and deadline
- Exploit idea: make the mutual-registration checks bind the wrong effective buyer or seller at settlement time
- Invariant to test: cancelling an offer should restore the seller's control over every listed loan without residue
- Expected Immunefi impact: Unintended or unfair fund distribution in secondary sale settlement
- Fast validation: Simulate cancel/relist cycles and ensure no listed loan remains locked or economically bound to a dead offer.
