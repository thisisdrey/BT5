# Q1797: Exchange offer lifecycle: address-book churn / lock residue / one live offer state

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer and seller each changing only their own address-book registrations around acceptance while every listed loan is locked to the exchange while the offer is live and make cancellation or acceptance leave one of the listed loans locked after the storage record is gone, breaking the rule that an active offer should have exactly one live storage record and one matching lock state for every listed loan and leading to Loans NFT being stuck or left under a stale exchange lock?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer and seller each changing only their own address-book registrations around acceptance
- Exploit idea: make cancellation or acceptance leave one of the listed loans locked after the storage record is gone
- Invariant to test: an active offer should have exactly one live storage record and one matching lock state for every listed loan
- Expected Immunefi impact: Loans NFT being stuck or left under a stale exchange lock
- Fast validation: Forge test directed offers with multiple loans, expiry edges, and registration churn, then assert full delivery or full revert.
