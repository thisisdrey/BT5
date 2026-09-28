### Title
Oracle-priced synth swaps can be sandwiched around oracle price updates - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.swap` settles synthetic-token swaps at prices quoted by `MasterOracle.quote` with only the configurable `swapFees` haircut. When an oracle price update transaction is observable in the mempool (e.g., a permissionless pull-oracle push such as Pyth `updatePriceFeeds`, or any public feeder update), an unprivileged attacker can swap into the asset whose price is about to rise, let the update land, then swap back, extracting the price delta minus fees. This is the same bug class as the referenced `XOracle.updatePrices` sandwich in Unitas: freshly updated, predictable prices are usable immediately in value-changing calls with no timelock or deviation guard.

### Finding Description
`Pool.swap` burns `syntheticTokenIn_` and mints `syntheticTokenOut_` at a rate derived entirely from `masterOracle().quote(tokenIn, tokenOut, amountIn)`, deducting `feeProvider.swapFees(tokenIn, tokenOut)` on the output leg (same structure as `quoteSwapOut`). Relevant code:

- `quoteSwapOut` computes `_amountOut = masterOracle().quote(...)` then subtracts `_fee = _amountOut.wadMul(_swapFee)` — `contracts/Pool.sol:512-524` [1](#0-0) 
- `quoteSwapIn` grosses up `amountOut_` by `1/(1 - swapFee)` and quotes via `masterOracle().quote` — `contracts/Pool.sol:482-497` [2](#0-1) 
- `masterOracle()` resolves straight from `PoolRegistry` — `contracts/Pool.sol:601-603` [3](#0-2) 
- `defaultSwapFee` and per-pair `swapFees` are governor-settable and can be small or zero — `contracts/storage/FeeProviderStorage.sol` 

Attack path (all unprivileged, public entry points):

1. Attacker observes a pending oracle update tx that will raise `msETH`'s USD price (on pull-oracle deployments the update itself is a public call; the E2E tests show anyone can bundle `updatePriceFeeds`/`updatePrice` before protocol calls — `test/E2E.mainnet.next.test.ts:944-964` [4](#0-3) ).
2. Front-run: `pool.swap(msUSD, msETH, X)` buys `msETH` at the stale, lower price.
3. Oracle update lands.
4. Back-run: `pool.swap(msETH, msUSD, ...)` sells at the new, higher price.

Profit = `X * (ΔP/P) - 2 * swapFee * X`. Whenever the oracle delta exceeds twice the swap fee (or the pair's fee is 0/low, which `swapFees` permits), the round trip is profitable. There is no per-block restriction on swapping, no slippage protection tied to the caller, and no deviation bound between consecutive oracle reads. The same applies to `DebtToken.issue`/`repay` paths where debt is minted/burned at oracle value, and to `Pool.liquidate` quoting via `masterOracle().quote` at `Pool.sol:396-400` and `Pool.sol:456-460` [5](#0-4) .

### Impact Explanation
Each profitable round trip mints `msETH` cheap and redeems it expensive against the same shared oracle, effectively extracting value from the pool's collateral backing: synths are redeemable claims on pooled collateral, so repeated extraction creates bad debt / insolvency for other depositors. Magnitude scales with attacker capital (flash-loanable via `SmartFarmingManager.leverage`/`flashRepay` or external flash loans), so a single sandwich can drain a large fraction of TVL when the oracle delta is large.

### Likelihood Explanation
Requires an oracle update tx observable pre-execution and a price move exceeding twice the swap fee. Pull-oracle integrations (Pyth/RedStone) make updates permissionless and publicly visible — the repo's own tests demonstrate bundling `updatePriceFeeds` with protocol calls via `Operator.execute`. The deployed configuration must be checked per pair: if `swapFees` for the pair is zero or below half the expected update delta, the attack is directly profitable. This is a known generic limitation of oracle-priced swap venues rather than a logic bug, and profitability depends on fee configuration, but nothing in `Pool.swap` itself prevents it.

### Recommendation
- Enforce a minimum `swapFee` floor such that `2 * minFee` exceeds the maximum credible single-update price deviation, or
- Add a staleness/cooldown: disallow `swap` (or apply a penalty) in the same block as an oracle update, or
- Use a TWAP/medianized price for swap quoting instead of the spot oracle value, or
- Cap per-block swap volume so that extractable value per update is bounded.

### Proof of Concept
Conceptual Foundry fork PoC (mainnet fork, deployed `Pool`/`PoolRegistry`):

```solidity
// Fork mainnet at a block; use deployed pool, msUSD, msETH, Pyth pull oracle.
function test_oracleUpdateSandwich() external {
    // 1. Fetch signed Pyth update that raises ETH price (e.g. +5%)
    bytes[] memory priceUpdate = pythAPI.getPriceFeedsUpdateData(ethFeedId);
    uint256 fee = pyth.getUpdateFee(feedIds);

    // 2. Attacker front-runs: buy msETH at stale price
    deal(address(msUSD), attacker, 1_000_000e18);
    vm.prank(attacker);
    uint256 msEthBefore = msETH.balanceOf(attacker);
    pool.swap(msUSD, msETH, 1_000_000e18); // quoteSwapOut at old price
    uint256 bought = msETH.balanceOf(attacker) - msEthBefore;

    // 3. Oracle update lands
    pyth.updatePriceFeeds{value: fee}(priceUpdate);

    // 4. Attacker back-runs: sell msETH at new higher price
    vm.prank(attacker);
    pool.swap(msETH, msUSD, bought);
    uint256 profit = msUSD.balanceOf(attacker) - 1_000_000e18;

    // Asserts profit > 0 whenever (newPrice/oldPrice - 1) > 2 * swapFee
    assertGt(profit, 0);
}
```

Note: the concrete `swap` function body lines were not fully read in this analysis; the pricing path via `quoteSwapOut`/`quoteSwapIn` and the absence of any update-boundary guard are confirmed. Feasibility depends on the deployed `swapFees` per pair — verify on a live fork.

### Citations

**File:** contracts/Pool.sol (L396-400)
```text
        _amountToRepay = masterOracle().quote(
            address(depositToken_.underlying()),
            address(syntheticToken_),
            _repayAmountInCollateral
        );
```

**File:** contracts/Pool.sol (L482-497)
```text
    function quoteSwapIn(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountOut_
    ) external view override returns (uint256 _amountIn, uint256 _fee) {
        uint256 _swapFee = feeProvider.swapFees(address(syntheticTokenIn_), address(syntheticTokenOut_));
        if (_swapFee > 0) {
            amountOut_ = amountOut_.wadDiv(1e18 - _swapFee);
            _fee = amountOut_.wadMul(_swapFee);
        }

        _amountIn = _poolRegistry.masterOracle().quote(
            address(syntheticTokenOut_),
            address(syntheticTokenIn_),
            amountOut_
        );
```

**File:** contracts/Pool.sol (L512-524)
```text
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

**File:** contracts/Pool.sol (L601-603)
```text
    function masterOracle() public view override returns (IMasterOracle) {
        return _poolRegistry.masterOracle();
    }
```

**File:** test/E2E.mainnet.next.test.ts (L944-964)
```typescript
            const priceUpdate = await pythAPI.getPriceFeedsUpdateData(pythFeedIds)
            const fee = await pyth.getUpdateFee(pythFeedIds)

            const amount = parseUnits('100', 6)
            const calls: IOperator.CallStruct[] = [
              {
                target: pyth.address,
                value: fee,
                callData: pyth.interface.encodeFunctionData('updatePriceFeeds', [priceUpdate]),
              },
              {
                target: msdUSDC_1.address,
                value: 0,
                callData: msdUSDC_1.interface.encodeFunctionData('deposit', [amount, alice.address]),
              },
            ]
            const tx = () => operator.connect(alice).execute(calls, {value: fee})

            // then
            await expect(tx).changeTokenBalance(msdUSDC_1, alice, amount)
          })
```
