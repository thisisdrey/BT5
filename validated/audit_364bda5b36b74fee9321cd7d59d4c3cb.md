### Title
Governance fee updates take effect immediately, allowing users to front-run `updateSwapFee` / `updateIssueFee` and pay the stale lower fee — (File: contracts/FeeProvider.sol)

### Summary
`FeeProvider` applies new fee values atomically in the same transaction that sets them. Any user can monitor the mempool for `updateSwapFee`, `updateIssueFee`, `updateRepayFee`, `updateWithdrawFee`, or `updateDepositFee` calls and front-run them with `Pool.swap`, debt issuance, repayment, or withdrawal transactions that are charged the old (lower) fee.

### Finding Description
All fee setters in `FeeProvider` write the new value directly to storage with no effective delay or timelock:

- `updateIssueFee` writes `issueFee = newIssueFee_` immediately — [1](#0-0) 
- `updateSwapFee` writes `swapFees[synthIn_][synthOut_] = newSwapFee_` immediately — [2](#0-1) 
- `updateRepayFee` and `updateWithdrawFee` behave identically — [3](#0-2) 

On the consumer side, `Pool.swap` reads the fee from the `FeeProvider` at execution time, so a swap included in the same block *before* the governor's update transaction is priced at the old fee. `swap` is a permissionless entry point guarded only by `whenNotShutdown`, `nonReentrant`, and `onlyIfSyntheticTokenExists` — none of which block front-running — [4](#0-3) . The same applies to `DebtToken` issuance (`issueFee`) and repayments (`repayFee`), and to `DepositToken` withdraw/deposit fees, all of which read `FeeProvider` state at call time.

Concretely: if `swapFees[msETH][msUSD]` is 0.55% (as configured on mainnet) and the governor submits `updateSwapFee(msETH, msUSD, 5%)`, a holder of msETH sees the pending tx, submits `Pool.swap(msETH, msUSD, X)` with higher priority gas, and pays 0.55% instead of 5% on the full amount. There is no per-tx size limit beyond the attacker's synth balance, and flash-issued debt via `DebtToken.flashIssue`/`SmartFarmingManager.leverage` can amplify the swapped amount within the same transaction. The protocol `feeCollector` receives `amountIn * oldFee` instead of `amountIn * newFee`.

### Impact Explanation
The protocol (feeCollector) collects less fee revenue than the governance-intended rate whenever a fee increase is broadcast on a public mempool. The lost amount is `volume * (newFee - oldFee)` per front-run transaction, bounded by `MAX_FEE_VALUE = 25%`. This is a direct reduction of protocol revenue (unclaimed yield) rather than user-fund theft, matching the severity profile of the reference issue.

### Likelihood Explanation
Front-running requires only observing a pending governor transaction — no privileged role. However, exploitation windows only exist when fees are actually raised, governors may use private mempools/multisig batches, and profit is capped by the fee delta times available synth liquidity. Medium-low likelihood, low-medium impact per occurrence.

### Recommendation
Apply a delayed-effectiveness pattern for fee increases, e.g. store `pendingFee` + `effectiveTimestamp` and activate after a delay, or route fee changes through a timelock so users cannot selectively transact under the old rate after the change is known. Alternatively, commit fee changes with the new rate taking effect from `block.timestamp + delay` while decreases can remain instant.

### Proof of Concept
```solidity
// Foundry fork test sketch
// 1. Setup: feeProvider.swapFees(msETH, msUSD) == 0.0055e18 (mainnet config)
// 2. Attacker holds (or flash-mints) msETH
// 3. Governor broadcasts updateSwapFee(msETH, msUSD, 0.05e18)
// 4. Attacker front-runs in the same block:
(uint256 amountOut, uint256 fee) = pool.swap(msETH, msUSD, attackerBalance);
// 5. Assert fee == attackerBalance * 0.0055e18 / 1e18  (old fee applied)
//    not  attackerBalance * 0.05e18 / 1e18
// 6. Governor tx then lands; subsequent swaps pay 5%.
```

Note: I was unable to read the exact lines where `Pool.swap` computes the fee via `FeeProvider.swapFees` (it occurs just after line 660 in `Pool.swap`/`quoteSwapOut`), so the precise fee-application line is inferred rather than directly cited.

### Citations

**File:** contracts/FeeProvider.sol (L91-97)
```text
    function updateIssueFee(uint256 newIssueFee_) external onlyGovernor {
        if (newIssueFee_ > MAX_FEE_VALUE) revert FeeIsGreaterThanTheMax();
        uint256 _currentIssueFee = issueFee;
        if (newIssueFee_ == _currentIssueFee) revert NewValueIsSameAsCurrent();
        emit IssueFeeUpdated(_currentIssueFee, newIssueFee_);
        issueFee = newIssueFee_;
    }
```

**File:** contracts/FeeProvider.sol (L126-154)
```text
    function updateRepayFee(uint256 newRepayFee_) external onlyGovernor {
        if (newRepayFee_ > MAX_FEE_VALUE) revert FeeIsGreaterThanTheMax();
        uint256 _currentRepayFee = repayFee;
        if (newRepayFee_ == _currentRepayFee) revert NewValueIsSameAsCurrent();
        emit RepayFeeUpdated(_currentRepayFee, newRepayFee_);
        repayFee = newRepayFee_;
    }

    /**
     * @notice Update swap fee
     */
    function updateSwapFee(address synthIn_, address synthOut_, uint256 newSwapFee_) external onlyGovernor {
        if (newSwapFee_ > MAX_FEE_VALUE) revert FeeIsGreaterThanTheMax();
        uint256 _current = swapFees[synthIn_][synthOut_];
        if (newSwapFee_ == _current) revert NewValueIsSameAsCurrent();
        emit SwapFeeUpdated(synthIn_, synthOut_, _current, newSwapFee_);
        swapFees[synthIn_][synthOut_] = newSwapFee_;
    }

    /**
     * @notice Update withdraw fee
     */
    function updateWithdrawFee(uint256 newWithdrawFee_) external onlyGovernor {
        if (newWithdrawFee_ > MAX_FEE_VALUE) revert FeeIsGreaterThanTheMax();
        uint256 _currentWithdrawFee = withdrawFee;
        if (newWithdrawFee_ == _currentWithdrawFee) revert NewValueIsSameAsCurrent();
        emit WithdrawFeeUpdated(_currentWithdrawFee, newWithdrawFee_);
        withdrawFee = newWithdrawFee_;
    }
```

**File:** contracts/Pool.sol (L642-660)
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
```
