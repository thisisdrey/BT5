# Q1698: Exchange offer lifecycle: accept timing / stale snapshot / all-or-nothing settlement

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer-controlled acceptance timing near expiry or around other state changes while the offer contains multiple loans whose economic state can diverge while storage stays static and make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way, breaking the rule that acceptance should either deliver every listed NFT and the agreed cash or revert cleanly and leading to Loans NFT being stuck or left under a stale exchange lock?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer-controlled acceptance timing near expiry or around other state changes
- Exploit idea: make acceptance settle against an offer snapshot whose effective economic state has already changed in a user-controlled way
- Invariant to test: acceptance should either deliver every listed NFT and the agreed cash or revert cleanly
- Expected Immunefi impact: Loans NFT being stuck or left under a stale exchange lock
- Fast validation: Forge test directed offers with multiple loans, expiry edges, and registration churn, then assert full delivery or full revert.
