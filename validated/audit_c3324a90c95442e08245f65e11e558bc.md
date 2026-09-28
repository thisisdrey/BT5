### Title
`SyntheticToken` burn paths via Pool/DebtToken never decrement `totalBridgedIn`, permanently shrinking the inbound bridging capacity - ([File: contracts/SyntheticToken.sol](contracts/SyntheticToken.sol))

### Summary
`SyntheticToken` tracks net bridged circulation with two cumulative counters, `totalBridgedIn` and `totalBridgedOut`, and enforces `bridgedInSupply() <= maxBridgedInSupply` on inbound mints. `totalBridgedIn` is incremented whenever `proxyOFT` credits tokens (bridge-in), but it is only ever compensated via `totalBridgedOut` when the burn is initiated **by `proxyOFT` itself**. When a user bridges synthetic tokens in and then burns them through any local path (`Pool.swap`, `DebtToken.repay`/`repayAll`, liquidation), `bridgedInSupply()` stays permanently inflated. This is the same bug class as the Remora `tokensSold` issue: an accounting counter is not decremented on one of the exit paths, permanently blocking new capacity.

### Finding Description
In `_mint`, when `_msgSender` is `proxyOFT`, `totalBridgedIn += amount_` is recorded and the cap is enforced. [1](#0-0) 

In `_burn`, `totalBridgedOut += amount_` is recorded **only** when `_msgSender` is `proxyOFT`; burns via Pool, DebtToken, or AMO never reduce the net bridged-in figure. [2](#0-1) 

`bridgedInSupply()` is computed as `totalBridgedIn - totalBridgedOut`, so bridged-in tokens that are destroyed locally remain counted forever. [3](#0-2) 

Attack path (all unprivileged):
1. Attacker calls `ProxyOFT.sendFrom` on a remote chain; on this chain LayerZero delivery calls the credit path which invokes `SyntheticToken.mint` from `proxyOFT`, incrementing `totalBridgedIn`.
2. Attacker burns those tokens locally instead of bridging them back out — e.g., `DebtToken.repay`/`Pool.swap` burns the synthetic via `syntheticToken.burn(from, amount)` where the caller is the DebtToken/Pool, so the `totalBridgedOut` branch is skipped.
3. `bridgedInSupply()` is permanently elevated by `amount`. Repeating this (or one large bridge-in) pushes `bridgedInSupply()` to `maxBridgedInSupply`.
4. Every subsequent inbound bridge message reverts in `_mint` with `SurpassMaxBridgingSupply`, DoSing the inbound direction of the bridge; the LayerZero messages fail delivery and the associated users' funds are stuck until the failure is resolved. [4](#0-3) 

### Impact Explanation
Permanent freezing/DoS of inbound bridging for all users of the synthetic token. Once `bridgedInSupply() > maxBridgedInSupply`, no user can receive tokens through `ProxyOFT` on this chain — every credit reverts. Unlike a temporary congestion, the deficit is never healed by local activity, because the only mechanism that restores headroom (`totalBridgedOut`) requires someone to voluntarily bridge **out**, which honest users have no incentive to do to fix the attacker's damage. This maps to the accepted impact category of temporary freezing of funds / liveness break of a core bridge invariant (conservation between `totalBridgedIn`, `totalBridgedOut`, and locally burned supply).

### Likelihood Explanation
Fully reachable by an unprivileged attacker using public entry points: `ProxyOFT.sendFrom`/`sendAndCall` on any supported remote chain and local `Pool.swap` or `DebtToken.repay` on this chain. No privileged role, oracle manipulation, or malicious LayerZero component is required — the attacker only needs a legitimate bridge-in followed by a legitimate local burn. The only precondition is that `maxBridgedInSupply` is set to a finite value, which is the purpose of the governor-configured cap; the cost to the attacker is bounded by the size of that cap.

### Recommendation
Mirror the Remora fix — decrement the counter on every exit path, not just the proxyOFT one. When a burn that is not initiated by `proxyOFT` occurs while `totalBridgedIn > totalBridgedOut`, the net bridged-in position should be reduced accordingly, e.g. track the amount of bridged-in supply separately from gross counters or decrement `totalBridgedIn` (clamped) when a locally-held bridged balance is burned:

```diff
 if (_isMsgSenderProxyOFT(_msgSender)) {
     totalBridgedOut += amount_;
     if (bridgedOutSupply() > maxBridgedOutSupply) revert SurpassMaxBridgingSupply();
-} else if (_isMsgSenderAmo(_msgSender)) {
+} else {
+    if (bridgedInSupply() > 0) totalBridgedIn -= Math.min(amount_, bridgedInSupply());
+    if (_isMsgSenderAmo(_msgSender)) {
         if (account_ != _msgSender) revert AmoInvalidAccount();
         amoSupply -= amount_;
+    }
 }
```

(Exact accounting treatment depends on intended semantics — e.g., whether locally minted and bridged-in supplies are fungible — but the invariant must be: burning bridged-in supply frees inbound bridge capacity.)

### Proof of Concept
Foundry-style PoC (fork or unit test with mocked LZ endpoint delivering to `ProxyOFT`):

```solidity
// 1. Governor configured: maxBridgedInSupply = 1_000e18 (finite cap)
// 2. Attacker bridges in 1_000e18 msUSD from remote chain.
//    lzReceive -> _creditTo -> syntheticToken.mint(attacker, 1_000e18)
//    state: totalBridgedIn = 1_000e18, bridgedInSupply() = 1_000e18
// 3. Attacker swaps the bridged-in msUSD for another synth via Pool.swap,
//    or repays debt -> DebtToken calls syntheticToken.burn(attacker, 1_000e18)
//    with _msgSender = debtToken (not proxyOFT).
//    state: totalBridgedIn = 1_000e18, totalBridgedOut = 0,
//           bridgedInSupply() = 1_000e18  // not decremented!
// 4. Honest user attempts to bridge in 1 wei:
//    syntheticToken.mint -> bridgedInSupply() + 1 > maxBridgedInSupply
//    -> reverts SurpassMaxBridgingSupply. Inbound bridge is DoSed.
```

Key assertions:
- After step 2: `syntheticToken.bridgedInSupply() == 1_000e18`
- After step 3 (burn via `Pool.swap`): `bridgedInSupply()` is still `1_000e18` even though `totalSupply` decreased by `1_000e18`
- Step 4: any `proxyOFT`-initiated `mint` of `> 0` reverts with `SurpassMaxBridgingSupply`

### Citations

**File:** contracts/SyntheticToken.sol (L160-167)
```text
    function bridgedInSupply() public view returns (uint256 _supply) {
        uint256 _totalBridgedIn = totalBridgedIn;
        uint256 _totalBridgedOut = totalBridgedOut;

        if (_totalBridgedIn > _totalBridgedOut) {
            return _totalBridgedIn - _totalBridgedOut;
        }
    }
```

**File:** contracts/SyntheticToken.sol (L275-284)
```text
        if (_isMsgSenderProxyOFT(_msgSender)) {
            totalBridgedOut += amount_;
            if (bridgedOutSupply() > maxBridgedOutSupply) revert SurpassMaxBridgingSupply();
        } else if (_isMsgSenderAmo(_msgSender)) {
            // AMO can only burn from self address.
            // account_ should be AMO and in this case it is same as _msgSender()
            if (account_ != _msgSender) revert AmoInvalidAccount();

            amoSupply -= amount_;
        }
```

**File:** contracts/SyntheticToken.sol (L338-341)
```text
        if (_isMsgSenderProxyOFT(_msgSender)) {
            totalBridgedIn += amount_;
            if (bridgedInSupply() > maxBridgedInSupply) revert SurpassMaxBridgingSupply();
        } else if (_isMsgSenderAmo(_msgSender)) {
```
