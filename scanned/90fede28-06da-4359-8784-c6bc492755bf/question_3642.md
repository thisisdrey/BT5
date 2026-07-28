# Q3642: TrustedSpender route and allowance scoping: expiry edge / route widening / strict expiry

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with transfer timing exactly around allowance expiry boundaries while another Safe has a similar delegate or recipient topology but no matching route and spend to a recipient or token/collection route that was never approved for that Safe, breaking the rule that expired allowances should never authorize any further ERC20 or ERC721 movement and leading to Protocol-facing unauthorized transfers that later harm another user or vault?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: transfer timing exactly around allowance expiry boundaries
- Exploit idea: spend to a recipient or token/collection route that was never approved for that Safe
- Invariant to test: expired allowances should never authorize any further ERC20 or ERC721 movement
- Expected Immunefi impact: Protocol-facing unauthorized transfers that later harm another user or vault
- Fast validation: Check that neighboring recipients, collections, or tokens cannot be reached through a superficially similar approved route.
