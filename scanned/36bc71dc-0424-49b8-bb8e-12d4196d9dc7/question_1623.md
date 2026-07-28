# Q1623: Exchange offer lifecycle: seller inputs / lock residue / full unlock on cancel

## Question
Can an unprivileged seller or designated buyer using only normal exchange entrypoints enter through `LoansExchange.createOffer/acceptOffer/cancelOffer` with seller-controlled loanIds, buyer, price, and deadline while the deadline is close enough that ordering and timestamp boundaries matter and make cancellation or acceptance leave one of the listed loans locked after the storage record is gone, breaking the rule that cancelling an offer should restore the seller's control over every listed loan without residue and leading to Loans NFT being stuck or left under a stale exchange lock?

## Target
- File/function: contracts/LoansExchange.sol / createOffer -> acceptOffer -> cancelOffer
- Entrypoint: LoansExchange.createOffer/acceptOffer/cancelOffer
- Attacker controls: seller-controlled loanIds, buyer, price, and deadline
- Exploit idea: make cancellation or acceptance leave one of the listed loans locked after the storage record is gone
- Invariant to test: cancelling an offer should restore the seller's control over every listed loan without residue
- Expected Immunefi impact: Loans NFT being stuck or left under a stale exchange lock
- Fast validation: Model cashflow accrual during an active offer and assert settlement cannot mix old storage with new economic state improperly.
