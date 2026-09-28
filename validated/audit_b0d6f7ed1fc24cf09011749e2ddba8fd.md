### Title
`Pool.swap` prices oracle-based synthetic exchanges without any slippage protection - ([File: contracts/Pool.sol])

### Summary
`Pool.swap(syntheticTokenIn_, syntheticTokenOut_, amountIn_)` is the direct Metronome analog of Rio's unprotected `deposit`/`depositETH`: the amount of `syntheticTokenOut_` minted to the caller is determined entirely by `masterOracle().quote()` inside `quoteSwapOut` at execution time, and the function exposes no `minAmountOut` (or `maxAmountIn`) parameter. A user cannot bound the exchange rate they accept, and an attacker who can move the oracle quote (same-transaction manipulation) can extract value or sandwich pending swaps.

### Finding Description [1](#0-0) 

`swap` performs:

1. `syntheticTokenIn_.burn(_msgSender, amountIn_)` — the user's input synth is burned unconditionally.
2. `(_amountOut, _fee) = quoteSwapOut(...)` — which calls `_poolRegistry.masterOracle().quote(syntheticTokenIn_, syntheticTokenOut_, amountIn_)` (`contracts/Pool.sol:513-517`) and deducts `feeProvider.swapFees(...)`.
3. `syntheticTokenOut_.mint(_msgSender, _amountOut)` — output minted at whatever the oracle says at execution time.

The interface confirms no slippage parameter exists: `swap(ISyntheticToken, ISyntheticToken, uint256)` returns `(_amountOut, _fee)` with no bound argument ( [2](#0-1) ). Notably, the codebase *does* implement this protection elsewhere — `SmartFarmingManager.flashRepay` enforces `swapAmountOutMin_` and reverts `FlashRepaySlippageTooHigh` ( [3](#0-2) ), and `leverage` enforces `depositAmountMin_` ( [4](#0-3) ) — so the omission on `Pool.swap` is inconsistent with the protocol's own design pattern.

Two exploitation modes:

- **Victim sandwich**: a user's `swap` sits in the mempool priced off the oracle. If any leg of the MasterOracle route for a synth pair uses a same-transaction-manipulable source (e.g., a vault-share/ERC4626 exchange rate movable by donation, or a DEX spot/TWAP window), an attacker moves the quote against the victim before execution and restores it after. The victim receives fewer `syntheticTokenOut_` than the fair value of what was burned, with no way to revert — funds loss identical to the Rio report.
- **Direct extraction**: the attacker manipulates the quote in their own favor, calls `swap`, mints more `syntheticTokenOut_` than the burned `syntheticTokenIn_` is worth, then unwinds the manipulation. The excess minted synth is backed by the pool's existing collateral, breaking solvency.

Existing guards do not stop this: `isSwapActive`, `onlyIfSyntheticTokenExists`, `whenNotShutdown` and `nonReentrant` are all satisfied during normal operation; there is no health check (no debt position is touched) and no bounds check on `_amountOut`.

### Impact Explanation
Direct theft of user funds (sandwiched swap yields less synth out) and/or protocol insolvency (attacker mints synth out worth more than synth in burned). The broken invariant is the conservation-of-value of the oracle-priced exchange: `valueIn == valueOut + fee` is only enforced by the oracle, which the user cannot bound.

### Likelihood Explanation
Requires a synth-pair oracle leg that is same-transaction manipulable, or simply organic price movement between mempool submission and inclusion for the victim-harm variant. The rules explicitly allow same-transaction manipulation and reject only incorrect third-party data. Caveat: the concrete `IMasterOracle` implementation is external to this repo (only `contracts/interfaces/external/IMasterOracle.sol` is in scope), so whether a deployed synth pair routes through a manipulable source must be confirmed against the deployed `masterOracle()` address on each chain (deployments exist for mainnet, optimism, base, hemi, swell). If all legs are pure Chainlink aggregators, the direct-extraction mode reduces to the mempool-slippage mode, which remains the same bug class as the Rio finding.

### Recommendation
Add a `minAmountOut_` parameter to `swap` (and optionally a `maxAmountIn_` variant using `quoteSwapIn`):

```solidity
function swap(
    ISyntheticToken syntheticTokenIn_,
    ISyntheticToken syntheticTokenOut_,
    uint256 amountIn_,
    uint256 minAmountOut_
) external ... {
    ...
    (_amountOut, _fee) = quoteSwapOut(syntheticTokenIn_, syntheticTokenOut_, amountIn_);
    if (_amountOut < minAmountOut_) revert SlippageTooHigh();
    ...
}
```

Alternatively expose an `amountOut`-exact swap that derives `amountIn_` via `quoteSwapIn`.

### Proof of Concept
Hardhat fork sketch (fork mainnet; addresses from `deployments/mainnet/`):

```typescript
// Pool at deployments/mainnet/Pool.json; synth tokens msETH/msUSD, MasterOracle = poolRegistry.masterOracle()
// Victim-harm variant:
const quoteBefore = await pool.quoteSwapOut(msETH, msUSD, amountIn);

// Attacker manipulates the oracle leg for msETH (e.g., donation to the underlying
// vault whose exchange rate feeds MasterOracle, or a flash-loan move of a spot/TWAP provider)
await manipulateOracleLeg(/* lower msETH/msUSD quote */);

// Victim's swap executes; has no minAmountOut to protect itself
await pool.connect(victim).swap(msETH.address, msUSD.address, amountIn);
const received = await msUSD.balanceOf(victim.address);
assert(received < quoteBefore._amountOut); // loss, no revert possible

// Attacker unwinds the manipulation and profits on the back-run
await unwindManipulation();
```

Direct-extraction variant: attacker manipulates the quote upward for `syntheticTokenOut_`, calls `swap` with flash-borrowed `syntheticTokenIn_` (mintable via `DebtToken.issue`/`flashIssue` against deposited collateral in the same tx), receives excess `syntheticTokenOut_`, repays the debt, and keeps the difference — value drained from pooled collateral.

A fully deterministic fork PoC requires knowing which oracle provider backs the chosen synth pair on the target chain; with the mock `MasterOracleMock`/`SwapperMock`-style wiring in `test/`, the missing bound is trivially demonstrated by any quote change between `quoteSwapOut` and `swap`.

### Citations

**File:** contracts/Pool.sol (L642-671)
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

        emit SyntheticTokenSwapped(_msgSender, syntheticTokenIn_, syntheticTokenOut_, amountIn_, _amountOut, _fee);
    }
```

**File:** contracts/interfaces/IPool.sol (L100-104)
```text
    function swap(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountIn_
    ) external returns (uint256 _amountOut, uint256 _fee);
```

**File:** contracts/SmartFarmingManager.sol (L98-127)
```text
    function flashRepay(
        ISyntheticToken syntheticToken_,
        IDepositToken depositToken_,
        uint256 withdrawAmount_,
        uint256 swapAmountOutMin_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfDepositTokenExists(depositToken_)
        onlyIfSyntheticTokenExists(syntheticToken_)
        returns (uint256 _withdrawn, uint256 _repaid)
    {
        ISyntheticToken _syntheticToken = syntheticToken_; // stack too deep

        address _msgSender = _msgSender();
        if (withdrawAmount_ == 0) revert AmountIsZero();
        if (withdrawAmount_ > depositToken_.balanceOf(_msgSender)) revert AmountIsTooHigh();
        IPool _pool = pool;
        IDebtToken _debtToken = _pool.debtTokenOf(_syntheticToken);
        if (swapAmountOutMin_ > _debtToken.balanceOf(_msgSender)) revert AmountIsTooHigh();

        // 1. withdraw collateral
        (_withdrawn, ) = depositToken_.flashWithdraw(_msgSender, withdrawAmount_);

        // 2. swap it for synth
        uint256 _swapAmountOut = _swap(swapper(), _collateralOf(depositToken_), _syntheticToken, _withdrawn, 0);
        if (_swapAmountOut < swapAmountOutMin_) revert FlashRepaySlippageTooHigh();

```

**File:** contracts/SmartFarmingManager.sol (L155-173)
```text
    function leverage(
        IERC20 tokenIn_,
        IDepositToken depositToken_,
        ISyntheticToken syntheticToken_,
        uint256 amountIn_,
        uint256 leverage_,
        uint256 depositAmountMin_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfDepositTokenExists(depositToken_)
        onlyIfSyntheticTokenExists(syntheticToken_)
        returns (uint256 _deposited, uint256 _issued)
    {
        if (amountIn_ == 0) revert AmountIsZero();
        if (leverage_ <= 1e18) revert LeverageTooLow();
        if (leverage_ > uint256(1e18).wadDiv(1e18 - depositToken_.collateralFactor())) revert LeverageTooHigh();
```
