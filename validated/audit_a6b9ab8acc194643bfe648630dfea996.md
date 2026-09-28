### Title
Governor's `removeDepositToken`/`removeDebtToken` can be permanently DoS'd by minting 1 wei of the token - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.removeDepositToken` and `Pool.removeDebtToken` are governor-only housekeeping functions that require the token's `totalSupply()` to be exactly zero before it can be removed from the offering lists (`depositTokens`, `debtTokens`, `depositTokenOf`, `debtTokenOf`). Any unprivileged user can cheaply and permanently prevent removal by acquiring a dust position (e.g., depositing 1 wei of underlying via `DepositToken.deposit`, or minting 1 wei of debt via `Pool.mint`), which keeps `totalSupply() > 0` forever.

### Finding Description
The bug class of the external report — an admin action gated on a permissionlessly-influenceable zero-balance/supply invariant that any user can grief with dust — exists in `Pool.sol`: [1](#0-0) 

```solidity
// contracts/Pool.sol
function removeDebtToken(IDebtToken debtToken_) external onlyGovernor {
    if (debtToken_.totalSupply() > 0) revert TotalSupplyIsNotZero();
    ...
}

function removeDepositToken(IDepositToken depositToken_) external onlyGovernor {
    if (depositToken_.totalSupply() > 0) revert TotalSupplyIsNotZero();
    ...
}
```

`totalSupply()` is fully under attacker control:

- For a deposit token, any EOA calls `DepositToken.deposit(amount_, onBehalfOf_)` with `amount_ = 1`, which mints 1 wei of msdToken via `_mint` (`totalSupply += amount_`). There is no minimum deposit and no privileged gate on `deposit` — only `whenNotPaused`, `nonReentrant`, and `onlyIfDepositTokenExists`/`onlyIfDepositTokenIsActive` checks, plus `maxTotalSupply` which a 1-wei mint cannot trip.
- For a debt token, any user can open a minimal position (deposit collateral, then `Pool.mint` 1 wei of the synthetic) so `DebtToken.totalSupply()` stays > 0; interest accrual only increases supply further.
- Even if the governor deactivates the deposit token (`isActive = false`, blocking new `_mint`s), dust the attacker already holds still keeps `totalSupply > 0`, and the attacker simply never withdraws/transfers to a burn.

Unlike the SymmIO case where the admin could sweep collateral, there is no path for the governor to force `totalSupply` to zero: burning requires the holder's cooperation (`withdraw`/`seize` only via liquidation of unhealthy positions), so the griefer can sustain the invariant indefinitely at negligible cost (1 wei + gas).

### Impact Explanation
The governor is unable to remove a deprecated/compromised collateral or debt token from the Pool. Since `depositTokenOf[underlying]` remains set, the underlying cannot be re-registered with a new DepositToken (`addDepositToken` reverts with `UnderlyingAssetInUse`), so the protocol is permanently stuck with the old token contract for that underlying — a persistent liveness/administrative-availability failure purchased for ~1 wei, matching the "admin action DoS'd by dust deposit" class.

### Likelihood Explanation
- Cost to attack: 1 wei of any listed underlying plus one `deposit` transaction; repeatable forever via front-running or a standing dust position.
- Preconditions: none beyond the token being listed; `deposit` is a public entry point callable through `Operator.execute` or directly.
- Constraints that do not help: pause would also block legitimate users; `maxTotalSupply` caps cannot prevent a 1-wei mint; deactivation cannot remove already-minted dust.

### Recommendation
Do not gate removal on `totalSupply() == 0`. Options:
- Allow removal when supply is nonzero but deactivate the token first (existing `updateIsActive`-style flag), so no new supply can be created and remaining positions can be wound down/liquidated normally.
- Alternatively, permit `removeDepositToken`/`removeDebtToken` with a nonzero supply and leave residual holders' withdraw/repay paths functional against a token that is simply no longer in `depositTokens`/`debtTokens` (requires auditing `onlyIfDepositTokenExists` call sites, since `withdraw`/`seize` depend on it — at minimum deactivate first and let `unlockedBalanceOf`/`withdraw` still serve remaining holders).

### Proof of Concept
Hardhat (fork or local) sketch:

```ts
// attacker griefs removeDepositToken with 1 wei
await underlying.mint(attacker.address, 1);
await underlying.connect(attacker).approve(depositToken.address, 1);
await depositToken.connect(attacker).deposit(1, attacker.address);

expect(await depositToken.totalSupply()).to.be.gt(0);
await expect(pool.connect(governor).removeDepositToken(depositToken.address))
  .to.be.revertedWithCustomError(pool, 'TotalSupplyIsNotZero');

// governor tries deactivation first - still stuck
await depositToken.connect(governor).toggleIsActive?.() // or equivalent flag setter
await expect(pool.connect(governor).removeDepositToken(depositToken.address))
  .to.be.revertedWithCustomError(pool, 'TotalSupplyIsNotZero');
```

Same pattern applies to `removeDebtToken` by opening a 1-wei debt position via `deposit` + `mint` before the removal transaction.

### Citations

**File:** contracts/Pool.sol (L725-744)
```text
    function removeDebtToken(IDebtToken debtToken_) external onlyGovernor {
        if (debtToken_.totalSupply() > 0) revert TotalSupplyIsNotZero();
        if (!debtTokens.remove(address(debtToken_))) revert DebtTokenDoesNotExist();

        delete debtTokenOf[debtToken_.syntheticToken()];

        emit DebtTokenRemoved(debtToken_);
    }

    /**
     * @notice Remove deposit token (i.e. collateral) from Synth
     */
    function removeDepositToken(IDepositToken depositToken_) external onlyGovernor {
        if (depositToken_.totalSupply() > 0) revert TotalSupplyIsNotZero();

        if (!depositTokens.remove(address(depositToken_))) revert DepositTokenDoesNotExist();
        delete depositTokenOf[depositToken_.underlying()];

        emit DepositTokenRemoved(depositToken_);
    }
```
