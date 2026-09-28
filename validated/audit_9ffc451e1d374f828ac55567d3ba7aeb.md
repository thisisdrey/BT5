### Title
Bridged transfers accepted by the source chain can permanently revert on the destination chain, leaving burned synths inaccessible on both chains - (File: contracts/SyntheticToken.sol)

### Summary
`ProxyOFT._debitFrom` burns the synthetic token on the source chain after checking only the *source-side* outbound cap (`maxBridgedOutSupply`), while `ProxyOFT._creditTo` mints on the destination chain after checking independently-configured *destination-side* caps (`maxBridgedInSupply`, `maxTotalSupply`) and the token's `isActive` flag. Because the egress validation and the ingress validation are not synchronized, a `sendFrom` that succeeds on chain A can be a message that always reverts on chain B, locking the burned funds.

### Finding Description
In `ProxyOFT._debitFrom` (contracts/ProxyOFT.sol:70-83), a public `sendFrom` call burns `amount_` via `syntheticToken.burn(...)`, which in `SyntheticToken._burn` only enforces `bridgedOutSupply() > maxBridgedOutSupply` on that chain (contracts/SyntheticToken.sol:275-277).

On the destination chain, `OFTCoreUpgradeable._sendAck` → `ProxyOFT._creditTo` calls `syntheticToken.mint(toAddress_, amount_)` (contracts/ProxyOFT.sol:86-93), which in `SyntheticToken._mint` enforces three extra conditions that the source never checks (contracts/SyntheticToken.sol:333-347):
- `bridgedInSupply() > maxBridgedInSupply` → `SurpassMaxBridgingSupply`
- `totalSupply > maxTotalSupply` → `SurpassMaxSynthSupply`
- `onlyIfSyntheticTokenIsActive` → `SyntheticIsInactive` if the synth is disabled on the destination

These caps are per-chain storage set independently by governance (deployments show divergent values, e.g. msUSD `maxBridgedInSupply` 35M on Base vs 10M inbound caps elsewhere). The counters are also asymmetric: `bridgedOutSupply = totalBridgedOut - totalBridgedIn` on the source vs `bridgedInSupply = totalBridgedIn - totalBridgedOut` on the destination, so there is no shared bound guaranteeing that what the source accepts the destination will accept.

When the destination mint reverts, `NonblockingLzAppUpgradeable.lzReceive` catches the revert and stores the payload in `failedMessages`; `retryMessage` re-executes `_nonblockingLzReceive` but will keep reverting on the same cap/inactive check, so the tokens are burned on the source and unmintable on the destination.

### Impact Explanation
User funds are burned on the source chain and cannot be credited on the destination chain — the synth becomes inaccessible on both layers. Recovery requires governance to raise `maxBridgedInSupply`/`maxTotalSupply` or re-enable the token on the destination chain before a retry can succeed (mirroring the UMA report's downgrade: not strictly trapped, but frozen pending admin action).

### Likelihood Explanation
Any unprivileged user can trigger this with a plain `sendFrom` whenever the destination's net bridged-in supply is within `amount_` of its cap, or the synth is inactive on the destination while still burnable on the source (`_burn` has no `onlyIfSyntheticTokenIsActive` check). On congested bridge routes or asymmetric cap configs (which the deployment tests show exist), a user bridging the gap amount lands exactly in the dead zone.

### Recommendation
Synchronize ingress/egress validation: either check the destination's remaining inbound capacity off-chain before allowing the burn (cannot be enforced atomically on-chain), or — practically — make the destination credit path non-reverting for cap overflow (e.g., remove `maxBridgedInSupply` from `_mint` when called by `proxyOFT`, since the source-side burn already guarantees global supply conservation), and apply `onlyIfSyntheticTokenIsActive` symmetrically to burn so a disabled synth cannot be bridged out at all.

### Proof of Concept
Extend `test/foundry/CrossChainsPlasmaEth.t.sol`:

```solidity
function test_sendFrom_revertsOnDestinationCap() public {
    vm.selectFork(ethFork);
    uint256 amount = 1e18;
    uint16 dstChainId = LZ_PLASMA_CHAIN_ID;

    deal(msUSD_eth, alice, amount);
    uint256 fee = msUSD_proxyOFT_eth.estimateSendFee(dstChainId, alice, amount);
    deal(alice, fee);

    // On Plasma: pin inbound cap so mint will revert
    vm.selectFork(plasmaFork);
    SyntheticToken synth = SyntheticToken(msUSD_plasma);
    vm.prank(poolRegistry_plasma.governor());
    synth.updateMaxBridgedInSupply(synth.bridgedInSupply()); // any inbound amount now overflows

    // Source side succeeds: burn is allowed
    vm.selectFork(ethFork);
    vm.prank(alice);
    msUSD_proxyOFT_eth.sendFrom{value: fee}(alice, dstChainId, alice, amount);
    assertEq(IERC20(msUSD_eth).balanceOf(alice), 0); // burned

    // Destination delivery: _creditTo -> mint reverts, payload stored as failed
    vm.selectFork(plasmaFork);
    vm.startPrank(lzEndpoint_plasma.defaultReceiveLibraryAddress());
    lzEndpoint_plasma.receivePayload(
        LZ_ETH_CHAIN_ID,
        abi.encodePacked(address(msUSD_proxyOFT_eth), address(msUSD_proxyOFT_plasma)),
        address(msUSD_proxyOFT_plasma),
        lzEndpoint_plasma.getInboundNonce(LZ_ETH_CHAIN_ID, abi.encode(address(msUSD_proxyOFT_eth))) + 1,
        SIMPLE_TRANSFER_GAS,
        abi.encode(PT_SEND, abi.encodePacked(alice), amount)
    );
    vm.stopPrank();

    // Tokens neither minted on destination nor recoverable on source
    assertEq(IERC20(msUSD_plasma).balanceOf(alice), 0);
    // retryMessage against the stored failed payload reverts with SurpassMaxBridgingSupply
}
```

The existing suite already proves the symmetric source-side revert (`SurpassMaxBridgingSupply` on `sendFrom` when `maxBridgedOutSupply` is pinned, test/E2E.base.test.ts:603-629); the missing piece is that the destination applies a stricter, independent check that the source path never mirrors.