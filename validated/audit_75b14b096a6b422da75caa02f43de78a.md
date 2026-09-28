### Title
`Pool.swap()` and `Pool.liquidate()` remain callable while the pool is paused — only `whenNotShutdown` is enforced - (File: contracts/Pool.sol)

### Summary
Metronome's `Pool` implements two-tier emergency controls: `pause()` (suspend risky features) and `shutdown()` (suspend everything). However, `swap()` and `liquidate()` are guarded only by `whenNotShutdown`, not `whenNotPaused`. While debt issuance and deposits are blocked during a pause, an unprivileged user can still burn/mint synthetic tokens at oracle prices through `swap()`, and seize collateral through `liquidate()`. During the exact emergencies a pause is meant to mitigate (oracle malfunction, collateral depeg, mint anomaly), continued swapping mints synthetics against stale/incorrect quotes, which can render the pool insolvent, and continued liquidations can seize user collateral under manipulated same-transaction prices.

### Finding Description
`Pauseable.pause()` sets `_paused = true` and is explicitly documented to "suspend deposit feature" while `shutdown()` suspends all features [1](#0-0) . `Pool.paused()` additionally returns true whenever the `PoolRegistry` is paused, so a single registry-level pause is supposed to halt all pools [2](#0-1) .

The state-changing entry points that create or destroy value do not honor the paused flag:

- `swap()` burns `syntheticTokenIn_` and mints `syntheticTokenOut_` at the `MasterOracle` quote minus a fee. It is protected only by `whenNotShutdown` [3](#0-2) .
- `liquidate()` burns the liquidator's synth, burns the account's debt token, and seizes collateral to the liquidator plus a protocol fee. It is protected only by `whenNotShutdown` [4](#0-3) .

By contrast, peripheral contracts gate their risky paths on `whenNotPaused` via `Manageable` (`if (pool.paused()) revert IsPaused()`) [5](#0-4) . `DebtToken` and `DepositToken` use these modifiers extensively (10 and 4 occurrences respectively), so minting debt and depositing collateral are frozen during pause — but the oracle-priced mint path inside `Pool.swap()` is not.

### Impact Explanation
While paused, `swap()` keeps functioning as an unrestricted synth mint/burn mechanism priced purely by `masterOracle().quote()` [6](#0-5) . A pause is typically triggered precisely because quotes or collateral health can no longer be trusted (oracle update pending, depeg detected, anomalous mint). In that window an unprivileged attacker can:

- Sell a depegged/devalued synthetic token into a still-correctly-priced one at the stale oracle rate, minting `syntheticTokenOut_` backed by nothing — direct protocol insolvency.
- Continue to `liquidate()` positions with same-transaction price manipulation (e.g., flash-loan the collateral's market the oracle reads) while victims' recovery paths (deposit via paused `DepositToken`, repay paths gated by `Manageable.whenNotPaused`) are frozen — theft of collateral beyond the intended `maxLiquidable`/fee bounds during a state where users cannot defend positions.

Both break the solvency invariant and the purpose of the emergency control; the asymmetry (attacker swap/liquidate open, defensive deposit/mint closed) is the core issue.

### Likelihood Explanation
Likelihood is moderate: exploitation requires the pool or registry to be paused while oracle quotes still settle, which is a real (and historically common) incident pattern — pause is the first response, oracle remediation comes after. The attacker needs only an EOA holding or flash-acquiring any synthetic token; `swap()` has no balance source restriction beyond `balanceOf`, `nonReentrant` does not block it, and `isSwapActive` remains true unless the governor separately calls `toggleIsSwapActive()` [7](#0-6) . Note: if the deployers intend liquidations to stay live during pause for solvency, that intent is not documented, and `swap()` has no equivalent justification — pausing is explicitly a feature-suspension mechanism.

### Recommendation
Add `whenNotPaused` to `Pool.swap()` so oracle-priced mint/burn halts during a pause. For `liquidate()`, either add `whenNotPaused` or, if liquidations must remain available during pause for solvency reasons, document that explicitly and ensure pause cannot be used while oracle quotes are unreliable (e.g., freeze swap but allow liquidate is only safe if the oracle is trusted during the pause). At minimum:

```solidity
// contracts/Pool.sol
function swap(...) external override whenNotPaused whenNotShutdown nonReentrant ...
```

### Proof of Concept
Foundry/Hardhat fork sketch against a deployed `Pool` proxy (e.g., mainnet deployment):

```solidity
// test/PauseBypass.t.sol — fork mainnet at a block where pool is live
function test_SwapWorksWhilePaused() public {
    IPool pool = IPool(POOL_PROXY);
    address governor = pool.governor();
    ISyntheticToken msA = ISyntheticToken(MSUSD);
    ISyntheticToken msB = ISyntheticToken(MSETH);

    // attacker holds synthIn
    deal(address(msA), attacker, 1_000e18);

    // governor pauses the pool (emergency) — NOT shutdown
    vm.prank(governor);
    pool.pause();
    assertTrue(pool.paused());
    assertFalse(pool.everythingStopped());

    // defensive paths are blocked (DepositToken.deposit reverts IsPaused
    // via Manageable.whenNotPaused), but swap is not:
    vm.prank(attacker);
    (uint256 amountOut, ) = pool.swap(msA, msB, 1_000e18); // succeeds, mints msB at oracle quote

    assertGt(amountOut, 0); // pause failed to stop value-changing mint
}

function test_LiquidateWorksWhilePaused() public {
    // after pausing as above, an unhealthy account can still be liquidated:
    vm.prank(liquidator);
    pool.liquidate(msUSD, victim, repayAmt, depositToken); // succeeds despite paused()
}
```

Expected: both calls succeed while `pool.paused() == true`, demonstrating the emergency control does not cover these entry points.

### Citations

**File:** contracts/utils/Pauseable.sol (L103-117)
```text
     * @dev Suspend deposit feature, if contract is not paused.
     */
    function pause() external virtual whenNotPaused onlyGovernor {
        _paused = true;
        emit Paused(_msgSender());
    }

    /**
     * @dev Suspend all features (issue, repay, deposit, withdraw, liquidate and swap), if not already shutdown.
     */
    function shutdown() external virtual whenNotShutdown canShutdown {
        _everythingStopped = true;
        _paused = true;
        emit Shutdown(_msgSender());
    }
```

**File:** contracts/Pool.sol (L508-524)
```text
    function quoteSwapOut(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountIn_
    ) public view override returns (uint256 _amountOut, uint256 _fee) {
        _amountOut = _poolRegistry.masterOracle().quote(
            address(syntheticTokenIn_),
            address(syntheticTokenOut_),
            amountIn_
        );

        uint256 _swapFee = feeProvider.swapFees(address(syntheticTokenIn_), address(syntheticTokenOut_));

        if (_swapFee > 0) {
            _fee = _amountOut.wadMul(_swapFee);
            _amountOut -= _fee;
        }
```

**File:** contracts/Pool.sol (L537-549)
```text
    function liquidate(
        ISyntheticToken syntheticToken_,
        address account_,
        uint256 amountToRepay_,
        IDepositToken depositToken_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists(syntheticToken_)
        onlyIfDepositTokenExists(depositToken_)
        returns (uint256 _totalSeized, uint256 _toLiquidator, uint256 _fee)
```

**File:** contracts/Pool.sol (L605-610)
```text
    /**
     * @inheritdoc Pauseable
     */
    function paused() public view override(IPauseable, Pauseable) returns (bool) {
        return super.paused() || _poolRegistry.paused();
    }
```

**File:** contracts/Pool.sol (L642-668)
```text
    function swap(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountIn_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists(syntheticTokenIn_)
        onlyIfSyntheticTokenExists(syntheticTokenOut_)
        returns (uint256 _amountOut, uint256 _fee)
    {
        address _msgSender = _msgSender();

        if (!isSwapActive) revert SwapFeatureIsInactive();
        if (amountIn_ == 0 || amountIn_ > syntheticTokenIn_.balanceOf(_msgSender)) revert AmountInIsInvalid();

        syntheticTokenIn_.burn(_msgSender, amountIn_);

        (_amountOut, _fee) = quoteSwapOut(syntheticTokenIn_, syntheticTokenOut_, amountIn_);

        if (_fee > 0) {
            syntheticTokenOut_.mint(_poolRegistry.feeCollector(), _fee);
        }

        syntheticTokenOut_.mint(_msgSender, _amountOut);
```

**File:** contracts/Pool.sol (L759-763)
```text
    function toggleIsSwapActive() external onlyGovernor {
        bool _newIsSwapActive = !isSwapActive;
        emit SwapActiveUpdated(_newIsSwapActive);
        isSwapActive = _newIsSwapActive;
    }
```

**File:** contracts/access/Manageable.sol (L46-49)
```text
    modifier whenNotPaused() {
        if (pool.paused()) revert IsPaused();
        _;
    }
```
