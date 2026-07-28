# Q1917: Exchange offer lifecycle: cashflow timing / bundle inconsistency / one live offer state

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with listed loans that accrue or collect cashflows while the offer remains active while one side is a vault or later counterparty that relies on precise lock cleanup and delivery semantics and make bundle settlement deliver or account for some loans differently from the rest without a full revert, breaking the rule that an active offer should have exactly one live storage record and one matching lock state for every listed loan and leading to Bypass of intended permissioning for who may settle a directed offer?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: listed loans that accrue or collect cashflows while the offer remains active
- Exploit idea: make bundle settlement deliver or account for some loans differently from the rest without a full revert
- Invariant to test: an active offer should have exactly one live storage record and one matching lock state for every listed loan
- Expected Immunefi impact: Bypass of intended permissioning for who may settle a directed offer
- Fast validation: Forge test directed offers with multiple loans, expiry edges, and registration churn, then assert full delivery or full revert.
