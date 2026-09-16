This confirms the Bancor-style constant-product invariant (`ExchangeProcessor.exchange`) used for swaps has no protocol fee subtracted, so a round-trip swap around a manipulated inject/withdraw is a pure zero-fee arbitrage for the sandwicher, mirroring the Primitive `Portfolio` case where the LP is forced into an unfavorable ratio while the attacker profits risk-free.

### Title
Missing slippage protection in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` allows sandwich attacks on liquidity providers - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
`ExchangeInjectContract` and `ExchangeWithdrawContract` let the exchange creator add/remove liquidity by specifying only one side's amount (`quant`); the other side (`anotherTokenQuant`) is derived from the *current* on-chain pool ratio `firstTokenBalance`/`secondTokenBalance` at execution time, exactly like Primitive's `Portfolio.allocate/deallocate` computing `deltaAsset`/`deltaQuote` from the live `virtualX`/`virtualY` reserves. Neither actuator lets the caller bound the computed `anotherTokenQuant`, unlike `ExchangeTransactionActuator`, which does have an `expected` (minimum-received) guard.

### Finding Description
In `ExchangeInjectActuator.execute`/`doValidate`, the counter-token amount is computed purely from the pool's current balances: [1](#0-0) 
and validated the same way in `doValidate`, with no user-supplied bound on `anotherTokenQuant`: [2](#0-1) 

`ExchangeWithdrawActuator` has the identical structure — `anotherTokenQuant` is derived solely from the live ratio with only a "not precise enough" rounding check, not a slippage/price bound: [3](#0-2) [4](#0-3) 

Any unprivileged account can move the pool ratio immediately before the inject/withdraw transaction is packed into a block by broadcasting `ExchangeTransactionContract` swaps, since `ExchangeTransactionActuator` freely permits swaps subject only to the caller's own `expected` minimum and an `ExchangeBalanceLimit` cap: [5](#0-4) 
The underlying swap math (`ExchangeCapsule.transaction`) is a plain constant-product formula with no protocol fee taken out, so a swap immediately followed by its inverse swap is fee-free and only costs energy/bandwidth: [6](#0-5) 

This is the exact bug class from the external report: a single `uint128`/`quant`-style parameter is used to compute liquidity deltas from spot reserves with no min/max protection, letting an MEV searcher sandwich the LP's inject/withdraw transaction — swap to skew the ratio, let the LP's inject/withdraw execute at the skewed ratio, swap back to restore the ratio while pocketing the difference caused by the LP's forced-favorable trade.

### Impact Explanation
The exchange creator (an ordinary account, not privileged) loses funds when injecting or withdrawing liquidity around a block boundary they don't control, exactly mirroring the LP loss described in the Primitive report (LP forced into `9.08e18 X / 1.08 Y` instead of `3.08 X / 30.85 Y`). Because swaps carry no protocol fee, the round-trip sandwich is close to risk-free for the attacker, who only pays TRX bandwidth/energy costs. This is a theft-of-funds vector against exchange creators, satisfying the "concrete unauthorized ... theft ... of funds" bar, though it is scoped to the legacy TRX-token `Exchange`/`ExchangeV2` bancor markets rather than a smart contract like Portfolio.sol.

### Likelihood Explanation
Exploitation only requires: (1) knowledge of a pending `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction in the mempool (public before block inclusion), and (2) the ability to broadcast two `ExchangeTransactionContract` swaps around it within the same block — all reachable by any unprivileged account holding the relevant tokens/TRX, with no special permission beyond normal transaction broadcasting. This is analogous to garden-variety MEV sandwiching on public mempools/block producers, so likelihood is high wherever such exchanges hold meaningful liquidity.

### Recommendation
Add optional `minAnotherTokenQuant` (for inject) and `minTokenQuant`/`minAnotherTokenQuant` (for withdraw, and a `maxAnotherTokenQuant` for inject) parameters to `ExchangeInjectContract`/`ExchangeWithdrawContract`, and revert in `doValidate`/`execute` when the actual computed `anotherTokenQuant` falls outside the caller-specified bounds — mirroring the `expected` field already present on `ExchangeTransactionContract`.

### Proof of Concept
1. Exchange creator broadcasts `ExchangeInjectContract` to add liquidity proportional to the current pool ratio.
2. Before that transaction is packaged, an attacker broadcasts an `ExchangeTransactionContract` swap that heavily skews `firstTokenBalance`/`secondTokenBalance` (bounded only by their own `expected` and `getExchangeBalanceLimit()`), analogous to step 1 in the report's PoC.
3. The creator's inject executes at the skewed ratio; `anotherTokenQuant` in `ExchangeInjectActuator.execute` (lines 71-83) is computed against the manipulated balances, forcing the creator to deposit a disadvantageous ratio of tokens with no bound check.
4. The attacker broadcasts the inverse `ExchangeTransactionContract` swap, restoring the ratio and extracting the value the creator was forced to overpay, at negligible protocol-fee cost since `ExchangeCapsule.transaction` (lines 124-169) applies no fee.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-231)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenID = secondTokenID;
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divide(bigFirstTokenBalance).longValueExact();
      newTokenBalance = addExact(firstTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(secondTokenBalance, anotherTokenQuant);
    } else {
      anotherTokenID = firstTokenID;
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divide(bigSecondTokenBalance).longValueExact();
      newTokenBalance = addExact(secondTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(firstTokenBalance, anotherTokenQuant);
    }

    if (anotherTokenQuant <= 0) {
      throw new ContractValidateException("the calculated token quant  must be greater than 0");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-89)
```java
      BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
      BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
      BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, tokenQuant),
            subtractExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, anotherTokenQuant),
            subtractExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-243)
```java
    BigDecimal bigFirstTokenBalance = new BigDecimal(String.valueOf(firstTokenBalance));
    BigDecimal bigSecondTokenBalance = new BigDecimal(String.valueOf(secondTokenBalance));
    BigDecimal bigTokenQuant = new BigDecimal(String.valueOf(tokenQuant));
    final boolean allowHarden = allowHarden();
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }
      if (allowHarden) {
        BigDecimal remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-68)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
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
