## Analog Found

### Title
Exchange liquidity operations (`ExchangeInjectActuator`/`ExchangeWithdrawActuator`) price token amounts off the manipulable instantaneous pool balances with no slippage protection - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
TRON's built-in bancor-style `Exchange` market maintains `firstTokenBalance`/`secondTokenBalance` on `ExchangeCapsule` that function as the pool's "spot price," directly analogous to Uniswap V3's `slot0`. Just like the reported `LiquidityManager.sol` issue, both `ExchangeInjectActuator` and `ExchangeWithdrawActuator` compute the counter-asset amount for a liquidity operation purely from this instantaneous, single-block balance ratio, with no TWAP and no slippage/expected-amount protection.

### Finding Description
In `ExchangeInjectActuator.doValidate`/`execute`, the amount of the "other" token required to inject liquidity is computed directly from the current pool ratio: `anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance)` [1](#0-0) . There is no caller-supplied minimum/maximum bound on `anotherTokenQuant` anywhere in the contract or validation logic — contrast this with `ExchangeTransactionActuator`, which enforces `anotherTokenQuant >= tokenExpected` as slippage protection [2](#0-1) .

`ExchangeWithdrawActuator` has the same structure: `anotherTokenQuant` is derived from the live pool ratio (`bigSecondTokenBalance.multiply(bigTokenQuant).divideToIntegralValue(bigFirstTokenBalance)`) [3](#0-2) , with only a fixed 0.01% internal rounding-precision check ("Not precise enough"), not a market-manipulation guard [4](#0-3) .

Both actuators restrict the caller to the exchange's creator (`account is not creator` checks) [5](#0-4) [6](#0-5) , but this creator is an ordinary account, not a privileged node role — it is the market/asset issuer persona explicitly in scope. Any unprivileged trader can call the public `ExchangeTransactionContract` (swap) actuator to move the pool's balances arbitrarily within the same block/mempool window before the creator's inject/withdraw executes [7](#0-6) , then reverse the swap afterward — a classic sandwich attack against the spot-ratio calculation, exactly the bug class described in the report (manipulable spot price feeding liquidity math with no TWAP).

### Impact Explanation
An attacker who can observe a pending `ExchangeInjectContract` or `ExchangeWithdrawContract` from the exchange creator can sandwich it with `ExchangeTransactionContract` swaps to skew `firstTokenBalance`/`secondTokenBalance` immediately before it executes, forcing the creator to inject an unfairly large amount of the counter-asset, or to withdraw less counter-asset value than the pool's true balance, extracting value from the creator/pool. This is a concrete economic loss (unbacked value extraction) reachable purely through unprivileged, signed transactions.

### Likelihood Explanation
Exploitation requires only ordinary account permissions and knowledge of a pending inject/withdraw transaction (visible in mempool) — no special network position, key compromise, or node privilege is needed. The lack of a slippage parameter (unlike `ExchangeTransactionActuator`, which has one) means there is no built-in defense once such a transaction is broadcast.

### Recommendation
Add a caller-supplied minimum/maximum bound (slippage protection) for `anotherTokenQuant` in both `ExchangeInjectContract` and `ExchangeWithdrawContract`, analogous to the `tokenExpected` field already used in `ExchangeTransactionContract`, so liquidity providers can reject execution if the pool ratio has moved unfavorably since transaction construction.

### Proof of Concept
1. Exchange creator broadcasts `ExchangeInjectContract` intending to inject `tokenQuant` of `firstToken` at the currently observed ratio.
2. Attacker observes this pending transaction and broadcasts an `ExchangeTransactionContract` selling a large amount of `firstToken` into the pool just before it, which is processed first (e.g., via fee/ordering), shifting `firstTokenBalance`/`secondTokenBalance` [8](#0-7) .
3. The creator's `ExchangeInjectActuator.execute` then computes `anotherTokenQuant` from the now-skewed balances [9](#0-8) , forcing the creator to deposit far more of the counter-asset than intended, with no check to reject the unfavorable ratio.
4. Attacker reverses their swap afterward, pocketing the difference extracted from the creator's injected liquidity.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-227)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L51-99)
```java
    try {
      final ExchangeTransactionContract exchangeTransactionContract = this.any
          .unpack(ExchangeTransactionContract.class);
      AccountCapsule accountCapsule = accountStore
          .get(exchangeTransactionContract.getOwnerAddress().toByteArray());

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L218-223)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
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
