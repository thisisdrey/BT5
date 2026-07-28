# Q1677: Exchange offer lifecycle: accept timing / bundle inconsistency / one live offer state

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer-controlled acceptance timing near expiry or around other state changes while every listed loan is locked to the exchange while the offer is live and make bundle settlement deliver or account for some loans differently from the rest without a full revert, breaking the rule that an active offer should have exactly one live storage record and one matching lock state for every listed loan and leading to Loans NFT being stuck or left under a stale exchange lock?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer-controlled acceptance timing near expiry or around other state changes
- Exploit idea: make bundle settlement deliver or account for some loans differently from the rest without a full revert
- Invariant to test: an active offer should have exactly one live storage record and one matching lock state for every listed loan
- Expected Immunefi impact: Loans NFT being stuck or left under a stale exchange lock
- Fast validation: Forge test directed offers with multiple loans, expiry edges, and registration churn, then assert full delivery or full revert.
