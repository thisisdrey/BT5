# Q1851: Exchange offer lifecycle: address-book churn / misbound whitelist / full unlock on cancel

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer and seller each changing only their own address-book registrations around acceptance while one side is a vault or later counterparty that relies on precise lock cleanup and delivery semantics and make the mutual-registration checks bind the wrong effective buyer or seller at settlement time, breaking the rule that cancelling an offer should restore the seller's control over every listed loan without residue and leading to Bypass of intended permissioning for who may settle a directed offer?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer and seller each changing only their own address-book registrations around acceptance
- Exploit idea: make the mutual-registration checks bind the wrong effective buyer or seller at settlement time
- Invariant to test: cancelling an offer should restore the seller's control over every listed loan without residue
- Expected Immunefi impact: Bypass of intended permissioning for who may settle a directed offer
- Fast validation: Forge test directed offers with multiple loans, expiry edges, and registration churn, then assert full delivery or full revert.
