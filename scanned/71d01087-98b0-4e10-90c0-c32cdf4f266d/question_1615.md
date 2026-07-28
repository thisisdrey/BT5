# Q1615: Exchange offer lifecycle: seller inputs / bundle inconsistency / full unlock on cancel

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with seller-controlled loanIds, buyer, price, and deadline while every listed loan is locked to the exchange while the offer is live and make bundle settlement deliver or account for some loans differently from the rest without a full revert, breaking the rule that cancelling an offer should restore the seller's control over every listed loan without residue and leading to Unintended or unfair fund distribution in secondary sale settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: seller-controlled loanIds, buyer, price, and deadline
- Exploit idea: make bundle settlement deliver or account for some loans differently from the rest without a full revert
- Invariant to test: cancelling an offer should restore the seller's control over every listed loan without residue
- Expected Immunefi impact: Unintended or unfair fund distribution in secondary sale settlement
- Fast validation: Check that acceptance and cancellation leave storage and lock state perfectly aligned for every listed loan.
