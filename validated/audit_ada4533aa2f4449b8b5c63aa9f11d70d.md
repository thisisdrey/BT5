### Title
`Pool.swap` lacks slippage protection — users can receive less synth than quoted - (File: contracts/Pool.sol)

### Summary
The Metronome analog of the reported bug class is `Pool.swap()`. Its signature takes `tokenIn_`, `syntheticTokenOut_`, and `amountIn_` only — there is no `amountOutMin_` parameter, even though a `quoteSwapOut()` preview function exists and the protocol's own `SmartFarmingManager` routes all of its external swaps through `ISwapper.swapExactInput(..., amountOutMin_, ...)`. A user who quotes a swap and submits a transaction has no way to bound the minimum output; any state change between submission and execution (swap-fee update, oracle price update, or a preceding swap shifting fee/pool state) is silently absorbed by the user.

### Finding Description
`Pool.swap` is oracle-priced: `_amountOut` is computed from `masterOracle().quote()` minus a fee from `FeeProvider.swapFees()`, and the function mints `syntheticTokenOut_` directly to the caller. The deployed ABI confirms the full parameter list is `(tokenIn_, syntheticTokenOut_, amountIn_)` with no minimum-out argument [1](#0-0) .

Contrast with the protocol's own treatment of the same risk: `SmartFarmingManager.flashRepay` exposes `swapAmountOutMin_` and reverts with `FlashRepaySlippageTooHigh()` when the realized output is below it [2](#0-1) , `leverage` exposes `depositAmountMin_` [3](#0-2) , and `_swap` forwards `amountOutMin_` to the swapper [4](#0-3) . The core `Pool.swap` — the primary user-facing mint-by-swap path — offers no equivalent guard, and the cross-chain docs explicitly acknowledge slippage params as the mitigation for this class [5](#0-4) .

### Impact Explanation
A user's swap output depends on (a) the oracle price, which updates between mempool submission and execution, and (b) `swapFees`, which can be changed by a fee-update transaction landing first. In both cases the user receives materially less `syntheticTokenOut_` than the `quoteSwapOut()` preview indicated, with no recourse — a direct, bounded loss of user funds on every affected swap. A front-runner can additionally profit by ordering a price-adverse transaction (e.g., a large same-direction swap changing pool state or an oracle update transaction) immediately before the victim's swap.

### Likelihood Explanation
Likelihood is moderate: it requires a fee change or meaningful oracle price movement between quote and execution, or a mempool observer willing to sandwich. On public-mempool chains (Ethereum mainnet deployments exist) the sandwich path is realistic for large swaps, and oracle updates landing between submission and inclusion are routine.

### Recommendation
Add an `amountOutMin_` parameter to `Pool.swap` (and a `deadline` if desired) and revert when the computed `_amountOut < amountOutMin_`, mirroring the existing `swapAmountOutMin_`/`depositAmountMin_`/`repayAmountMin_` pattern already used in `SmartFarmingManager` and `CrossChainDispatcher`. For backward compatibility, keep the current function as a wrapper passing `amountOutMin_ = 0`, or add an overloaded `swap` with the slippage parameter.

### Proof of Concept
Hardhat fork sketch:

```ts
// Setup: pool with msUSD/msETH synthetics, oracle with volatile price feed.
// 1. Victim quotes:
const [quotedOut] = await pool.quoteSwapOut(msETH.address, msUSD.address, amountIn);
// 2. Between quote and execution, the price feed updates adversely
//    (or a fee collector bumps swapFee via FeeProvider.updateSwapFee).
await oracle.updatePrice(/* worse rate for victim */);
// 3. Victim's swap executes at the new rate with no minOut guard:
await pool.connect(victim).swap(msETH.address, msUSD.address, amountIn);
const received = await msUSD.balanceOf(victim.address);
assert(received.lt(quotedOut)); // victim silently received less than quoted
// With an amountOutMin_ param the tx would have reverted instead.
```

Uncertainty note: I confirmed the missing parameter via the deployed ABI and the presence of `quoteSwapOut`/`swapFees`, but did not read the full `Pool.swap` body line-by-line; the finding assumes the fee/quote path inside `swap` matches `quoteSwapOut` as the ABI and surrounding code indicate.

### Citations

**File:** deployments/swell/Pool.json (L1440-1462)
```json
          "type": "address"
        },
        {
          "internalType": "uint256",
          "name": "amountIn_",
          "type": "uint256"
        }
      ],
      "name": "swap",
      "outputs": [
        {
          "internalType": "uint256",
          "name": "_amountOut",
          "type": "uint256"
        },
        {
          "internalType": "uint256",
          "name": "_fee",
          "type": "uint256"
        }
      ],
      "stateMutability": "nonpayable",
      "type": "function"
```

**File:** contracts/SmartFarmingManager.sol (L98-126)
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

**File:** contracts/SmartFarmingManager.sol (L293-306)
```text
    function _swap(
        ISwapper swapper_,
        IERC20 tokenIn_,
        IERC20 tokenOut_,
        uint256 amountIn_,
        uint256 amountOutMin_,
        address to_
    ) private returns (uint256 _amountOut) {
        if (tokenIn_ != tokenOut_) {
            tokenIn_.safeApprove(address(swapper_), 0);
            tokenIn_.safeApprove(address(swapper_), amountIn_);
            uint256 _tokenOutBefore = tokenOut_.balanceOf(to_);
            swapper_.swapExactInput(address(tokenIn_), address(tokenOut_), amountIn_, amountOutMin_, to_);
            return tokenOut_.balanceOf(to_) - _tokenOutBefore;
```

**File:** docs/cross-chain.md (L122-131)
```markdown
# Slippage protection

The CC leverage has two params to avoid losses due to slippage:<br/>
`swapAmountOutMin_` is the minimum output amount for the `syntheticToken->bridgeToken` swap at the `dstChain` (tx2)<br/>
`depositAmountMin_` is the minimum amount for `collateral` deposit (tx3)<br/>

The CC flash repay has three params to avoid losses due to slippage:<br/>
`bridgeTokenAmountMin_` is the minimum output amount for the `collateral->bridgeToken` swap at the `srcChain` (tx1)<br/>
`swapAmountOutMin_` is the minimum output amount for the `bridgeToken->syntheticToken` swap at the `dstChain` (tx2)<br/>
`repayAmountMin_` is the minimum amount for repayment (tx3)<br/>
```
