### Title
`DebtToken._mint` / `SyntheticToken._mint` hard supply-cap reverts can be front-run to DoS victims' `Operator.execute` batches and bridge sends - ([File: contracts/DebtToken.sol](contracts/DebtToken.sol))

### Summary
`DebtToken._mint` reverts with `SurpassMaxDebtSupply` when `totalSupply_ + amount_ > maxTotalSupply` instead of capping the mint, and `SyntheticToken._mint`/`_burn` revert with `SurpassMaxBridgingSupply` when `bridgedInSupply()`/`bridgedOutSupply()` exceed their caps. Because `Operator.execute` executes its `Call[]` atomically and reverts the whole transaction if any sub-call fails, an unprivileged attacker can front-run a victim's batched transaction, consume the remaining cap headroom with their own position, and force the victim's entire multicall (and any queued bridging `sendFrom`) to revert — repeatedly, since the attacker can free and re-fill the headroom. This is the same bug class as the JOJO `ORDER_FILLED_OVERFLOW` finding: a strict "would-exceed-limit ⇒ revert" check that a front-runner can trip to deny service.

### Finding Description
In `DebtToken._mint` the cap is enforced as a hard revert:

`contracts/DebtToken.sol:590-591`
```solidity
totalSupply_ += amount_;
if (totalSupply_ > maxTotalSupply) revert SurpassMaxDebtSupply();
```

`DebtToken.issue` (public, `whenNotShutdown`, `nonReentrant`, checks only the caller's own collateral) and `mint` (via `SmartFarmingManager`) both reach `_mint`. There is no partial-fill: an `issue` that would push supply over `maxTotalSupply` reverts entirely rather than minting up to the cap.

Likewise in `SyntheticToken`:

`contracts/SyntheticToken.sol:338-347`
```solidity
if (_isMsgSenderProxyOFT(_msgSender)) {
    totalBridgedIn += amount_;
    if (bridgedInSupply() > maxBridgedInSupply) revert SurpassMaxBridgingSupply();
}
...
totalSupply += amount_;
if (totalSupply > maxTotalSupply) revert SurpassMaxSynthSupply();
```

and `_burn` on the outbound path (`contracts/SyntheticToken.sol:275-277`) reverts when `bridgedOutSupply() > maxBridgedOutSupply`.

`Operator.execute` (`contracts/Operator.sol:34-55`) loops over `calls_` and propagates any inner revert, so one failing sub-call DoSes the entire batch (e.g., `updatePriceFeeds` + `deposit` + `issue` + `leverage`).

Attack path (concrete):
1. Victim broadcasts `operator.execute([depositCollateral, issue(X, victim), ...])` where `X` was sized against current `totalSupply`.
2. Attacker sees mempool tx, front-runs with `deposit` + `issue(headroom, attacker)` using their own collateral, pushing `totalSupply_` within dust of `maxTotalSupply`.
3. Victim's `issue` hits `SurpassMaxDebtSupply`; the whole `execute` reverts, including already-successful sub-calls.
4. Attacker then `repay`s to free headroom and can repeat the grief on the victim's resubmission — or simply hold the cap full to deny all new issuance until they unwind.

The bridging variant is analogous: the attacker calls `ProxyOFT.sendFrom` with their own synths to push `bridgedOutSupply()` to `maxBridgedOutSupply`, causing every other user's outbound bridge send to revert until the attacker bridges back in (which they can do via their own remote-chain `sendFrom`, a purely public action).

### Impact Explanation
Temporary freezing of funds / liveness denial. Victims cannot complete batched position operations (deposit+issue, leverage flows through `SmartFarmingManager` that end in `mint`/`issue`, or `sendFrom` bridging) while the attacker holds the cap saturated. On Metronome chains where `Operator.execute` is the normal entry point for multi-step flows (e.g., Pyth `updatePriceFeeds` + deposit + issue), the revert kills the entire user transaction, forcing re-computation and resubmission that can be re-griefed. For bridging, users' synths are stuck on the source chain while `bridgedOutSupply` is saturated. The attacker's capital is recoverable (debt repaid, synths bridged back), so the cost is gas plus bridge/LZ fees rather than principal.

### Likelihood Explanation
Moderate. No privileged role is needed: `issue` only requires the caller's own collateralized position (`DebtToken.sol:235-271`), and `sendFrom`/`_burn` bridging is open to any synth holder. The constraint is capital: the attacker must hold enough issuance/bridge volume to fill cap headroom, and `issue` enforces `debtFloorInUsd` (`DebtToken.sol:580-588`), so each filler position needs at least the floor-sized debt — but this is temporary, reclaimable capital. Caps are governance-set and can be close to live supply (e.g., deployed `maxBridgedInSupply`/`maxBridgedOutSupply` values are in the tens of millions for msUSD and 4500 for msETH per the deploy configs), so headroom may be small enough to make this cheap. No reentrancy guard, SynthContext check, or pause flag distinguishes cap-filling front-run transactions from legitimate ones.

### Recommendation
Mirror the JOJO fix: clamp to the cap instead of reverting.

```diff
// DebtToken._mint
- totalSupply_ += amount_;
- if (totalSupply_ > maxTotalSupply) revert SurpassMaxDebtSupply();
+ uint256 _headroom = maxTotalSupply - totalSupply_;
+ if (amount_ > _headroom) amount_ = _headroom;
+ if (amount_ == 0) revert SurpassMaxDebtSupply();
+ totalSupply_ += amount_;
```

For `SyntheticToken._mint`/`_burn`, clamp `amount_` to the remaining `maxBridgedInSupply`/`maxBridgedOutSupply`/`maxTotalSupply` headroom (and, for the OFT path, credit/debit only the clamped amount so `ProxyOFT` doesn't silently mint less than the LayerZero message claimed — the safer variant is to keep the revert on inbound `_creditTo` but expose the headroom check off-chain; the clamp is appropriate for user-initiated `issue`/`sendFrom`). Note `DebtToken.issue` computes `_issued`/`_fee` from `amount_` before `_mint` clamps — the clamped amount must be propagated back so `SyntheticTokenIssued` and the debt balance stay consistent.

### Proof of Concept
Hardhat fork sketch (pattern follows `test/Operator.test.ts`):

```ts
// given: msUSD debt token, maxTotalSupply = current supply + H (small headroom)
const {Pool1, MsUSDDebtToken_Pool1, MsUSDSynthetic, USDCDepositToken_Pool1, Operator} = deployments;

// attacker opens own collateralized position and fills the headroom
await usdc.connect(attacker).approve(msdUSDC.address, ATTACKER_COLLATERAL);
await msdUSDC.connect(attacker).deposit(ATTACKER_COLLATERAL, attacker.address);
// front-run: issue exactly `headroom` of debt (>= debtFloorInUsd)
await msUSDDebt.connect(attacker).issue(headroom, attacker.address);
expect(await msUSDDebt.totalSupply()).to.be.closeTo(maxTotalSupply, epsilon);

// victim's batch: deposit + issue(victimAmount) reverts entirely
const calls = [
  {target: msdUSDC.address, value: 0, callData: msdUSDC.interface.encodeFunctionData('deposit', [VICTIM_DEPOSIT, victim.address])},
  {target: msUSDDebt.address, value: 0, callData: msUSDDebt.interface.encodeFunctionData('issue', [VICTIM_ISSUE, victim.address])},
];
await expect(operator.connect(victim).execute(calls)).to.be.reverted; // SurpassMaxDebtSupply

// attacker frees headroom and repeats on victim's resubmission
await msUSD.connect(attacker).approve(msUSDDebt.address, headroom);
await msUSDDebt.connect(attacker).repay(attacker.address, headroom);

// bridging variant:
await msETH.connect(attacker).approve(msETHProxyOFT.address, fillAmt);
await msETHProxyOFT.connect(attacker).sendFrom(attacker.address, dstChainId, attacker.address, fillAmt, ...);
await expect(msETHProxyOFT.connect(victim).sendFrom(...)).to.be.reverted; // SurpassMaxBridgingSupply
```

Caveats / unverified details: the feasibility depends on the live `maxTotalSupply`/`maxBridgedOutSupply` headroom and `debtFloorInUsd` on each chain (governor-set; queryable on a fork via `debtToken.maxTotalSupply()`, `debtToken.totalSupply()`, `pool.debtFloorInUsd()`, `synth.maxBridgedOutSupply()`, `synth.bridgedOutSupply()`). If headroom is large, attack cost is the capital (reclaimable) needed to fill it. The clamped-mint fix also requires updating `issue`'s emitted amounts to use the post-clamp value, which I did not fully trace through `SyntheticTokenIssued` accounting.