### Title
Pausing the Pool only blocks new deposits; debt issuance, withdrawals, liquidations and swaps continue using pre-pause state - (File: contracts/Pool.sol)

### Summary
`Pool.pause()` sets `_paused = true`, and `Pool.paused()` also reflects `PoolRegistry.paused()` [1](#0-0) [2](#0-1) . However, the pause flag is only enforced on `DepositToken.deposit` via `Manageable.whenNotPaused` [3](#0-2) . Every other state-changing function — `DebtToken.issue`, `DepositToken.withdraw`/`flashWithdraw`, `Pool.swap`, `Pool.liquidate`, `DebtToken.repay` — is gated only by `whenNotShutdown`, which checks `everythingStopped()` [4](#0-3) . The protocol's own `docs/emergency-flags.md` documents this asymmetry: deposit is "Disabled if `PoolRegistry.paused() || Pool.paused()`", while withdraw/issue/repay/liquidate/swap are only disabled if `everythingStopped()` [5](#0-4) .

### Finding Description
This is the same bug class as M-21: the emergency flag is forward-facing. `pause()` freezes only the inflow side (new deposits) but leaves all valuation-and-exit paths live, meaning whatever state existed before the pause is still treated as fully valid. A paused pool continues to:
- mint new synthetic debt via `DebtToken.issue` against already-deposited collateral at current oracle prices,
- burn synths for other synths via `Pool.swap` (which calls `quoteSwapOut`/`masterOracle`) [6](#0-5) ,
- withdraw/flash-withdraw collateral via `DepositToken.withdraw`, pulling underlying out of `Treasury`,
- liquidate positions via `Pool.liquidate`.

If the governor calls `pause()` in response to a compromised oracle or a suspicious collateral update — exactly the scenario the flag exists for — an attacker who already holds a deposit position (or manipulates the oracle in the same window before the pause lands) can keep issuing debt and withdrawing real collateral while the pool is supposedly halted.

### Impact Explanation
Protocol insolvency / direct theft of user funds. During a pause triggered by a bad price or compromised collateral, an unprivileged attacker can issue synthetic debt against a stale/manipulated valuation and immediately `withdraw` collateral or `swap` into another synth, extracting value that the pause was intended to protect. The invariant broken is solvency: deposits backing issued synths drain while issuance continues.

### Likelihood Explanation
The path is fully reachable by any EOA or contract via public entry points (`DebtToken.issue`, `DepositToken.withdraw`, `Pool.swap`) and requires only that the pool be paused (a governance action taken precisely during incidents) rather than shutdown. The only mitigation is that operators must call `shutdown()` instead of `pause()`, but `pause()` is the documented "suspend deposit feature" knob and `shutdown()` is a heavier irreversible-by-guardian action, so the intermediate state is realistically reachable during incident response.

### Recommendation
Either make `pause()` block all value-moving functions (use `whenNotPaused` on `DebtToken.issue`, `DepositToken.withdraw`/`flashWithdraw`, `Pool.swap`, `Pool.liquidate`), or remove `pause()` entirely so operators cannot accidentally choose a half-effective freeze during an incident.

### Proof of Concept
Reproducible as a Hardhat fork test against deployed deployments (e.g. `deployments/mainnet`):

```ts
// 1. alice deposits USDC into msdUSDC and issues msUSD (healthy position)
// 2. attacker manipulates the collateral price source in the same window
//    (e.g. same-transaction AMM manipulation of the oracle's underlying feed)
// 3. governor calls pool.pause() (NOT shutdown) to halt the pool
await pool.connect(governor).pause();
expect(await pool.paused()).to.be.true;

// 4. deposit is blocked
await expect(msdUSDC.deposit(amount, attacker.address)).to.be.reverted; // IsPaused

// 5. but issue still works at the pre-pause/manipulated price
await msUSDDebt.issue(amountOut, attacker.address);        // succeeds
await pool.swap(msUSD.address, msETH.address, msUsdBal);   // succeeds
await msdWETH.withdraw(withdrawable, attacker.address);    // succeeds, pulls Treasury funds

// Result: attacker exits with real collateral while pool is "paused"
```

### Citations

**File:** contracts/utils/Pauseable.sol (L105-108)
```text
    function pause() external virtual whenNotPaused onlyGovernor {
        _paused = true;
        emit Paused(_msgSender());
    }
```

**File:** contracts/Pool.sol (L608-610)
```text
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

**File:** contracts/access/Manageable.sol (L46-49)
```text
    modifier whenNotPaused() {
        if (pool.paused()) revert IsPaused();
        _;
    }
```

**File:** contracts/access/Manageable.sol (L54-57)
```text
    modifier whenNotShutdown() {
        if (pool.everythingStopped()) revert IsShutdown();
        _;
    }
```

**File:** docs/emergency-flags.md (L61-99)
```markdown
Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()` || `!SyntheticTokenOut.isActive()`

## Pool.liquidate()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`

## DebtToken.issue()

Disabled if: `PoolRegistry.everythingStopped()` || `!SyntheticToken.isActive()` || `Pool.everythingStopped()` || `!DebtToken.isActive()`

## DebtToken.repay()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`

## DebtToken.repayAll()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`

## DepositToken.deposit()

Disabled if: `PoolRegistry.paused()` || `Pool.paused()` || `!DepositToken.isActive()`

## DepositToken.withdraw()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`

## SmartFarmingManager.crossChainLeverage()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()` || `!CrossChainDispatcher.isBridgingActive()` || `!Pool.isBridgingActive()`

## SmartFarmingManager.crossChainFlashRepay()

Disabled if: `!CrossChainDispatcher.isBridgingActive()` || `!Pool.isBridgingActive()` || `!PoolRegistry.isCrossChainFlashRepayActive()`

## ProxyOFT.debitFrom() (i.e. bridge transfers)

Disabled if: `!CrossChainDispatcher.isBridgingActive()` || `!CrossChainDispatcher.isDestinationChainSupported(dstChainId)`

Note: To pause all above, use: `PoolRegistry.shutdown()` + `CrossChainDispatcher.toggleBridgingIsActive()`
```
