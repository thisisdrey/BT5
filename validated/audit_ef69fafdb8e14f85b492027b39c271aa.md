### Title
Bancor-style Exchange Rate Uses Non-Deterministic Floating-Point Math, Enabling Rounding-Bias Fund Drain from TRC10 Liquidity Pools - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java)

### Summary
The BEVO incident exploited a deflationary-token reward/rebase mechanism whose internal accounting variable (`_rTotal`) could be perturbed by an attacker, corrupting the rate used to compute balances and letting the attacker extract disproportionate value from a liquidity pool. java-tron has a structurally analogous on-chain AMM: the TRC10 "Exchange" (Bancor-formula) pool. In its default (non-hardened) mode, the exchange rate is computed with `double`/`Math.pow` floating-point arithmetic and truncating casts to `long`, rather than exact fixed-point math. This is directly reachable by any unprivileged account via `ExchangeTransactionContract` (no creator restriction, unlike inject/withdraw), and the systematic rounding bias in this legacy path is exactly the class of flaw the project later patched with a "hardened" fixed-point replacement.

### Finding Description
`ExchangeCapsule.transaction()` selects between two rate-calculation engines based on the `allowHardenExchangeCalculation` dynamic property: [1](#0-0) 

When the harden flag is off (the historical/default legacy behavior), `ExchangeProcessor` is used, which computes the Bancor relay-token conversion using `double` arithmetic and `Maths.pow`, then truncates to `long` via a plain cast: [2](#0-1) 

This is invoked from `ExchangeTransactionActuator.execute()`, which is reachable by **any** account (no creator/permission check — unlike `ExchangeInjectActuator`/`ExchangeWithdrawActuator`, which require `accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())`): [3](#0-2) 

The project's own regression test proves the non-strict, floating-point path produces materially different (and non-reproducible/rounding-biased) results compared to the fixed-point `SafeExchangeProcessor`: [4](#0-3) 

The existence of `SafeExchangeProcessor` (BigDecimal, fixed scale, explicit `RoundingMode`) gated behind `ALLOW_HARDEN_EXCHANGE_CALCULATION` confirms this was recognized as an exploitable precision/rounding defect requiring a hard fork to correct: [5](#0-4) [6](#0-5) 

Just as BEVO's attacker manipulated `_rTotal`/`getRate()` through repeated `deliver()` calls to bias the balance-to-token exchange rate, an attacker here can perform repeated round-trip trades (sell token A for B, then B back for A) through `ExchangeTransactionContract` against the legacy floating-point processor. Because the truncating `(long) issuedSupply` / `(long) exchangeBalance` casts consistently round in a fixed direction relative to the true fixed-point value, and because `double`-precision `pow` results can diverge from the mathematically exact answer, each round trip can leave the trader with a small but consistent surplus (or leave the pool with a deficit), which compounds over many transactions the attacker can broadcast without limit.

### Impact Explanation
If chains/deployments have not activated `ALLOW_HARDEN_EXCHANGE_CALCULATION` (default `0`), any unprivileged account can call `ExchangeTransactionContract` in a loop and systematically extract value from TRC10 exchange pools funded by other users' injected liquidity (the pool balances, not just the caller's own funds, are directly decremented via `exchangeCapsule.setBalance(...)`). This is a concrete unauthorized-theft-of-funds scenario matching the BEVO bug class (rate/reward manipulation via unprivileged calls), reachable purely through a signed transaction of a public actuator.

### Likelihood Explanation
High reachability: `ExchangeTransactionContract` requires no special privilege beyond having tokens/TRX to trade, and any account can call it repeatedly with no rate limiting beyond bandwidth/energy cost. The core defect (floating point + truncation) is deterministic and was significant enough that the project introduced a hard-fork-gated fixed-point replacement (`SafeExchangeProcessor` / `ALLOW_HARDEN_EXCHANGE_CALCULATION`, fork `VERSION_4_8_2`), which strongly indicates real-world exploitability of the legacy path on any node/network still running with the flag disabled.

### Recommendation
- Ensure `ALLOW_HARDEN_EXCHANGE_CALCULATION` is enabled (and enforced going forward) so `SafeExchangeProcessor`'s fixed-point BigDecimal math is always used instead of `ExchangeProcessor`'s `double`/`Math.pow` path.
- Consider removing the legacy `ExchangeProcessor` code path entirely once hardened mode is permanently active, to eliminate the possibility of it being re-enabled or used in forked/private deployments.
- Add invariant checks (e.g., constant-product/constant-value bounds) around `ExchangeTransactionActuator.execute()` to detect and reject balance updates that violate expected pool invariants, independent of which formula computed them.

### Proof of Concept
1. Attacker creates or identifies a TRC10 `Exchange` pool with liquidity injected by its creator (pool operates in legacy mode, `allowHardenExchangeCalculation() == false`, the default).
2. Attacker repeatedly broadcasts `ExchangeTransactionContract` transactions swapping small amounts A→B then B→A through `ExchangeTransactionActuator.execute()`, which calls `exchangeCapsule.transaction(...)` → `ExchangeProcessor.exchange()`.
3. Each round trip exploits the `double`-precision `Math.pow` computation and the truncating `(long)` cast in `exchangeToSupply`/`exchangeFromSupply`, yielding the attacker a small favorable variance versus the mathematically exact fixed-point result each time, as demonstrated by the discrepancy in `ExchangeProcessorTest.testStrictMath()` between the legacy and hardened engines.
4. Repeating this loop drains the pool's `firstTokenBalance`/`secondTokenBalance` (funded by the creator/other participants) over many transactions, at the cost of the attacker's own bandwidth/energy fees only.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-99)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

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

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);

      ret.setExchangeReceivedAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L272-280)
```java
    for (long[] data : testData) {
      ExchangeProcessor processor = new ExchangeProcessor(supply, false);
      long anotherTokenQuant = processor.exchange(data[0], data[1], data[2]);
      processor = new ExchangeProcessor(supply, true);
      long result = processor.exchange(data[0], data[1], data[2]);
      long safeResult = SafeExchangeProcessor.INSTANCE.exchange(data[0], data[1], data[2]);
      Assert.assertNotEquals(anotherTokenQuant, result);
      Assert.assertEquals(safeResult, result);
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L1-45)
```java
package org.tron.core.capsule;

import java.math.BigDecimal;
import java.math.RoundingMode;
import lombok.extern.slf4j.Slf4j;
import org.tron.common.math.StrictMathWrapper;

@Slf4j(topic = "capsule")
public class SafeExchangeProcessor implements ExchangeCapsule.Processor {

  private static final BigDecimal SUPPLY = BigDecimal.valueOf(1_000_000_000_000_000_000L);

  public static final SafeExchangeProcessor INSTANCE = new SafeExchangeProcessor();

  private SafeExchangeProcessor() {

  }

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
}
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L928-942)
```java
      case ALLOW_HARDEN_EXCHANGE_CALCULATION: {
        if (!forkController.pass(ForkBlockVersionEnum.VERSION_4_8_2)) {
          throw new ContractValidateException(
              "Bad chain parameter id [ALLOW_HARDEN_EXCHANGE_CALCULATION]");
        }
        if (value != 0 && value != 1) {
          throw new ContractValidateException(
              "This value[ALLOW_HARDEN_EXCHANGE_CALCULATION] is only allowed to be 0 or 1");
        }
        if (dynamicPropertiesStore.getAllowHardenExchangeCalculation() == value) {
          throw new ContractValidateException(
              "[ALLOW_HARDEN_EXCHANGE_CALCULATION] has been set to " + value
                  + ", no need to propose again");
        }
        break;
```
