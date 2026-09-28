### Title
`totalBridgedIn` is never decremented when bridged-in synths are burned by Pool/DebtToken, permanently inflating `bridgedInSupply` and blocking inbound bridging — (File: contracts/SyntheticToken.sol)

### Summary
`SyntheticToken` tracks cross-chain supply with two monotonic counters, `totalBridgedIn` and `totalBridgedOut`, which are only mutated inside `_mint`/`_burn` when `msg.sender == proxyOFT`. When a synthetic token that was minted via an inbound bridge message is later burned on this chain through a non-bridge path (debt repay, swap burn, AMO-independent seize/burn), `totalBridgedIn` is left unchanged. The stale residue inflates `bridgedInSupply() = totalBridgedIn - totalBridgedOut`, which is the same accounting surface that gates every future inbound bridge credit via `SurpassMaxBridgingSupply`. This is the direct analog of the reported `inFlightBridgeAmounts` bug: a bridge-side accounting entry that is not cleared after the assets are settled/consumed, permanently polluting a value used in a solvency/liveness check.

### Finding Description
In `SyntheticToken._mint`, inbound bridge credits do `totalBridgedIn += amount_` and enforce `bridgedInSupply() <= maxBridgedInSupply` (`contracts/SyntheticToken.sol:338-340`). In `_burn`, only burns initiated by `proxyOFT` update a counter (`totalBridgedOut += amount_`); burns initiated by a registered `Pool` or `DebtToken` (repay, liquidation burn, swap burn) decrement `totalSupply` but never touch `totalBridgedIn` (`contracts/SyntheticToken.sol:270-294`).

Concretely:

1. Attacker issues `msUSD` on Optimism (or buys it on a DEX there) — those tokens carry zero contribution to mainnet `totalBridgedOut`.
2. Attacker calls `ProxyOFT.sendFrom` on Optimism to move `X` msUSD to mainnet. On mainnet, `_creditTo` → `syntheticToken.mint` sets `totalBridgedIn += X` and `totalSupply += X` (`contracts/ProxyOFT.sol:86-93`).
3. Attacker repays debt / swaps / otherwise burns the `X` msUSD on mainnet through `Pool`/`DebtToken`. `totalSupply` decreases but `totalBridgedIn` stays at `X`.
4. `bridgedInSupply()` now reports `X` units of "bridged-in supply" that no longer exist on this chain — a stale residue identical in class to stale `inFlightBridgeAmounts` polluting `tvl`.
5. The residue is permanent and cumulative. Once the accumulated stale amount pushes `bridgedInSupply()` to `maxBridgedInSupply` (finite, e.g. `10,000,000` msUSD on mainnet per `test/E2E.mainnet.next.test.ts:988`), every subsequent inbound `_creditTo` reverts with `SurpassMaxBridgingSupply`. Since `LzApp`/`NonblockingLzApp` only stores the failing message in `failedMessages`, legitimate users' inbound transfers are stuck and can only be retried after the governor raises `maxBridgedInSupply`.

The symmetric check in `_burn` (`bridgedOutSupply() > maxBridgedOutSupply`) is not exploitable in the same way because outbound debits and inbound credits net correctly; the stale accumulation is asymmetric — only `totalBridgedIn` grows without a corresponding decrement path.

### Impact Explanation
Breaks the bridge accounting invariant and causes temporary freezing of funds: every inbound LayerZero credit for the synth reverts once stale `bridgedInSupply` reaches the cap, so in-flight user transfers queue in `failedMessages` and remain unspendable until governance intervenes. Additionally, the over-reported `bridgedInSupply` permanently understates `bridgedOutSupply`, weakening the `maxBridgedOutSupply` safety bound — an attacker can exceed the configured bridged-out cap by first padding `totalBridgedIn` and then burning locally.

### Likelihood Explanation
Reachable by an unprivileged attacker using only public entry points: issue/buy synths on a supported remote chain, `ProxyOFT.sendFrom` to mainnet, then burn them locally via `DebtToken.repay`/`Pool.swap`. No privileged role, oracle manipulation, or malicious endpoint is required. The cost is the capital needed to push the synth through the bridge, and the effect compounds organically even without an attacker because ordinary repays of bridged-in synths already produce the residue.

### Recommendation
Decrement the bridged counters whenever bridged-in supply is destroyed by a non-bridge burn, or restructure the accounting to track per-direction net supply instead of cumulative counters — e.g. maintain `bridgedInSupply`/`bridgedOutSupply` as signed net values and clamp them down on local burns of supply that originated cross-chain. Alternatively, track in-flight vs. settled bridge amounts separately (per the referenced recommendation) so settled amounts can be cleared.

### Proof of Concept
Foundry fork test based on `test/foundry/CrossChains.t.sol`:

```solidity
function test_staleBridgedInSupply_blocksInboundBridging() external {
    uint256 amount = 200e18;
    uint16 srcChainId = LZ_OP_CHAIN_ID;

    // 1. Issue msUSD on Optimism and bridge it to mainnet (ProxyOFT.sendFrom).
    _issueOnOptimism(amount);
    vm.selectFork(optimismFork);
    uint256 fee = proxyOFT_msUSD_optimism.estimateSendFee(LZ_MAINNET_CHAIN_ID, alice, amount);
    vm.startPrank(alice);
    deal(alice, fee);
    proxyOFT_msUSD_optimism.sendFrom{value: fee}(alice, LZ_MAINNET_CHAIN_ID, alice, amount);
    vm.stopPrank();

    // 2. Deliver on mainnet -> _creditTo -> mint: totalBridgedIn += amount.
    vm.selectFork(mainnetFork);
    vm.startPrank(lzEndpoint_mainnet.defaultReceiveLibraryAddress());
    lzEndpoint_mainnet.receivePayload({
        _srcChainId: srcChainId,
        _srcAddress: abi.encodePacked(address(proxyOFT_msUSD_optimism), address(proxyOFT_msUSD_mainnet)),
        _dstAddress: address(proxyOFT_msUSD_mainnet),
        _nonce: lzEndpoint_mainnet.getInboundNonce(srcChainId, abi.encode(address(proxyOFT_msUSD_optimism))) + 1,
        _gasLimit: SIMPLE_TRANSFER_GAS,
        _payload: abi.encode(PT_SEND, abi.encodePacked(alice), amount)
    });
    vm.stopPrank();
    assertEq(msUSD_mainnet.bridgedInSupply(), amount);

    // 3. Alice repays/burns the bridged-in synths via DebtToken (non-bridge burn).
    //    totalSupply drops, but totalBridgedIn stays -> bridgedInSupply() stays == amount (STALE).
    vm.startPrank(alice);
    msUSD_mainnet.approve(address(msUSDDebt_mainnet), amount);
    msUSDDebt_mainnet.repay(alice, amount); // burns via DebtToken path, counter untouched
    vm.stopPrank();

    // BUG: bridged-in supply is stale - tokens no longer exist on this chain.
    assertEq(msUSD_mainnet.totalSupply(), 0);
    assertEq(msUSD_mainnet.bridgedInSupply(), amount); // still reported

    // 4. Stale residue accumulates toward maxBridgedInSupply; once reached, every
    //    subsequent inbound credit reverts with SurpassMaxBridgingSupply, and the
    //    message is parked in failedMessages until the governor raises the cap.
}
```