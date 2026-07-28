# Q1709: Exchange offer lifecycle: accept timing / bundle inconsistency / one live offer state

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer-controlled acceptance timing near expiry or around other state changes while the offer contains multiple loans whose economic state can diverge while storage stays static and make bundle settlement deliver or account for some loans differently from the rest without a full revert, breaking the rule that an active offer should have exactly one live storage record and one matching lock state for every listed loan and leading to Bypass of intended permissioning for who may settle a directed offer?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer-controlled acceptance timing near expiry or around other state changes
- Exploit idea: make bundle settlement deliver or account for some loans differently from the rest without a full revert
- Invariant to test: an active offer should have exactly one live storage record and one matching lock state for every listed loan
- Expected Immunefi impact: Bypass of intended permissioning for who may settle a directed offer
- Fast validation: Model cashflow accrual during an active offer and assert settlement cannot mix old storage with new economic state improperly.
