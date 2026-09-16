### Title
Missing minimum-output (slippage) protection in `ExchangeInjectContract`/`ExchangeWithdrawContract` allows front-run to force sub-optimal token amounts - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeTransactionContract` explicitly protects the caller from slippage by requiring an `expected` minimum-received amount that is checked before execution, but the sibling contracts `ExchangeInjectContract` and `ExchangeWithdrawContract` have no such field, so the counter-token amount computed at execution time can silently diverge from what the caller intended when they signed the transaction.

### Finding Description
`ExchangeTransactionContract` carries an `expected` field [1](#0-0) , and `ExchangeTransactionActuator.doValidate()` reverts the transaction if the computed `anotherTokenQuant` is below that `expected` value [2](#0-1) .

In contrast, `ExchangeInjectContract` and `ExchangeWithdrawContract` only carry `owner_address`, `exchange_id`, `token_id`, and `quant` — there is no minimum/expected counter-token amount field at all [3](#0-2) . In `ExchangeWithdrawActuator`, the amount of the "other" token the caller receives (`anotherTokenQuant`) is computed purely from the exchange's current on-chain reserve ratio at execution time (`firstTokenBalance`/`secondTokenBalance` from the `ExchangeCapsule`) and the caller-specified `quant`, with no bound the caller can impose [4](#0-3) . The `doValidate()` method for withdraw only checks precision/rounding tolerance ("Not precise enough"), never a caller-specified minimum [5](#0-4) . The same pattern applies to `ExchangeInjectActuator`, whose `anotherTokenQuant` is likewise derived only from the live reserve ratio with no lower/upper bound check [6](#0-5) .

Because `ExchangeTransactionContract` is permissionlessly callable by any account (any unprivileged trader), any address can submit a trade against the exchange pool between the time an `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction is signed and the time it is packed into a block, shifting `firstTokenBalance`/`secondTokenBalance`. Since inject/withdraw carry no `expected`/minimum-output guard, the actuator will execute with whatever ratio exists at that later block, silently giving the caller a drastically different (and potentially far less favorable) counter-token amount than intended — with no revert path available to them.

### Impact Explanation
An exchange-pool participant (the exchange creator, who is the only account authorized to inject/withdraw per the "is not creator" checks [7](#0-6) [8](#0-7) ) can be sandwiched by ordinary `ExchangeTransactionContract` trades from any account, causing them to receive an unbacked/sub-optimal amount of the counter token on withdrawal, or to inject an unfavorable ratio, resulting in real economic loss with no on-chain protection. This is a concrete loss-of-funds vector reachable purely by broadcasting standard signed transactions.

### Likelihood Explanation
Any account can broadcast `ExchangeTransactionContract` transactions against a target exchange pool at will, and transaction ordering within a block/mempool is influenceable by fee/priority, so timing a trade immediately before a known inject/withdraw is straightforward. The lack of any `expected` parameter on `ExchangeInjectContract`/`ExchangeWithdrawContract` means there is no way for the victim to opt into protection, making this reliably exploitable whenever an exchange creator performs inject/withdraw operations on an active pool.

### Recommendation
Add a caller-specified minimum (and/or maximum) expected counter-token amount field to `ExchangeInjectContract` and `ExchangeWithdrawContract` (mirroring the `expected` field in `ExchangeTransactionContract`), and validate the computed `anotherTokenQuant` against it in `ExchangeInjectActuator.doValidate()`/`ExchangeWithdrawActuator.doValidate()`, reverting with a `ContractValidateException` if the bound is violated, consistent with the existing `"token required must greater than expected"` check in `ExchangeTransactionActuator`.

### Proof of Concept
1. An exchange creator holds an exchange pool with reserves `firstTokenBalance = A`, `secondTokenBalance = B`, and signs an `ExchangeWithdrawContract` for `quant` of the first token, expecting `anotherTokenQuant ≈ B*quant/A` of the second token.
2. Before this transaction is included in a block, any unprivileged account broadcasts an `ExchangeTransactionContract` trade that shifts the pool ratio (e.g., sells a large amount of the second token into the pool), moving reserves to `A' , B'` where `B'/A' << B/A`.
3. When the creator's withdraw transaction executes, `ExchangeWithdrawActuator.execute()` recomputes `anotherTokenQuant` from the now-shifted `A'`/`B'` ratio [9](#0-8) , yielding a far smaller counter-token payout than the creator expected, and the transaction succeeds because there is no minimum-output check to revert it.

### Citations

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L17-29)
```text
message ExchangeInjectContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}

message ExchangeWithdrawContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}
```

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L31-37)
```text
message ExchangeTransactionContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
  int64 expected = 5;
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L63-89)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeWithdrawContract.getTokenId().toByteArray();
      long tokenQuant = exchangeWithdrawContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
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
