### Title
Non-deterministic floating-point pricing math in TRX/TRC10 Exchange actuators can cause consensus divergence or exploitable value leakage - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The Aave V3 incident stemmed from a price/valuation miscalculation in a market-making/valuation formula that produced an incorrect asset value, triggering wrongful economic outcomes. The closest reachable analog in java-tron is the bonding-curve pricing math used by the built-in Exchange (TRX↔TRC10 AMM), which is computed with `double`-precision floating point (`Math.pow`) rather than deterministic fixed-point arithmetic, in a code path that is still reachable by any signed transaction.

### Finding Description
`ExchangeCapsule.transaction()` picks between a "legacy" processor and a "hardened" `BigDecimal`-based processor depending on the `useStrictMath` flag and a `hardenedCalc` flag supplied by the caller: [1](#0-0) 

The legacy `ExchangeProcessor` computes the relay/output quantity for a swap using `double` arithmetic and `Maths.pow`, which itself dispatches to either `StrictMathWrapper.pow` or the non-strict `MathWrapper.pow` (effectively `java.lang.Math.pow`) based on a runtime boolean: [2](#0-1) [3](#0-2) 

`java.lang.Math.pow` (unlike `StrictMath.pow`) is explicitly *not* guaranteed to produce bit-identical results across JVM implementations, JIT compilers, or CPU architectures (it may use platform intrinsics/FMA). Every consensus node in java-tron must independently execute `ExchangeInjectActuator`/`ExchangeTransactionActuator`/`ExchangeWithdrawActuator` when applying the same block, and these actuators feed the `dynamicStore.allowStrictMath()` value straight into this pricing function: [4](#0-3) [5](#0-4) 

A dedicated hardened path (`SafeExchangeProcessor`, using `BigDecimal` and `StrictMathWrapper`) was later introduced specifically to avoid this class of bug, but it is only used when `allowHardenExchangeCalculation()` is set, which is a chain-parameter gated by SR governance rather than always-on: [6](#0-5) [7](#0-6) 

Until/unless that parameter is active on a given chain, any unprivileged account can submit `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract` transactions that force every full node to evaluate `Math.pow` (non-strict) on attacker-chosen operands as part of block application.

### Impact Explanation
If any two full nodes in the network (different JVM vendor/version, different CPU with/without certain vector extensions, or different `-XX` JIT flags) compute even a 1-ULP-different result for the same `Math.pow` call, the derived `anotherTokenQuant`/`buyTokenQuant` will differ, causing the resulting account balances and `ExchangeCapsule` balances to diverge between nodes for the same block — i.e., a state-root/consensus mismatch (chain split), which is one of the explicitly accepted "Critical" impacts in scope. Separately, even absent divergence, floating point truncation in `(long) issuedSupply` /`(long) exchangeBalance` in `ExchangeProcessor` (`exchangeToSupply`/`exchangeFromSupply`) is a rounding behavior that does not preserve the exact bonding-curve invariant, which is the same root-cause category as the Aave CAPO bug (an approximate/rounded price feeding directly into value transfer), and can be leveraged by a trader to extract more value from the pool than the exact curve allows, echoing the "undervaluation triggering wrongful value transfer" pattern from the reported incident. This maps to the codebase's own historical concern, evidenced by the later addition of `SafeExchangeProcessor`/`StrictMathWrapper` specifically to "avoid...for cross-platform consistency" per the `@Deprecated` note on `Maths`: [8](#0-7) 

### Likelihood Explanation
Triggering the vulnerable path only requires submitting a normal, permissionless `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract` — no special privilege is needed, and Exchange pools are a long-standing public java-tron feature. The actual manifestation of cross-node divergence depends on whether the network's heterogeneous node fleet (different JDKs/CPUs) actually produces differing `Math.pow` outputs for some operand, which is plausible but not guaranteed on every input; likewise, the rounding-based value leakage is a smaller, continuous drift rather than an instant drain, both of which are consistent with why this bug class is rated Medium in the analogous report rather than Critical.

### Recommendation
Make the deterministic, `BigDecimal`/`StrictMath`-based `SafeExchangeProcessor` path (and `StrictMathWrapper` arithmetic in `AbstractExchangeActuator`) the unconditional default for all Exchange actuators rather than gating it behind a governance-activated dynamic parameter, and remove/replace the deprecated non-strict `MathWrapper`/`Maths.pow` usage in `ExchangeProcessor` entirely so no consensus-critical code path can execute non-strict floating point math.

### Proof of Concept
1. Run two full nodes on different JVM builds/CPU architectures (a scenario java-tron's own SR network realistically has).
2. Ensure `allowHardenExchangeCalculation` and `allowStrictMath` dynamic parameters are at their pre-activation/default state.
3. Broadcast an `ExchangeTransactionContract` (or `ExchangeInjectContract`) against an existing Exchange pool with operand values chosen so that the intermediate `Math.pow` computation in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` lands near a rounding boundary.
4. Compare the resulting `ExchangeCapsule` balances / account asset balances computed by each node for the same block; a divergence (or, on a single homogeneous test setup, a value inconsistent with the exact bonding-curve output computed via `SafeExchangeProcessor`) demonstrates the flaw.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-29)
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
```

**File:** common/src/main/java/org/tron/common/math/Maths.java (L1-9)
```java
package org.tron.common.math;

/**
 * This class is deprecated and should not be used in new code,
 * for cross-platform consistency, please use {@link StrictMathWrapper} instead,
 * especially for floating-point calculations.
 */
@Deprecated
public class Maths {
```

**File:** common/src/main/java/org/tron/common/math/Maths.java (L17-19)
```java
  public static double pow(double a, double b, boolean useStrictMath) {
    return useStrictMath ? StrictMathWrapper.pow(a, b) : MathWrapper.pow(a, b);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-219)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
```java
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            secondTokenBalance, tokenQuant), firstTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, tokenQuant),
            addExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            firstTokenBalance, tokenQuant), secondTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, anotherTokenQuant),
            addExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L1-44)
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
```
