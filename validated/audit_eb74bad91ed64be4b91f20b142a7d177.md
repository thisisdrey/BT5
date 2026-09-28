### Title
Attacker can inflate `totalSupply` to `maxTotalSupply` via ordinary borrowing, permanently reverting inbound bridge mints and all new issuance — ([File: contracts/SyntheticToken.sol](contracts/SyntheticToken.sol))

### Summary
`SyntheticToken._mint` reverts with `SurpassMaxSynthSupply` whenever `totalSupply > maxTotalSupply`. Because regular users grow `totalSupply` by borrowing through `DebtToken.issue`, an unprivileged attacker can push `totalSupply` up to the cap. Once there, every mint path reverts — including `ProxyOFT` crediting inbound LayerZero transfers and any new `DebtToken.issue` — until the attacker repays or is liquidated, or the governor raises the cap. This is the same bug class as the report: an attacker-controlled ratio/supply drives a global check (`amount > cap`) into revert, DoS-ing a receive/mint path for everyone.

### Finding Description
`SyntheticToken._mint` enforces a global supply cap on every mint:

```solidity
totalSupply += amount_;
if (totalSupply > maxTotalSupply) revert SurpassMaxSynthSupply();
``` [1](#0-0) 

`totalSupply` is increased by two permissionless-reachable mint paths guarded only by `onlyIfCanMint` (which accepts any registered `Pool`/`DebtToken`/`proxyOFT`/`amo` as sender): [2](#0-1) 

1. **Borrowing**: a user calls `DebtToken.issue(...)`, which calls `syntheticToken.mint` → `_mint`. Any borrower can grow `totalSupply` up to `maxTotalSupply` simply by posting collateral and issuing synth.
2. **Bridging in**: `ProxyOFT` calls `mint` on inbound messages, hitting the same check.

An attacker deposits collateral into a registered `Pool` and calls `DebtToken.issue` repeatedly until `totalSupply` is just below `maxTotalSupply`, then issues the remainder so `totalSupply == maxTotalSupply` (or leaves headroom smaller than the next inbound bridge amount). From that point:

- Every inbound `ProxyOFT` `_creditTo` → `syntheticToken.mint` reverts with `SurpassMaxSynthSupply`, so LayerZero `lzReceive` deliveries for the synth fail. Failed packets must be retried and will keep reverting while the cap is saturated, freezing the bridged funds in transit.
- All new `DebtToken.issue` calls revert, freezing new borrowing for all users.
- `AMO` mints and `SmartFarmingManager` leverage flows that mint synth also revert.

Unlike the report, no user action can clear the condition: victims cannot reduce `totalSupply` (the attacker's debt is the attacker's). Recovery requires either the attacker repaying, a liquidation (which the attacker can avoid by staying healthy), or the governor calling `updateMaxTotalSupply`.

### Impact Explanation
- **Temporary freezing of funds**: inbound LayerZero transfers of the synthetic token revert at `mint`, leaving bridged principal undeliverable for as long as the cap is saturated. The attacker has no incentive to repay (debt accrues interest but minted synth can be sold/held), so this can persist indefinitely absent governance intervention.
- **Protocol-wide liveness DoS**: all borrowing of that synthetic halts, and `SurpassMaxSynthSupply` also blocks `flashIssue`/leverage paths that transiently mint.

### Likelihood Explanation
Cost is bounded by the collateral needed to issue up to `maxTotalSupply` of debt (collateral-factor discounted), comparable to the report's requirement of locking ve tokens to raise gauge weight. The attacker only needs a healthy position (collateralFactor < 1 cushion), and on chains where `maxTotalSupply` is set tight relative to circulating supply the attack is cheap. No privileged role, oracle manipulation, or timing window is needed — only public `DebtToken.issue` calls.

### Recommendation
Decouple bridge conservation from the borrow-side supply cap. Options:

- Track minted-by-borrow vs bridged-in supply separately and only apply `maxTotalSupply` to non-bridge mints (`else if` on `_isMsgSenderProxyOFT`, mirroring the `totalBridgedIn` accounting), so inbound `_creditTo` cannot be blocked by borrow volume:
```solidity
if (_isMsgSenderProxyOFT(_msgSender)) {
    totalBridgedIn += amount_;
    if (bridgedInSupply() > maxBridgedInSupply) revert SurpassMaxBridgingSupply();
} else {
    totalSupply += amount_;
    if (totalSupply > maxTotalSupply) revert SurpassMaxSynthSupply();
}
```
- Alternatively, make `maxBridgedInSupply`/`maxBridgedOutSupply` the only bridge-side caps and exclude bridged-in amounts from the `maxTotalSupply` check.
- Also consider a permissionless "cap relief" path (e.g., allow burns even when paused) so liquidations/repayments always reduce `totalSupply`.

### Proof of Concept
Foundry/Hardhat fork sketch (pool, deposit token `dt`, debt token `debt`, synth `msX`, proxyOFT already wired on mainnet fork):

```solidity
// Setup: fork mainnet; maxTotalSupply = M for msX; attacker EOA.
IERC20 collateral = IERC20(dt.underlying());
collateral.approve(address(dt), type(uint256).max);

// 1. Attacker deposits collateral and issues debt until totalSupply ~ M
uint256 headroom = msX.maxTotalSupply() - msX.totalSupply();
dt.deposit(collateralNeeded);              // enough collateral to issue ~headroom
debt.issue(headroom, attacker);            // mints headroom of msX to attacker
assertEq(msX.totalSupply(), msX.maxTotalSupply());

// 2. Inbound bridge delivery now reverts
// Simulate lzReceive crediting `amount` to victim:
vm.prank(address(msX.proxyOFT()));
vm.expectRevert(SurpassMaxSynthSupply.selector);
msX.mint(victim, 1e18);                    // _creditTo path reverts -> packet fails

// 3. New borrowing also reverts for any user
vm.prank(bob);
vm.expectRevert(SurpassMaxSynthSupply.selector);
debt.issue(1e18, bob);

// 4. Condition persists until attacker repays/liquidated or governor raises cap
debt.repay(attacker, attacker.balanceOf(debt)); // only attacker can clear it
```

The revert propagates through `ProxyOFT._creditTo` → `lzReceive`, marking the LayerZero message failed; retries keep reverting while `totalSupply == maxTotalSupply`, which matches the report's "rewards never reach the gauge until weight is reduced" grief pattern — here, bridged funds never arrive until the cap is relieved.

### Citations

**File:** contracts/SyntheticToken.sol (L97-106)
```text
    modifier onlyIfCanMint() {
        address _msgSender = _msgSender();
        if (
            !_isMsgSenderProxyOFT(_msgSender) &&
            !_isMsgSenderAmo(_msgSender) &&
            !_isMsgSenderPool(_msgSender) &&
            !_isMsgSenderDebtToken(_msgSender)
        ) revert SenderCanNotMint();
        _;
    }
```

**File:** contracts/SyntheticToken.sol (L346-348)
```text
        totalSupply += amount_;
        if (totalSupply > maxTotalSupply) revert SurpassMaxSynthSupply();
        balanceOf[account_] += amount_;
```
