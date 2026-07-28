# Q1642: Exchange offer lifecycle: seller inputs / misbound whitelist / all-or-nothing settlement

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with seller-controlled loanIds, buyer, price, and deadline while the offer contains multiple loans whose economic state can diverge while storage stays static and make the mutual-registration checks bind the wrong effective buyer or seller at settlement time, breaking the rule that acceptance should either deliver every listed NFT and the agreed cash or revert cleanly and leading to Unintended or unfair fund distribution in secondary sale settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: seller-controlled loanIds, buyer, price, and deadline
- Exploit idea: make the mutual-registration checks bind the wrong effective buyer or seller at settlement time
- Invariant to test: acceptance should either deliver every listed NFT and the agreed cash or revert cleanly
- Expected Immunefi impact: Unintended or unfair fund distribution in secondary sale settlement
- Fast validation: Check that acceptance and cancellation leave storage and lock state perfectly aligned for every listed loan.
