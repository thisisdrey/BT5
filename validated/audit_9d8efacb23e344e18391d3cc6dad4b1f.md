### Title
Stale-oracle sandwich on `Pool.swap` mints unbacked synthetic value - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.swap` burns one synthetic asset and mints another at the instantaneous `MasterOracle` rate, with no staleness check of its own and a per-pair fee that defaults to 0. Because the pull-oracle price updates (e.g., Pyth `updatePriceFeeds`) are permissionless and visible in the mempool, an unprivileged user can front-run a price update with a swap at the stale price, extracting value at the expense of all synthetic-token holders, whose aggregate supply becomes undercollateralized. This is the same bug class as the Ubiquity pool mint/redeem sandwich: two oracle-priced assets are exchanged atomically at a stale rate.

### Finding Description
`Pool.initialize` enables swapping by default (`isSwapActive = true`). [1](#0-0) 

`Pool.swap` burns `amountIn_` of `syntheticTokenIn_`, prices the trade via `quoteSwapOut`, and mints `syntheticTokenOut_` to the caller: [2](#0-1) 

`quoteSwapOut` converts `amountIn_` straight through `masterOracle().quote(assetIn, assetOut)` and only deducts `feeProvider.swapFees(...)`: [3](#0-2) 

`swapFees` is a per-pair mapping set only by governor via `updateSwapFee`; unset pairs are 0, and `MAX_FEE_VALUE` is capped at 25%, so small oracle deviations are never covered: [4](#0-3) 

There is no freshness/staleness guard inside `Pool.swap`, no min-out slippage parameter, and no restriction on when a user may swap relative to an oracle update. The only guards are `whenNotShutdown`, `nonReentrant`, token-existence checks, and the `isSwapActive` flag — none of which prevent trading against a stale quote. Price updates on the pull oracle are public transactions, so a pending update is visible and sandwichable.

Attack trace:
1. Attacker holds (or market-buys) `msETH`; mempool shows a pending `updatePriceFeeds` lowering ETH price (e.g., $3000 → $2700).
2. Front-run: call `pool.swap(msETH, msUSD, 1e18)`. Oracle still quotes msETH at $3000 → attacker receives 3000 msUSD (0 or small fee). The burned msETH is worth only $2700 post-update.
3. The update executes; attacker repeats whenever profitable. Every swap at the stale rate minted more `syntheticTokenOut_` than the burned input is worth.

### Impact Explanation
Synthetic tokens are liabilities backed by borrowers' collateral (`debtPositionOf` enforces `debtInUsd <= issuableLimitInUsd` only for minting debt). `swap` mints new synths against burned synths — not against collateral — so mispriced swaps create excess synthetic supply with no backing. Each profitable sandwich leaves the pool holding a USD-denominated deficit: total synth liabilities exceed total collateral backing, i.e., protocol insolvency transferred to all synthetic holders (identical to the Ubiquity report, where Dollar holders were left with the devalued collateral). Magnitude scales with attacker capital and the size of the price update; flash-borrowed or market-bought synth inventory is recyclable across every feed update.

### Likelihood Explanation
Requires only that (a) a pending oracle update moves a synth's price by more than the pair's swap fee (often 0), and (b) the attacker holds the depreciating synth. Pull-oracle update transactions are public and frequent on the deployed configuration (mainnet/base/hemi deployments use Pyth/RedStone pull feeds, and `Operator.execute` even documents update-then-act batched flows). Any MEV bot watching the mempool can execute this without privileges.

### Recommendation
- Charge a non-zero baseline `swapFee` on all pairs sized to exceed expected single-update price drift.
- Add a per-call staleness bound in `swap`/`quoteSwapOut` (reject quotes older than `maxDelay`) independent of the oracle's own expiry.
- Optionally require the caller to pass `minAmountOut_` and enforce a time-weighted or multi-source price for swaps.

### Proof of Concept
Foundry-style test against the existing harness (`MasterOracleMock` used by `test/foundry/Pool.invariants.t.sol`):

```solidity
// setUp: pool with msETH, msUSD synths + debt tokens; MasterOracleMock at
// eth=$3000, usd=$1; alice holds 1 msETH issued against collateral.

function test_staleOracleSwapSandwich() public {
    // 1. Alice swaps at stale $3000 price (front-runs the update tx)
    vm.prank(alice);
    pool.swap(msETH, msUSD, 1e18);
    assertEq(msUSD.balanceOf(alice), 3000e18); // minted at stale rate

    // 2. Pending price update executes
    masterOracle.updatePrice(address(msETH), 2700e18);

    // 3. Alice's msUSD is still redeemable at $1; the 1 msETH she burned
    //    was worth only $2700 -> $300 of unbacked msUSD supply exists.
    uint256 liabilitiesUsd = msUSD.totalSupply().wadMul(1e18)
        + msETH.totalSupply().wadMul(2700e18);
    // collateral backing did not change -> liabilities exceed backing by $300
    assertGt(liabilitiesUsd, collateralValueUsd + 299e18);
}
```

The same reproduction applies on a mainnet fork by sandwiching a real `Pyth.updatePriceFeeds` transaction around `Pool.swap` on the deployed `Pool` at `deployments/mainnet/Pool.json`.

### Citations

**File:** contracts/Pool.sol (L175-182)
```text
    function initialize(IPoolRegistry poolRegistry_) public initializer {
        if (address(poolRegistry_) == address(0)) revert PoolRegistryIsNull();
        __Pauseable_init();

        _poolRegistry = poolRegistry_;
        isSwapActive = true;
        maxLiquidable = 0.5e18; // 50%
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

**File:** contracts/FeeProvider.sol (L137-143)
```text
    function updateSwapFee(address synthIn_, address synthOut_, uint256 newSwapFee_) external onlyGovernor {
        if (newSwapFee_ > MAX_FEE_VALUE) revert FeeIsGreaterThanTheMax();
        uint256 _current = swapFees[synthIn_][synthOut_];
        if (newSwapFee_ == _current) revert NewValueIsSameAsCurrent();
        emit SwapFeeUpdated(synthIn_, synthOut_, _current, newSwapFee_);
        swapFees[synthIn_][synthOut_] = newSwapFee_;
    }
```
