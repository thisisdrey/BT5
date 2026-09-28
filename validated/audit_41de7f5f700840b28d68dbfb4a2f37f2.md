### Title
`Pool.swap` executes oracle-priced swaps with no user-supplied minimum-out, exposing swappers to oracle-update front-running and same-transaction price manipulation - (File: contracts/Pool.sol)

### Summary
`Pool.swap` burns a synthetic token and mints another purely at the `MasterOracle` quote, but the signature (`swap(syntheticTokenIn_, syntheticTokenOut_, amountIn_)`) carries no `amountOutMin_` slippage parameter. Any movement of the quoted price between transaction submission and execution — a pending Chainlink/RedStone update, a pull-oracle price push, or same-transaction manipulation of a manipulable sub-oracle (e.g. LP/vault-share based providers wired into `MasterOracle`) — silently reprices the trade, and the protocol mints `syntheticTokenOut_` at the drifted rate.

### Finding Description
`swap` validates only `isSwapActive` and `amountIn_ <= balance`, then burns `syntheticTokenIn_` and calls `quoteSwapOut`, which computes `_amountOut` entirely from `_poolRegistry.masterOracle().quote(...)` and the configured swap fee [1](#0-0) [2](#0-1) . The output amount is minted unconditionally — there is no caller-defined floor.

Contrast with the SmartFarmingManager paths, where slippage protection was explicitly designed in: `flashRepay` takes `swapAmountOutMin_` and reverts `FlashRepaySlippageTooHigh` if the swap result is below it [3](#0-2) , and `leverage` takes `depositAmountMin_` [4](#0-3) . The direct `Pool.swap` entry point offers no equivalent guard, and the `IPool.swap` interface confirms no min-out parameter exists [5](#0-4) .

Attack path (unprivileged): an attacker holding `msUSD` observes a pending oracle update that will raise `msETH`'s USD price (or a price-provider value derived from an on-chain rate that can be moved in-transaction). In one bundle they swap `msUSD → msETH` at the stale low quote via `pool.swap(msUSD, msETH, amountIn)`, the update executes, and they swap back at the new quote. Each round trip mints synthetic value in excess of what was burned, extracting the difference from the shared collateral backing of all synthetic-token holders. Symmetrically, a regular user's `swap` transaction landing after an adverse oracle update delivers less `syntheticTokenOut_` than quoted at submission, with no way to bound the loss.

The relevant modifiers do not prevent this: `whenNotShutdown`, `nonReentrant`, and the `onlyIfSyntheticTokenExists` checks are orthogonal to pricing, and `syntheticTokenOut_.mint` is subject only to the synth's own supply cap, which bounds but does not price the mint [6](#0-5) .

### Impact Explanation
Each mispriced swap mints more `syntheticTokenOut_` than the burned `syntheticTokenIn_` is worth in USD terms (or vice versa for the victim user), directly diluting the collateral pool backing all synthetics. Repeated extraction accumulates into bad debt: synthetic supply exceeds oracle-implied collateral backing, harming redeemers and depleting collateral available for liquidation payouts. For the plain-user variant, the loss is direct theft of expected output on a single swap.

### Likelihood Explanation
Requires the swap feature to be enabled (`isSwapActive`) and an oracle price movement exploitable in a single block — Chainlink update front-running and in-flight pull-oracle updates are well-documented MEV vectors, and several oracle providers in scope (Curve `get_virtual_price`, vault-share exchange rates) are same-transaction manipulable. The attacker needs no privileges, no governance action, and only synthetic-token balance, obtainable via normal `DebtToken.issue`. Capital and gas costs are modest relative to extractable slippage on large swaps, so likelihood is moderate.

### Recommendation
Add a `uint256 amountOutMin_` parameter to `Pool.swap` (and `IPool.swap`), and revert if `_amountOut < amountOutMin_`, mirroring the `FlashRepaySlippageTooHigh` pattern already used in `SmartFarmingManager.flashRepay`. Optionally also add a `deadline`. Frontends and `Operator.execute` batched calls can pass `quoteSwapOut` results minus a user-chosen tolerance.

### Proof of Concept
Foundry fork sketch on mainnet (contracts are upgradeable proxies; use deployed `Pool`, `msUSD`, `msETH`, `MasterOracle` addresses as in `deployments/mainnet/Pool.json`):

```solidity
// test/foundry/poc/SwapSlippage.t.sol
function test_swap_no_min_out_oracle_frontrun() public {
    // setup: attacker holds msUSD (obtained via deposit + DebtToken.issue)
    uint256 amountIn = 1_000_000e18; // 1M msUSD

    uint256 quotedBefore = pool.quoteSwapOut(msUSD, msETH, amountIn)._amountOut;

    // 1) Attacker bundles: swap BEFORE pending oracle update raises msETH/USD
    pool.swap(msUSD, msETH, amountIn);          // minted at stale (cheap) msETH price
    uint256 msEthGot = msETH.balanceOf(attacker);

    // 2) Oracle update lands (Chainlink push / pull-oracle update in same or next tx)
    pushNewEthPrice(higherPrice);               // e.g. +2%

    // 3) Swap back; more msUSD minted than initially burned
    uint256 msUsdBefore = msUSD.balanceOf(attacker);
    pool.swap(msETH, msUSD, msEthGot);
    uint256 profit = msUSD.balanceOf(attacker) - msUsdBefore - amountIn;

    // attacker ends with more USD value than started, extracted from collateral backing
    assertGt(profit, 0);
}
```

Note the asymmetry that makes this exploitable: `swap` mints `_amountOut` with no floor [7](#0-6) , so neither leg reverts even though the execution price differed from any price the caller could have bound. A "victim" variant of the PoC calls `quoteSwapOut`, submits `swap`, inserts an oracle update in between, and shows the received `_amountOut` is materially below the quote with no revert path.

### Citations

**File:** contracts/Pool.sol (L508-525)
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
    }
```

**File:** contracts/Pool.sol (L642-670)
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

**File:** contracts/SmartFarmingManager.sol (L155-169)
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
```

**File:** contracts/interfaces/IPool.sol (L100-104)
```text
    function swap(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountIn_
    ) external returns (uint256 _amountOut, uint256 _fee);
```
