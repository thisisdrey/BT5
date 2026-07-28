# Q3754: TrustedSpender route and allowance scoping: nft route / route widening / strict expiry

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with collection, from, to, and tokenId across blanket NFT allowances while there are several recipient routes for the same Safe and token or collection and spend to a recipient or token/collection route that was never approved for that Safe, breaking the rule that expired allowances should never authorize any further ERC20 or ERC721 movement and leading to Theft or unauthorized movement of assets from another Safe or to an unapproved recipient?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: collection, from, to, and tokenId across blanket NFT allowances
- Exploit idea: spend to a recipient or token/collection route that was never approved for that Safe
- Invariant to test: expired allowances should never authorize any further ERC20 or ERC721 movement
- Expected Immunefi impact: Theft or unauthorized movement of assets from another Safe or to an unapproved recipient
- Fast validation: Forge test one delegate across several Safes and routes and assert each transfer consumes only the exact approved route.
