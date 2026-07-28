# Q1814: Exchange offer lifecycle: address-book churn / lock residue / all-or-nothing settlement

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with buyer and seller each changing only their own address-book registrations around acceptance while the deadline is close enough that ordering and timestamp boundaries matter and make cancellation or acceptance leave one of the listed loans locked after the storage record is gone, breaking the rule that acceptance should either deliver every listed NFT and the agreed cash or revert cleanly and leading to Bypass of intended permissioning for who may settle a directed offer?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: buyer and seller each changing only their own address-book registrations around acceptance
- Exploit idea: make cancellation or acceptance leave one of the listed loans locked after the storage record is gone
- Invariant to test: acceptance should either deliver every listed NFT and the agreed cash or revert cleanly
- Expected Immunefi impact: Bypass of intended permissioning for who may settle a directed offer
- Fast validation: Forge test directed offers with multiple loans, expiry edges, and registration churn, then assert full delivery or full revert.
