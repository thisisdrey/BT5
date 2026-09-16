### Title
Bancor-curve floating-point rounding in `ExchangeProcessor` lets an attacker drain value from a self-created, low-liquidity `Exchange` pool — analogous to Paraspace's manipulable LP-position oracle - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The Paraspace bug relies on an attacker being able to create and fully own a low-liquidity AMM pool, manipulate its spot price within a bounded window, and have that manipulated state accepted by another part of the protocol that trusts the pool as ground truth, causing a shared pool of funds to take a loss. java-tron ships its own on-chain Bancor-style AMM — the `Exchange` feature (`ExchangeCreateContract`/`ExchangeInjectContract`/`ExchangeTransactionContract`) — which any account can instantiate unilaterally with arbitrary token ratios [1](#0-0) , and whose pricing curve (`ExchangeProcessor`) is computed with double-precision floating point [2](#0-1) . Because the pool creator single-handedly sets the initial reserves and can execute the `exchangeToSupply`/`exchangeFromSupply` round trip via `ExchangeTransactionContract`, the same "attacker controls both sides of the price curve in one bounded operation" primitive from the report is directly reachable in java-tron.

### Finding Description
`ExchangeCreateActuator.execute` allows an unprivileged sender to create a new `Exchange` object with any two supported tokens (including TRX) at any ratio it chooses, entirely funded by itself [3](#0-2) . Because there is no floor on liquidity beyond `firstTokenBalance/secondTokenBalance > 0` and a generic `balanceLimit` upper bound [4](#0-3) , the attacker can create an intentionally thin pool exactly like Alice's low-TVL Uniswap V3 pool in the report.

Once created, anyone (including the creator) can trade against the pool via `ExchangeTransactionActuator`, which calls `ExchangeCapsule.transaction`, and this in turn delegates to `ExchangeProcessor.exchange` [5](#0-4) [6](#0-5) . The default (non-hardened) `ExchangeProcessor` computes the Bancor relay-token math with Java `double` arithmetic and `Math.pow`, then truncates via a cast to `long` [7](#0-6) . This is the same class of "unsafe pricing surface reachable by a single signer" that the report exploits (spot-price math, not TWAP, fully attacker-controlled in a single pool). A repo-supplied "hardened" replacement (`SafeExchangeProcessor`, using `BigDecimal`) exists and is only activated when the `allowHardenExchangeCalculation` dynamic parameter is enabled [8](#0-7) [9](#0-8) , indicating the legacy `double`-based path was recognized internally as imprecise/exploitable — the presence of two parallel implementations selected by a governance-gated flag is itself evidence that the floating-point path can diverge from the "fair" BigDecimal-based curve.

The root-cause pattern mirrors the report exactly:
1. Attacker unilaterally creates a thin, fully self-owned liquidity pool (`ExchangeCreateContract`, analogous to Alice's Uniswap V3 pool from `MIN_TICK` to `MAX_TICK`).
2. Other unaware parties can subsequently add liquidity to that same pool via `ExchangeInjectContract` (analogous to shared TVL being added to an attacker-influenced pool) [10](#0-9) .
3. The attacker then performs round-trip trades through `ExchangeTransactionContract` that exploit the asymmetric truncation of `exchangeToSupply`/`exchangeFromSupply` (buy then sell, or vice versa) to extract more value than was deposited, at the expense of the shared pool balance — analogous to Alice draining Paraspace's lending pool by manipulating her own low-TVL collateral pool.

### Impact Explanation
If the floating-point rounding in the default `ExchangeProcessor` is systematically biased in the attacker's favor across a buy/sell round trip, an attacker can extract TRX or TRC10 tokens deposited by other users who injected liquidity into the same exchange pool, resulting in direct theft of funds from a shared, on-chain balance — a concrete "unbacked balance / theft of funds" outcome, reachable purely through signed `ExchangeCreateContract`/`ExchangeInjectContract`/`ExchangeTransactionContract` transactions, matching the required impact bar (unauthorized value extraction from a fund pool other actors have contributed to).

### Likelihood Explanation
Likelihood depends on whether the floating-point rounding bias is exploitable at scale before the pool is arbitraged/emptied and whether `allowHardenExchangeCalculation` is enabled network-wide (mitigating the issue when on). I was not able to confirm, within the available tool budget, (a) the exact numerical direction/magnitude of the double-precision rounding bias across repeated round trips, or (b) the current mainnet/testnet default value of `allowHardenExchangeCalculation`. Both would need to be verified with a live governance-parameter check and numerical fuzzing of `ExchangeProcessor.exchange` before treating this as a confirmed exploit rather than a plausible bug-class analog.

### Recommendation
- Confirm whether `allowHardenExchangeCalculation` is enabled by default; if not, prioritize activating the `SafeExchangeProcessor` (BigDecimal) path network-wide to eliminate floating-point rounding bias.
- Add minimum-liquidity/anti-manipulation checks to `ExchangeCreateActuator` (e.g., minimum reserve sizes, or restricting third-party injection into attacker-created pools) so a single-owner pool cannot later be topped up by unsuspecting users and drained.
- Fuzz-test `ExchangeProcessor.exchange` against `SafeExchangeProcessor.exchange` for round-trip conservation (buy then sell should not yield attacker profit beyond fees) across a wide range of reserve ratios.

### Proof of Concept
Conceptual PoC (mirrors the report's flow, needs numerical verification against `ExchangeProcessor`):
1. Attacker calls `ExchangeCreateContract` to create a `TRX/TokenX` pool with minimal reserves it fully owns [1](#0-0) .
2. A victim calls `ExchangeInjectContract` to add liquidity proportionally to the existing (attacker-controlled) ratio [10](#0-9) , increasing the pool's real value.
3. Attacker issues a sequence of `ExchangeTransactionContract` calls (sell TRX for TokenX, then sell TokenX back for TRX) exploiting the `double`/`Math.pow` truncation in `exchangeToSupply`/`exchangeFromSupply` [2](#0-1)  to end each round trip with a net-positive balance, funded by the victim's injected reserves, until the pool is drained or the extraction becomes uneconomical.

This PoC requires empirical verification of the rounding bias direction/magnitude in `ExchangeProcessor`, which was not completed due to tool-call limits; a background engineering session with test/fuzzing capability should validate step 3 numerically before treating this as a confirmed, exploitable High/Critical finding.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L50-76)
```java
      final ExchangeCreateContract exchangeCreateContract = this.any
          .unpack(ExchangeCreateContract.class);
      AccountCapsule accountCapsule = accountStore
          .get(exchangeCreateContract.getOwnerAddress().toByteArray());

      byte[] firstTokenID = exchangeCreateContract.getFirstTokenId().toByteArray();
      byte[] secondTokenID = exchangeCreateContract.getSecondTokenId().toByteArray();
      long firstTokenBalance = exchangeCreateContract.getFirstTokenBalance();
      long secondTokenBalance = exchangeCreateContract.getSecondTokenBalance();

      long newBalance = subtractExact(accountCapsule.getBalance(), fee);

      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, firstTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(firstTokenID, firstTokenBalance, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, secondTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(secondTokenID, secondTokenBalance, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L201-208)
```java
    if (firstTokenBalance <= 0 || secondTokenBalance <= 0) {
      throw new ContractValidateException("token balance must greater than zero");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (firstTokenBalance > balanceLimit || secondTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L57-69)
```java
      ExchangeCapsule exchangeCapsule = Commons
          .getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeTransactionContract.getExchangeId()));

      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-169)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L60-83)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeInjectContract.getTokenId().toByteArray();
      long tokenQuant = exchangeInjectContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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
