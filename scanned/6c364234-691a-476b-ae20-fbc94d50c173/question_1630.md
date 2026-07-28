# Q1630: Exchange offer lifecycle: seller inputs / bundle inconsistency / all-or-nothing settlement

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with seller-controlled loanIds, buyer, price, and deadline while the deadline is close enough that ordering and timestamp boundaries matter and make bundle settlement deliver or account for some loans differently from the rest without a full revert, breaking the rule that acceptance should either deliver every listed NFT and the agreed cash or revert cleanly and leading to Unintended or unfair fund distribution in secondary sale settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: seller-controlled loanIds, buyer, price, and deadline
- Exploit idea: make bundle settlement deliver or account for some loans differently from the rest without a full revert
- Invariant to test: acceptance should either deliver every listed NFT and the agreed cash or revert cleanly
- Expected Immunefi impact: Unintended or unfair fund distribution in secondary sale settlement
- Fast validation: Simulate cancel/relist cycles and ensure no listed loan remains locked or economically bound to a dead offer.
