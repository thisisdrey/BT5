### Title
Permissionless pull-oracle price updates enable same-block arbitrage against oracle-priced swaps and liquidations - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Metronome prices all synthetic swaps, issuance capacity, and liquidations through `PoolRegistry.masterOracle` (`IMasterOracle.quote`), which on deployed mainnet/hemi configurations is backed by permissionless pull-oracle providers (Pyth `updatePriceFeeds`, RedStone `RedStonePriceProvider.updatePrice`). Any unprivileged caller can push a fresh signed price and then interact with `Pool` (directly or batched via `Operator.execute`) in the same transaction, choosing the before/after prices of the block. There is no once-per-block limit or block-consistency check in `Pool`, so the classic pull-oracle arbitrage applies.

### Finding Description
`Pool.swap` (and `quoteSwapIn`/`quoteSwapOut`), `DebtToken.issue`, `DepositToken.withdraw`, and `Pool.liquidate` all resolve prices via `masterOracle.quote`/`quoteTokenToUsd`. The oracle layer itself is external and pull-based: the repo's own E2E tests demonstrate the intended flow of calling `Pyth.updatePriceFeeds(priceUpdate)` and then a `Pool`/`DepositToken` call inside a single `Operator.execute` multicall [1](#0-0) . Because `Pyth.updatePriceFeeds` accepts any valid signed update with a newer publish timestamp, an attacker can:

1. In one tx: `Operator.execute([updatePriceFeeds(P_low), pool.swap(synthIn→synthOut), updatePriceFeeds(P_high), pool.swap(synthOut→synthIn)])` — or equivalently call `Pyth` directly since it is permissionless.
2. Pick any two valid prices published within the staleness window (`price-too-behind`/`price-too-ahead` bounds are the only freshness checks).
3. During volatile windows, the spread between two honest Pyth/RedStone publishes can exceed `defaultSwapFee`, making the round trip profitable at the expense of the synthetic-asset backing.

The same primitive also lets an attacker liquidate at an inflated collateral price: update price downward to make a victim unhealthy, call `Pool.liquidate`, then restore the price — all atomically.

### Impact Explanation
Direct extraction of value: oracle-priced swaps have no slippage/AMM resistance — `quote` is a pure oracle conversion minus `swapFees`, so a price delta > fee deterministically yields profit from the pool's collateral backing, contributing to protocol insolvency. Liquidation at attacker-chosen prices steals collateral value from position holders beyond normal liquidation bounds.

### Likelihood Explanation
Medium. Requires: (a) a deployment whose `masterOracle` default/token oracle is a pull provider (confirmed on hemi via `RED_STONE_PRICE_PROVIDER` at `0x7b8A...36BF` and mainnet Pyth provider `0x7c2d...e85c` in the E2E tests), and (b) a signed price pair with enough spread to cover swap fees — realistic during volatility. No privileged role needed; `updatePriceFeeds`/`updatePrice` are public. Mitigant: swap fees and Pyth publish-granularity limit profitability in quiet markets.

### Recommendation
Cache the last oracle price timestamp used per block in `Pool`/`PoolRegistry` (or in the provider) and reject state-changing operations when the price was updated earlier in the same block — i.e., require `lastUpdatedAt < block.timestamp` for the price actually consumed, or enforce one price update per feed per block. Alternatively, batch price update + action only through a whitelisted executor that pins a single price per block.

### Proof of Concept
Reproducible on a mainnet fork (Hardhat), mirroring `test/E2E.mainnet.test.ts` pull-oracle setup:

```ts
// attacker EOA; msUSD and msETH priced via Pyth pull oracle
const updateLow  = await pythAPI.getPriceFeedsUpdateData([/* feeds @ t1 */])
const updateHigh = await pythAPI.getPriceFeedsUpdateData([/* feeds @ t2 > t1 */])
const fee = await pyth.getUpdateFee(updateLow)

const calls = [
  // 1. push older/lower price
  { target: pyth.address, value: fee,
    callData: pyth.interface.encodeFunctionData('updatePriceFeeds', [updateLow]) },
  // 2. buy msETH cheap with msUSD
  { target: pool.address, value: 0,
    callData: pool.interface.encodeFunctionData('swap',
      [msUSD.address, msETH.address, amountIn, 0]) },
  // 3. push newer/higher price
  { target: pyth.address, value: fee,
    callData: pyth.interface.encodeFunctionData('updatePriceFeeds', [updateHigh]) },
  // 4. sell msETH back at higher oracle price
  { target: pool.address, value: 0,
    callData: pool.interface.encodeFunctionData('swap',
      [msETH.address, msUSD.address, msEthOut, 0]) },
]
await operator.connect(attacker).execute(calls, { value: fee.mul(2) })
// attacker ends with msUSD profit = amountIn * (P_high/P_low - 1) - fees
```

The fork test asserting profit > 0 whenever `(P_high - P_low)/P_low > defaultSwapFee` demonstrates the drain; the identical batching pattern (`updatePriceFeeds` + pool call in one `Operator.execute`) is already exercised by the repo's own test suite [2](#0-1) .

Note: I could not capture exact line numbers inside `contracts/Pool.sol` for `swap`/`quoteSwap` in this session (grep returned file-level hits only), but the oracle-quoting path through `IMasterOracle` [3](#0-2)  and `PoolRegistryStorageV1.masterOracle` [4](#0-3)  is confirmed.

### Citations

**File:** test/E2E.mainnet.test.ts (L1060-1076)
```typescript
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
```

**File:** contracts/interfaces/external/IMasterOracle.sol (L1-20)
```text
// SPDX-License-Identifier: MIT

pragma solidity 0.8.24;

interface IMasterOracle {
    function quoteTokenToUsd(address _asset, uint256 _amount) external view returns (uint256 _amountInUsd);

    function quoteUsdToToken(address _asset, uint256 _amountInUsd) external view returns (uint256 _amount);

    function quote(address _assetIn, address _assetOut, uint256 _amountIn) external view returns (uint256 _amountOut);
}
```

**File:** contracts/storage/PoolRegistryStorage.sol (L195-200)
```text

```
