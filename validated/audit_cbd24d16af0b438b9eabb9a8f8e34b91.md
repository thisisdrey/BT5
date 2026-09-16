### Title
Precision-inconsistent Bancor formula in legacy `ExchangeProcessor` allows post-trade balance invariant to be bypassed - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The whaleswap incident was caused by a Uniswap-style constant-product (`K`) invariant check being computed with mismatched decimal magnitude, so the post-trade balance check silently passed for a trade that should have failed, letting the attacker drain reserves. `java-tron`'s built-in DEX-like feature (the `Exchange`/`ExchangeV2` bancor-formula market) has an analogous structural weakness: the trade output is computed by `ExchangeProcessor` using raw IEEE-754 `double` math (`Math.pow`), and the resulting new pool balances are **never re-validated against any invariant** unless the trade happens to go through the "hardened" code path.

### Finding Description
`ExchangeCapsule.transaction()` [1](#0-0)  selects between two mutually-inconsistent implementations of the bancor exchange formula based on the `hardenedCalc` flag:
- Legacy path: `ExchangeProcessor`, which computes `exchangeToSupply`/`exchangeFromSupply` purely with `double` arithmetic and `Math.pow` [2](#0-1) .
- Hardened path: `SafeExchangeProcessor`, which uses `BigDecimal` with 18-digit scale for the same formula [3](#0-2) .

Critically, the post-trade sanity check `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` that guards against a corrupted/negative pool state is **only performed when `hardenedCalc` is true**:
```
if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
  throw new ContractValidateException("Exchange balance must be >=0 after transaction");
}
``` [4](#0-3) 

In the legacy (non-hardened) path, `buyTokenQuant` is derived from double-precision `Math.pow` calls over pool balances that can be as large as `dynamicStore.getExchangeBalanceLimit()` (validated up to that limit in `ExchangeTransactionActuator.doValidate()` at [5](#0-4) ). This mirrors the whaleswap root cause: the invariant/magnitude of the calculation is not verified against the true integer math result before mutating the pool state and crediting the caller with `anotherTokenQuant` in `ExchangeTransactionActuator.execute()` [6](#0-5) . Because the exponents used (`0.0005` and `2000.0`) are the inverse of each other applied to very large numbers, and `double` only carries ~15-17 significant decimal digits, precision loss at these scales is expected and unguarded in the legacy path — there is no re-derivation/re-check of the resulting balances against the pre-trade product invariant, unlike the hardened path.

Whether the legacy (non-hardened) path is still reachable in production depends on the `AllowHardenExchangeCalculation` dynamic parameter, controlled via `AbstractExchangeActuator.allowHarden()` and `DynamicPropertiesStore` [7](#0-6) ; I could not fully confirm from the index whether this flag currently defaults to hardened-only on mainnet — this would need to be checked against `DynamicPropertiesStore`'s default/committee-controlled value directly in a live checkout.

### Impact Explanation
If the legacy processor is still reachable, an attacker who is an unprivileged transaction sender can call `ExchangeTransactionContract` (`ExchangeTransactionActuator`) with carefully chosen `quant`/`expected` values against a pool with large balances, exploiting `double` precision loss in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` to receive an `anotherTokenQuant` inconsistent with the true bancor formula while the pool balances are updated without any invariant check. This can result in draining of TRX/TRC10 reserves held in the `Exchange`/`ExchangeV2` pools — a direct theft-of-funds impact analogous to the whaleswap loss.

### Likelihood Explanation
Exploitability depends entirely on whether the non-hardened path (`hardenedCalc == false`) is still reachable on the target network via the `AllowHardenExchangeCalculation` proposal parameter. If hardening is permanently enabled network-wide, this reduces to a latent/dead-code weakness rather than a live vulnerability. I was not able to conclusively determine the current default/enforced state of this flag from the indexed code alone.

### Recommendation
- Verify and, if necessary, enforce that `AllowHardenExchangeCalculation` is permanently set to `1` on mainnet (i.e., retire/deny the legacy `ExchangeProcessor` code path entirely rather than gating it behind a toggle).
- Move the `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` invariant check outside the `hardenedCalc` conditional so it always applies, regardless of which processor computed the trade.
- Consider replacing `ExchangeProcessor`'s `double`/`Math.pow`-based bancor formula entirely with the `BigDecimal`-based `SafeExchangeProcessor` implementation, removing the split code path that created the whaleswap-style magnitude/precision inconsistency in the first place.

### Proof of Concept
Not fully constructible from the indexed code alone: exploiting this requires (1) confirming `AllowHardenExchangeCalculation` is disabled or can be disabled for a target `Exchange` pool, and (2) computing specific large pool balances / `quant` values where `Math.pow(1 + quant/newBalance, 0.0005)` in `double` diverges materially from the `BigDecimal` equivalent, then submitting an `ExchangeTransactionContract` transaction with those parameters to receive more of the counter-token than the true invariant allows. A concrete PoC transaction would need to be built and tested against a running node/testnet to confirm the magnitude of the exploitable precision gap, which is outside what can be verified through static code review here.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-168)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

    long buyTokenQuant = 0;
    long firstTokenBalance = this.exchange.getFirstTokenBalance();
    long secondTokenBalance = this.exchange.getSecondTokenBalance();
    long newFirstTokenBalance;
    long newSecondTokenBalance;

    if (this.exchange.getFirstTokenId().equals(ByteString.copyFrom(sellTokenID))) {
      buyTokenQuant = processor.exchange(firstTokenBalance,
          secondTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(firstTokenBalance, sellTokenQuant)
          : firstTokenBalance + sellTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(secondTokenBalance, buyTokenQuant)
          : secondTokenBalance - buyTokenQuant;

    } else {
      buyTokenQuant = processor.exchange(secondTokenBalance,
          firstTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(firstTokenBalance, buyTokenQuant)
          : firstTokenBalance - buyTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(secondTokenBalance, sellTokenQuant)
          : secondTokenBalance + sellTokenQuant;

    }

    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

    return buyTokenQuant;
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L67-91)
```java
      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-205)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L1-40)
```java
package org.tron.core.actuator;

import com.google.protobuf.GeneratedMessageV3;
import org.tron.common.math.StrictMathWrapper;
import org.tron.protos.Protocol.Transaction.Contract.ContractType;

public abstract class AbstractExchangeActuator extends AbstractActuator {

  public AbstractExchangeActuator(ContractType type, Class<? extends GeneratedMessageV3> clazz) {
    super(type, clazz);
  }

  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }

  public long subtractExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.subtractExact(x, y) : x - y;
  }

  public long addExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.addExact(x, y) : x + y;
  }
}


```
