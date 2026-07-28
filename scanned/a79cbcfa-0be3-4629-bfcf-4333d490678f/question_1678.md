# Q1678: Exchange offer lifecycle: accept timing / bundle inconsistency / all-or-nothing settlement

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer-controlled acceptance timing near expiry or around other state changes while every listed loan is locked to the exchange while the offer is live and make bundle settlement deliver or account for some loans differently from the rest without a full revert, breaking the rule that acceptance should either deliver every listed NFT and the agreed cash or revert cleanly and leading to Unintended or unfair fund distribution in secondary sale settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer-controlled acceptance timing near expiry or around other state changes
- Exploit idea: make bundle settlement deliver or account for some loans differently from the rest without a full revert
- Invariant to test: acceptance should either deliver every listed NFT and the agreed cash or revert cleanly
- Expected Immunefi impact: Unintended or unfair fund distribution in secondary sale settlement
- Fast validation: Check that acceptance and cancellation leave storage and lock state perfectly aligned for every listed loan.
