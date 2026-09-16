### Title
`ExchangeInjectContract` and `ExchangeWithdrawContract` compute counter-token amounts from live AMM pool state with no user-supplied slippage bound - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java])

### Summary
The external report flags `RioLRTCoordinator.deposit()` for converting an input amount into an output amount using a live, externally-influenced price with no way for the caller to bound the result (no min-out parameter). The same bug class exists in java-tron's built-in bancor-style Exchange (AMM) feature: `ExchangeInjectContract` and `ExchangeWithdrawContract` compute the paired-token amount from the exchange pool's live ratio at execution time, but unlike `ExchangeTransactionContract` (which carries an `expected` field checked against the computed output), neither of these two contract types has any user-supplied bound field in their protobuf definitions or actuators.

### Finding Description
`ExchangeTransactionContract` (a trade against the AMM) was hardened with an `expected` field, and `ExchangeTransactionActuator.doValidate()` explicitly rejects the transaction if the computed output is less than the caller's expectation: [1](#0-0) 

By contrast, `ExchangeInjectContract` and `ExchangeWithdrawContract` have no such field at all: [2](#0-1) 

`ExchangeInjectActuator.execute()` computes the paired-token amount (`anotherTokenQuant`) purely from the exchange's current on-chain balances at execution time, then reduces that exact amount from the caller's account balance: [3](#0-2) 

The same pattern applies to `ExchangeWithdrawActuator.execute()`, which computes `anotherTokenQuant` from live pool balances and credits it to the caller: [4](#0-3) 

The only bound present in `ExchangeWithdrawActuator.doValidate()` is a "Not precise enough" check comparing the `BigDecimal`-computed amount against the actuator's own integer-math result — a rounding-consistency check between two independently computed values within the same call, not a caller-supplied minimum/maximum acceptable to the user against pool-ratio drift caused by other transactions: [5](#0-4) 

Because the exchange pool ratio (`firstTokenBalance`/`secondTokenBalance`) can be altered by any other account's `ExchangeTransactionContract` broadcast before the victim's `ExchangeInject`/`ExchangeWithdraw` transaction is packed into the same or a later block, the amount actually charged/paid at execution time can diverge arbitrarily from what the caller observed and intended when constructing/signing their transaction — exactly the "convert asset amount based on live external state with no slippage bound" pattern described in the original report.

### Impact Explanation
- For `ExchangeInject`: only the exchange creator can call it (enforced by `!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())` check), but that creator can be forced to inject a materially larger quantity of the paired token than intended if pool ratio shifts between transaction construction and inclusion, resulting in an unintended asset loss.
- For `ExchangeWithdraw`: similarly restricted to the creator, but the creator can receive materially less of the paired token than expected if the ratio is manipulated (e.g., via a same-block `ExchangeTransactionContract`) before their withdrawal executes.
- Because both actuators are gated to the exchange creator, this is more limited in blast radius than an open, permissionless `deposit()`; still, it is a concrete, exploitable inconsistency where one of three AMM interaction contracts (`ExchangeTransactionContract`) received slippage protection while the other two (`ExchangeInjectContract`, `ExchangeWithdrawContract`) did not, despite performing conceptually the same "convert amount via live price/ratio" operation the report calls out.

### Likelihood Explanation
Exploitation requires the attacker to be able to influence the exchange's pool ratio via their own `ExchangeTransactionContract` transactions and have it processed in the same block or ahead of the victim's inject/withdraw transaction — a scenario within reach of any account able to broadcast transactions, without requiring any special privilege, consistent with the "single signed transaction" reachability constraint. The creator-only gating reduces the population of at-risk accounts to exchange creators, which somewhat lowers overall likelihood compared to a fully permissionless `deposit()`.

### Recommendation
Add an `expected`/`min_another_token_quant` (and, for inject, potentially a `max_another_token_quant`) field to `ExchangeInjectContract` and `ExchangeWithdrawContract`, mirroring the pattern already used in `ExchangeTransactionContract`, and enforce it in `ExchangeInjectActuator.doValidate()` / `ExchangeWithdrawActuator.doValidate()` before committing balance changes.

### Proof of Concept
Not independently reproduced in this analysis (no test harness run); the analog is derived by direct comparison of the three Exchange actuators/protos in the repository, which shows `ExchangeTransactionContract` alone carries the `expected` slippage-bound field: [6](#0-5) 
while `ExchangeInjectActuator` and `ExchangeWithdrawActuator` compute and apply amounts from live pool state with no equivalent caller-supplied bound, as cited above.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L17-37)
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

message ExchangeTransactionContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
  int64 expected = 5;
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-99)
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

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .reduceAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-104)
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

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, tokenQuant));
      } else {
        accountCapsule.addAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
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
