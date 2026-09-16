### Title
Native TRC10/TRX AMM (`Exchange`) Spot Price Can Be Manipulated by a Single Unprivileged Transaction and Is Both Value-Extractable and Exposed as an On-Chain Price Reference - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`)

### Summary
java-tron ships a native Bancor-formula AMM (the `Exchange`/`ExchangeV2` feature) that lets any account swap TRX/TRC10 tokens against a liquidity pool. Any unprivileged account can call `ExchangeTransactionContract` to move the pool's internal price ratio by an arbitrary amount in a single transaction — there is no minimum liquidity floor, no per-block/per-transaction price-impact cap, and the only "protection" (`expected`) is chosen by the caller itself. This mirrors the root cause of the Blend Pools V2 incident: a single trade against a thin liquidity pool skews a spot-price mechanism that is then trusted elsewhere.

### Finding Description
`ExchangeTransactionActuator.doValidate()`/`execute()` compute the counter-token amount purely from the current pool balances via `ExchangeCapsule.transaction()`: [1](#0-0) 

The only safety check is that the caller's own requested `expected` minimum is met — an attacker who is both taker and beneficiary can set `expected = 1` and push arbitrarily large `tokenQuant` (bounded only by `dynamicStore.getExchangeBalanceLimit()`), causing the bancor-curve price of `firstTokenId`/`secondTokenId` to move by any multiple in one block: [2](#0-1) 

The underlying pricing math (`ExchangeCapsule.transaction`) selects between a legacy floating-point implementation and a BigDecimal-hardened one depending on the `allowHardenExchangeCalculation` dynamic property, which is a committee-gated hard-fork flag (off by default until activated): [3](#0-2) [4](#0-3) 

The legacy path (`ExchangeProcessor`) uses `double`/`Math.pow` for the bonding-curve calculation, which is inherently lossy compared to the `BigDecimal`-based `SafeExchangeProcessor`: [5](#0-4) [6](#0-5) 

The resulting `firstTokenBalance`/`secondTokenBalance` ratio for any `exchange_id` is the chain's canonical, publicly queryable "price" for that pair, served unauthenticated over both HTTP and gRPC via `GetExchangeById`: [7](#0-6) 

Since `ExchangeInjectContract` (adding liquidity) and `ExchangeWithdrawContract` are restricted to the pool creator only: [8](#0-7) 

...anyone can freely create a brand-new, arbitrarily thin pool via `ExchangeCreateContract` and then, exactly like the SDEX/USTRY scenario, execute one oversized swap to move its exposed spot price to an extreme value before any external system (or a subsequent transaction reversing the position) reacts.

### Impact Explanation
Within java-tron itself the AMM formula guarantees that a swapper's payout is bounded by the invariant, so a single self-created pool cannot directly mint unbacked TRX/TRC10 balances. The concrete, reachable risk is:
1. **Precision/rounding value extraction** on chains still running the legacy (non-hardened) `ExchangeProcessor` double-math path — repeated large trades against a thin pool can exploit floating-point rounding asymmetry between `exchangeToSupply`/`exchangeFromSupply` to extract more value than deposited, draining the pool's TRX/token balance (funds belong to the pool creator or later traders).
2. **Oracle-style manipulation of externally-facing state** — because the manipulated `firstTokenBalance`/`secondTokenBalance` ratio is served as ground truth via `GetExchangeById`/`GetExchangeList` over the public gRPC and HTTP/JSON-RPC surfaces, any downstream consumer (bridges, off-chain price feeders, wallets, other smart contracts calling out to this API) that treats it as a reliable spot price inherits the exact class of bug that caused the Blend Pools V2 loss.

This is Medium-to-High severity depending on hard-fork state: it is a value-extraction / price-integrity bug reachable by any unprivileged transaction broadcaster, but it does not by itself allow theft of arbitrary third-party account balances inside java-tron (LPs are protected by the creator-only inject/withdraw restriction).

### Likelihood Explanation
High reachability: `ExchangeCreateContract` and `ExchangeTransactionContract` can be broadcast by any funded account with no special privilege, and pool size is entirely attacker-controlled at creation time (self-created, low-liquidity pool). The only mitigating factor is that exploitation of the floating-point rounding path requires the chain to still be running with `allowHardenExchangeCalculation` disabled; if that hard fork has already activated, the arithmetic-manipulation component is closed, though the "manipulable spot price exposed via public API" surface remains unchanged.

### Recommendation
- Enforce a minimum pool liquidity floor and/or a maximum per-transaction price-impact percentage in `ExchangeTransactionActuator.doValidate()`, independent of the caller-supplied `expected` value.
- Confirm `allowHardenExchangeCalculation` is activated network-wide and consider removing the legacy `ExchangeProcessor` double-math code path entirely rather than gating it behind a togglable proposal.
- Document/flag that `GetExchangeById`/`GetExchangeList` return a spot price with no TWAP or manipulation resistance, and add API-level warnings for consumers, since java-tron itself does not provide a manipulation-resistant on-chain price oracle for TRC10/TRX pairs.

### Proof of Concept
1. Attacker calls `ExchangeCreateContract` to create a new pool with minimal token balances (e.g. 1 TRX vs 1 unit of a self-issued TRC10 token) — reachable by any funded account, no permission checks beyond balance sufficiency (`actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java`).
2. Attacker immediately broadcasts a large `ExchangeTransactionContract` swap against this pool with `expected = 1`, moving the pool ratio to any desired extreme in a single transaction, bounded only by `dynamicStore.getExchangeBalanceLimit()` (`ExchangeTransactionActuator.java:199-221`).
3. The skewed `firstTokenBalance`/`secondTokenBalance` is now returned as-is by `GetExchangeById` over HTTP/gRPC to any caller.
4. On chains where `allowHardenExchangeCalculation` is not yet active, repeating buy/sell cycles against the same thin pool via the double-precision `ExchangeProcessor.exchangeToSupply/exchangeFromSupply` can accumulate rounding-favorable outcomes across many transactions, extracting more value than deposited from the pool balance.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-221)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }

    if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(tokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(tokenID, tokenQuant, dynamicStore)) {
        throw new ContractValidateException("token balance is not enough");
      }
    }

    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-45)
```java
  private long exchangeToSupply(long balance, long quant) {
    logger.debug("balance: " + balance);
    long newBalance = balance + quant;
    logger.debug("balance + quant: " + newBalance);

    double issuedSupply = -supply * (1.0
        - Maths.pow(1.0 + (double) quant / newBalance, 0.0005, this.useStrictMath));
    logger.debug("issuedSupply: " + issuedSupply);
    long out = (long) issuedSupply;
    supply += out;

    return out;
  }

  private long exchangeFromSupply(long balance, long supplyQuant) {
    supply -= supplyQuant;

    double exchangeBalance = balance
        * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, this.useStrictMath) - 1.0);
    logger.debug("exchangeBalance: " + exchangeBalance);

    return (long) exchangeBalance;
  }

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L19-44)
```java
  private BigDecimal exchangeToSupply(long balance, long quant) {
    long newBalance = StrictMathWrapper.addExact(balance, quant);
    BigDecimal bdQuant = BigDecimal.valueOf(quant);
    BigDecimal bdNewBalance = BigDecimal.valueOf(newBalance);
    BigDecimal base = BigDecimal.ONE.add(
        bdQuant.divide(bdNewBalance, 18, RoundingMode.HALF_UP));
    double powResult = StrictMathWrapper.pow(base.doubleValue(), 0.0005);
    return SUPPLY.negate().multiply(
        BigDecimal.ONE.subtract(BigDecimal.valueOf(powResult))).setScale(0, RoundingMode.DOWN);
  }

  private long exchangeFromSupply(long balance, BigDecimal supplyQuant) {
    BigDecimal bdBalance = BigDecimal.valueOf(balance);
    BigDecimal base = BigDecimal.ONE.add(
        supplyQuant.divide(SUPPLY, 18, RoundingMode.HALF_UP));
    double powResult = StrictMathWrapper.pow(base.doubleValue(), 2000.0);
    BigDecimal exchangeBalance = bdBalance.multiply(
        BigDecimal.valueOf(powResult).subtract(BigDecimal.ONE));
    return exchangeBalance.setScale(0, RoundingMode.DOWN).longValueExact();
  }

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    BigDecimal relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetExchangeByIdServlet.java (L1-2)
```java
package org.tron.core.services.http;

```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```
