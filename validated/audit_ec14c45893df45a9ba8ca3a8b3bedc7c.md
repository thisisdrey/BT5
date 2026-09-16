## Analog Found: Missing Slippage Protection in `ExchangeInjectActuator` / `ExchangeWithdrawActuator`

### Title
Missing slippage/minimum-output protection allows MEV sandwiching of `ExchangeInjectContract` and `ExchangeWithdrawContract` transactions - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
Tron's built-in Bancor-style TRC10 exchange supports `ExchangeInject` and `ExchangeWithdraw` operations, which are restricted to the exchange's creator (an "asset issuer" role). Both actuators compute the counter-token amount (`anotherTokenQuant`) from the pool's live balance ratio at execution time, with no user-supplied minimum/maximum bound to protect against ratio manipulation. This is the same missing-slippage-tolerance bug class as the reported issue, except the "swap with `amountOutMin = 0`" is here a proportional-balance calculation with no expected-value check at all.

### Finding Description
`ExchangeTransactionActuator` — the actual swap operation on this exchange — is properly protected: callers supply `expected` and the actuator validates `anotherTokenQuant < tokenExpected` before executing: [1](#0-0) 

In contrast, `ExchangeInjectActuator.execute` computes `anotherTokenQuant` purely from the pool's current `firstTokenBalance`/`secondTokenBalance` ratio and the requested `tokenQuant`, with no minimum/maximum bound supplied by the caller: [2](#0-1) 

The `doValidate` method for Inject only checks that the calculated amount is `> 0` and within a balance limit — never that it matches any expectation from the creator: [3](#0-2) 

Similarly, `ExchangeWithdrawActuator.execute` computes `anotherTokenQuant` from the live pool ratio with only a rounding-precision check ("Not precise enough"), not a price/ratio-manipulation check: [4](#0-3) [5](#0-4) 

Because the pool ratio (`firstTokenBalance`/`secondTokenBalance`) can be freely moved by anyone submitting an `ExchangeTransactionContract` trade against that same exchange (any account, not just the creator), an attacker who observes a pending `ExchangeInject`/`ExchangeWithdraw` transaction in the mempool can:
1. Front-run it with an `ExchangeTransactionContract` trade that skews the pool ratio in their favor.
2. Let the victim's Inject/Withdraw execute at the skewed ratio, forcing the creator to deposit more of the second token (Inject) or receive less of the second token (Withdraw) than the ratio at submission time implied.
3. Back-run with a reverse trade to restore the ratio and capture the extracted value.

This is exactly the "0 slippage tolerance" bug class from the external report, just applied to the on-chain AMM-style exchange's liquidity operations instead of a swap function.

### Impact Explanation
The exchange creator (an asset-issuer-equivalent role, explicitly in scope) loses value on every `ExchangeInject`/`ExchangeWithdraw` transaction that goes through the public transaction pool, since there is no way to bound the acceptable counter-token amount. This is a concrete theft-of-funds vector: the attacker profits at the direct expense of the liquidity provider, extracting value from the exchange pool (which indirectly affects all other holders of pool tokens as well, since the pool ratio is permanently skewed by the attack round-trip, subject to fees/rounding).

### Likelihood Explanation
Likelihood is high wherever the exchange pool has meaningful liquidity and any account can submit `ExchangeTransactionContract` trades against it (see `ExchangeTransactionActuator.execute`), which is the normal operating mode of TRC10 exchanges. An attacker only needs to observe the target's pending Inject/Withdraw transaction and race two of their own trade transactions around it — a standard, well-understood MEV sandwich pattern requiring no special privileges, matching the reachable-by-signed-transaction criteria.

### Recommendation
Add an explicit slippage-bound field to `ExchangeInjectContract` and `ExchangeWithdrawContract` (analogous to `ExchangeTransactionContract.expected`), and validate the computed `anotherTokenQuant` against it in `ExchangeInjectActuator`/`ExchangeWithdrawActuator`'s `doValidate`/`execute`, rejecting the transaction if the ratio has moved outside the caller's tolerance — mirroring the protection already present in `ExchangeTransactionActuator`.

### Proof of Concept
1. Exchange creator submits `ExchangeInjectContract` intending to inject `tokenQuant` of `firstTokenID` at the currently observed ratio, expecting to also spend a known amount of `secondTokenID`.
2. Attacker observes this pending transaction and front-runs it with an `ExchangeTransactionContract` (validated/executed via `ExchangeTransactionActuator`) that shifts `firstTokenBalance`/`secondTokenBalance` heavily in one direction.
3. The victim's `ExchangeInjectActuator.execute` runs against the now-skewed ratio at lines [6](#0-5) , computing a much larger `anotherTokenQuant` than the creator intended, and deducting it from their account via `reduceAssetAmountV2`.
4. Attacker back-runs with a reverse `ExchangeTransactionContract` trade to restore the ratio, pocketing the difference extracted from the victim's inject operation (or, symmetrically, causing a withdraw to return less than expected).

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L65-83)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L218-243)
```java
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
