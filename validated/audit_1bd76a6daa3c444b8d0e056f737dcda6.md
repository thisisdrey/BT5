### Title
Missing slippage/minimum-output protection in `ExchangeWithdrawActuator` and `ExchangeInjectActuator` enables MEV sandwich attacks on TRC10 Bancor-style exchanges - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
`ExchangeTransactionActuator` (the trade/swap path of java-tron's built-in TRC10 Bancor exchange) protects callers from adverse price movement by requiring a caller-supplied `expected` amount that is validated against the amount actually computed at execution time. `ExchangeWithdrawActuator` and `ExchangeInjectActuator`, which change the exchange's liquidity pool balances, compute the counter-token amount purely from the pool's live `firstTokenBalance`/`secondTokenBalance` ratio at execution time with no caller-supplied minimum/maximum bound, exactly the missing-slippage-parameter pattern described in the referenced report.

### Finding Description
In `ExchangeTransactionActuator.doValidate()` the contract carries a `tokenExpected` field that is checked against the freshly computed `anotherTokenQuant`, rejecting the transaction if the received amount would be less than the caller's expectation: [1](#0-0) 

By contrast, `ExchangeWithdrawActuator.execute()`/`doValidate()` computes `anotherTokenQuant` solely from the exchange's current pool balances (`firstTokenBalance`, `secondTokenBalance`) and the requested `tokenQuant`, with no field in `ExchangeWithdrawContract` allowing the caller to bound the acceptable output: [2](#0-1) 
The only sanity check performed is an internal precision/rounding consistency check ("Not precise enough"), which validates the actuator's own math is self-consistent — it does not protect against the pool ratio having moved between the time the creator crafted/broadcast the transaction and the time it is executed on-chain: [3](#0-2) 

`ExchangeInjectActuator` has the identical structural gap: `anotherTokenQuant` is derived from the live pool ratio with no expected/minimum bound parameter in `ExchangeInjectContract`: [4](#0-3) 

Because pool ratios are shared, publicly-readable state that any unprivileged account can move by broadcasting an `ExchangeTransactionContract` swap (buy/sell against the same exchange), an attacker who observes a pending withdraw/inject transaction in the mempool can:
1. Front-run it with a swap that skews the first/second token ratio in the direction that minimizes the amount the withdrawer/injector will receive/be charged for the counter-token.
2. Let the victim's withdraw/inject execute against the manipulated ratio.
3. Back-run with a reverse swap to restore the ratio and capture the extracted value, classic sandwich/MEV pattern identical to the referenced `claimRewardAsMochi` finding.

### Impact Explanation
An attacker can extract value from the exchange creator performing a withdraw or inject operation by manipulating the pool price immediately before execution, since there is no way for the creator to specify a minimum acceptable `anotherTokenQuant` (for withdraw) or a maximum acceptable `anotherTokenQuant` cost (for inject). This constitutes unauthorized value extraction/theft of funds from the withdrawing/injecting account via transaction ordering manipulation, matching a Medium-severity fund-loss class analogous to the reported "no-slippage-check" MEV issue.

### Likelihood Explanation
Any account can call `ExchangeTransactionActuator` against the target exchange pair since it has no permission checks beyond signature/balance validation, and transaction ordering within a block/producer window is influenced by fee/broadcast timing, making the sandwich pattern practically executable by anyone monitoring the mempool for pending `ExchangeWithdrawContract`/`ExchangeInjectContract` transactions from exchange creators.

### Recommendation
Add an explicit slippage-bound field to `ExchangeWithdrawContract` and `ExchangeInjectContract` (e.g., `expected`/`minReceived` for withdraw and `maxAnotherTokenQuant` for inject), mirroring the pattern already implemented in `ExchangeTransactionContract`/`ExchangeTransactionActuator`, and validate the computed `anotherTokenQuant` against it in `doValidate()` before allowing execution.

### Proof of Concept
1. Attacker monitors the mempool for a pending `ExchangeWithdrawContract` transaction from an exchange creator targeting exchange `X`.
2. Attacker broadcasts an `ExchangeTransactionContract` swap against exchange `X` with higher energy/fee to be included first, shifting `firstTokenBalance`/`secondTokenBalance` unfavorably for the pending withdraw.
3. The victim's `ExchangeWithdrawActuator.execute()` computes `anotherTokenQuant` off the now-skewed ratio at [5](#0-4) , causing the creator to receive less counter-token than intended, with no field to reject the unfavorable outcome.
4. Attacker submits a reverse swap to restore the ratio, realizing the difference as profit — an on-chain sandwich attack requiring only ordinary signed transactions.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L228-243)
```java
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
