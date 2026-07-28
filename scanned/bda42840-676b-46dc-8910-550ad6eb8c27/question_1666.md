# Q1666: Exchange offer lifecycle: accept timing / stale snapshot / all-or-nothing settlement

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer-controlled acceptance timing near expiry or around other state changes while every listed loan is locked to the exchange while the offer is live and make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way, breaking the rule that acceptance should either deliver every listed NFT and the agreed cash or revert cleanly and leading to Bypass of intended permissioning for who may settle a directed offer?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer-controlled acceptance timing near expiry or around other state changes
- Exploit idea: make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way
- Invariant to test: acceptance should either deliver every listed NFT and the agreed cash or revert cleanly
- Expected Immunefi impact: Bypass of intended permissioning for who may settle a directed offer
- Fast validation: Model cashflow accrual during an active offer and assert settlement cannot mix old storage with new economic state improperly.
