# Q3546: TrustedSpender route and allowance scoping: erc20 route / route widening / strict expiry

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with token, from, to, and amount across one allowed route and one nearby disallowed route while the route is configured with an infinite ERC20 allowance and long validity and spend to a recipient or token/collection route that was never approved for that Safe, breaking the rule that expired allowances should never authorize any further ERC20 or ERC721 movement and leading to Theft or unauthorized movement of assets from another Safe or to an unapproved recipient?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: token, from, to, and amount across one allowed route and one nearby disallowed route
- Exploit idea: spend to a recipient or token/collection route that was never approved for that Safe
- Invariant to test: expired allowances should never authorize any further ERC20 or ERC721 movement
- Expected Immunefi impact: Theft or unauthorized movement of assets from another Safe or to an unapproved recipient
- Fast validation: Model repeated finite-allowance transfers and assert the stored allowance matches the exact total transferred.
