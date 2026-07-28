# Q1894: Exchange offer lifecycle: cashflow timing / lock residue / all-or-nothing settlement

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with listed loans that accrue or collect cashflows while the offer remains active while the offer contains multiple loans whose economic state can diverge while storage stays static and make cancellation or acceptance leave one of the listed loans locked after the storage record is gone, breaking the rule that acceptance should either deliver every listed NFT and the agreed cash or revert cleanly and leading to Loans NFT being stuck or left under a stale exchange lock?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: listed loans that accrue or collect cashflows while the offer remains active
- Exploit idea: make cancellation or acceptance leave one of the listed loans locked after the storage record is gone
- Invariant to test: acceptance should either deliver every listed NFT and the agreed cash or revert cleanly
- Expected Immunefi impact: Loans NFT being stuck or left under a stale exchange lock
- Fast validation: Forge test directed offers with multiple loans, expiry edges, and registration churn, then assert full delivery or full revert.
