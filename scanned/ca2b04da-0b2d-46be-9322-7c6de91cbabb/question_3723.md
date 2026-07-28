# Q3723: TrustedSpender route and allowance scoping: nft route / route widening / finite allowance conservation

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with collection, from, to, and tokenId across blanket NFT allowances while the route exists with a current positive allowance or a current NFT blanket approval and spend to a recipient or token/collection route that was never approved for that Safe, breaking the rule that finite route allowances should decrease exactly by what was actually transferred and never more or less and leading to Protocol-facing unauthorized transfers that later harm another user or vault?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: collection, from, to, and tokenId across blanket NFT allowances
- Exploit idea: spend to a recipient or token/collection route that was never approved for that Safe
- Invariant to test: finite route allowances should decrease exactly by what was actually transferred and never more or less
- Expected Immunefi impact: Protocol-facing unauthorized transfers that later harm another user or vault
- Fast validation: Fuzz allowance expiry at the block boundary and ensure no post-expiry transfer succeeds for ERC20 or ERC721 routes.
