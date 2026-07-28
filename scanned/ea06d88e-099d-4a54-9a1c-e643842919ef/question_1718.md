# Q1718: Exchange offer lifecycle: accept timing / lock residue / all-or-nothing settlement

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer-controlled acceptance timing near expiry or around other state changes while one side is a vault or later counterparty that relies on precise lock cleanup and delivery semantics and make cancellation or acceptance leave one of the listed loans locked after the storage record is gone, breaking the rule that acceptance should either deliver every listed NFT and the agreed cash or revert cleanly and leading to Bypass of intended permissioning for who may settle a directed offer?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer-controlled acceptance timing near expiry or around other state changes
- Exploit idea: make cancellation or acceptance leave one of the listed loans locked after the storage record is gone
- Invariant to test: acceptance should either deliver every listed NFT and the agreed cash or revert cleanly
- Expected Immunefi impact: Bypass of intended permissioning for who may settle a directed offer
- Fast validation: Forge test directed offers with multiple loans, expiry edges, and registration churn, then assert full delivery or full revert.
