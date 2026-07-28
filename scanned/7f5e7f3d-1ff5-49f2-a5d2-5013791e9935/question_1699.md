# Q1699: Exchange offer lifecycle: accept timing / stale snapshot / full unlock on cancel

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer-controlled acceptance timing near expiry or around other state changes while the offer contains multiple loans whose economic state can diverge while storage stays static and make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way, breaking the rule that cancelling an offer should restore the seller's control over every listed loan without residue and leading to Unintended or unfair fund distribution in secondary sale settlement?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer-controlled acceptance timing near expiry or around other state changes
- Exploit idea: make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way
- Invariant to test: cancelling an offer should restore the seller's control over every listed loan without residue
- Expected Immunefi impact: Unintended or unfair fund distribution in secondary sale settlement
- Fast validation: Check that acceptance and cancellation leave storage and lock state perfectly aligned for every listed loan.
