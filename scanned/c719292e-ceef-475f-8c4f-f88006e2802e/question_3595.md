# Q3595: TrustedSpender route and allowance scoping: expiry edge / route widening / finite allowance conservation

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with transfer timing exactly around allowance expiry boundaries while the route exists with a current positive allowance or a current NFT blanket approval and spend to a recipient or token/collection route that was never approved for that Safe, breaking the rule that finite route allowances should decrease exactly by what was actually transferred and never more or less and leading to Bypass of intended permissions and allowance scoping?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: transfer timing exactly around allowance expiry boundaries
- Exploit idea: spend to a recipient or token/collection route that was never approved for that Safe
- Invariant to test: finite route allowances should decrease exactly by what was actually transferred and never more or less
- Expected Immunefi impact: Bypass of intended permissions and allowance scoping
- Fast validation: Check that neighboring recipients, collections, or tokens cannot be reached through a superficially similar approved route.
