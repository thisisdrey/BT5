### Title
`VesperGateway.withdraw` redeems Vesper shares with no minimum-output slippage check, allowing frontrunning loss of user funds - ([File: contracts/VesperGateway.sol](contracts/VesperGateway.sol))

### Summary
`VesperGateway.withdraw` burns the caller's `msdToken`, withdraws the corresponding `vToken` shares from the `DepositToken`, and then redeems them for `underlying` via `vToken_.withdraw(_vTokenAmount)`, forwarding whatever amount comes back to the user. There is no `minAmountOut` parameter and no post-check on `_underlyingAmount`, so the value the user receives is entirely dependent on the Vesper pool's state at execution time. An unprivileged attacker can frontrun the victim's transaction and manipulate the Vesper pool so the share redemption returns less underlying than the fair quote.

### Finding Description
In `VesperGateway.withdraw` (contracts/VesperGateway.sol:73-93):

```solidity
(uint256 _vTokenAmount, ) = _depositToken.withdraw(amount_, address(this));
uint256 _balanceBefore = _underlying.balanceOf(address(this));
vToken_.withdraw(_vTokenAmount);
uint256 _underlyingAmount = _underlying.balanceOf(address(this)) - _balanceBefore;
_underlying.safeTransfer(_msgSender, _underlyingAmount);
```

The amount of underlying returned by `IVPool.withdraw` is a function of the Vesper pool's `pricePerShare`, available liquidity, and pool-level withdraw fees — all of which are manipulable by third parties in the same block window. Metronome itself acknowledges this non-determinism in `AMO._withdrawFromVesper` (contracts/AMO.sol:142-143): *"Vesper pool may withdraw less, of course burn less shares too, than requested."*

Attack path:
1. Victim submits `VesperGateway.withdraw(pool, vToken, amount)` expecting ~`amount` underlying based on the current quote.
2. Attacker (any EOA) frontruns with a transaction that reduces the underlying returned per share of the same `vToken` — e.g., draining the Vesper pool's liquid token balance via a large `vToken.withdraw`, or interacting with the pool/strategy so that the victim's redemption incurs a loss/withdraw fee or receives less.
3. The victim's `msdToken` is still burned for the full `amount_` (the `DepositToken.withdraw` leg is 1:1 minus a fixed fee), but step 3 of the gateway returns a smaller `_underlyingAmount`, which is silently forwarded.

The invariant that breaks is fair redemption: shares in → proportional collateral out. The function accepts a non-deterministic external redemption without any user-specified bound, which is exactly the bug class of the reference report (no threshold on output of a share-sale). Notably, the parallel `SmartFarmingManager.flashRepay`/`leverage` flows do include explicit `swapAmountOutMin_`/`depositAmountMin_` slippage parameters (contracts/SmartFarmingManager.sol:96-126, 153-161), showing the protocol's own convention that externally-priced outputs must be bounded — the gateway path lacks this.

`VesperGateway.withdraw` is `nonReentrant` but that does not help: the manipulation happens in a separate (earlier) transaction. `UnregisteredPool` only restricts `pool_`, and `vToken_`/the pool state are fully attacker-influenceable. The same gap applies to `NativeTokenGateway` only trivially (WETH unwrap is 1:1), so `VesperGateway` is the reachable surface.

### Impact Explanation
The victim permanently loses the difference between the fair underlying value of their burned shares and the reduced amount actually returned by `vToken_.withdraw`. This is a direct loss of user funds caused by a frontrunnable, unbounded external redemption.

### Likelihood Explanation
Requires a deployed pool whose collateral is a Vesper `vToken` (vaUSDC, vaETH, vaWSTETH DepositTokens exist in the deployments) and a Vesper pool whose redemption output can be moved by an unprivileged party within the same block (withdraw-fee events, liquidity depletion, strategy loss realization). Frontrunning/sandwiching on public entry points is within the attacker model, and no modifier, pause flag, or health check prevents it — the only guard is `nonReentrant`, which is irrelevant cross-transaction.

### Recommendation
Add a `minUnderlyingAmount_` parameter to `VesperGateway.withdraw` and revert if `_underlyingAmount < minUnderlyingAmount_`, matching the `swapAmountOutMin_` pattern already used in `SmartFarmingManager.flashRepay`.

### Proof of Concept
Conceptual Hardhat fork sketch:

```solidity
// fork mainnet; pool = deployed Pool; vToken = vaUSDC; depositToken = msdVaUSDC
// victim holds `amount` of msdVaUSDC and approved VesperGateway

// 1. Attacker frontruns: deplete Vesper pool liquidity / realize loss so that
//    pricePerShare-effective redemption drops (e.g., attacker withdraws a large
//    vToken position, or triggers strategy rebalance/withdraw fee).
vm.prank(attacker);
vToken.withdraw(attackerShares); // large redemption reducing pool token balance

// 2. Victim tx executes in same block with worse redemption:
vm.prank(victim);
vesperGateway.withdraw(pool, vToken, amount);

// 3. Assert victim received < expected underlying while full `amount` shares were burned
assertLt(underlying.balanceOf(victim) - before, quotedAmount);
assertEq(depositToken.balanceOf(victim), 0);
```

The PoC requires a Vesper pool state where an unprivileged action reduces per-share redemption output (liquidity exhaustion or withdraw fee); on a fork this can be demonstrated by impersonating a large shareholder or by using a pool with an active withdraw fee — the contract code itself makes no allowance for checking the result.