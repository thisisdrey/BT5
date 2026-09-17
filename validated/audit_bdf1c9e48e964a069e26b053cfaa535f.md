Confirmed: `ExchangeInjectContract` and `ExchangeWithdrawContract` only carry `owner_address`, `exchange_id`, `token_id`, `quant` — no `expected`/minimum-output field, unlike `ExchangeTransactionContract` which explicitly has `expected` at line 36 for slippage bound checking against `anotherTokenQuant`. [1](#0-0) 

### Title
Missing Slippage/Minimum-Output Protection in ExchangeInject and ExchangeWithdraw Actuators - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
The bancor-style TRC10 `Exchange` (AMM pool) feature in java-tron mints/burns liquidity proportionally to the current pool reserve ratio, exactly analogous to the `buyShares` mechanics described in the external report. While `ExchangeTransactionContract` (a swap) has an on-chain `expected` field enforced in `ExchangeTransactionActuator.doValidate()` to protect callers from slippage, `ExchangeInjectContract` and `ExchangeWithdrawContract` (liquidity add/remove) carry no equivalent minimum-output or ratio-bound field, so the counter-token amount computed at execution time can silently differ from what the signer expected when the transaction was created.

### Finding Description
`ExchangeInjectActuator.execute()`/`doValidate()` computes `anotherTokenQuant` purely from the exchange's live `firstTokenBalance`/`secondTokenBalance` at block-application time: [2](#0-1) 
There is no parameter in `ExchangeInjectContract` letting the caller bound the minimum acceptable `anotherTokenQuant` (or maximum `tokenQuant` debited) for the ratio at execution time — contrast with `ExchangeTransactionContract.expected`, which is validated against `anotherTokenQuant` before the swap is allowed to proceed: [3](#0-2) 
`ExchangeWithdrawActuator` has the same gap: it only enforces an internal precision-rounding tolerance (~0.0001), not an economically meaningful minimum-output bound chosen by the caller: [4](#0-3) 
Because a signed transaction can sit in the mempool for up to the network's expiration window before being packaged, and any intervening `ExchangeTransactionContract` (swap) by any third party moves the pool ratio, the eventual `anotherTokenQuant` realized by the inject/withdraw actuator can diverge materially from what the signer intended, with no on-chain mechanism to abort the operation.

Note: the on-chain transaction `expiration` field (checked in `Manager.validateCommon`) already provides deadline protection generically for all transactions, so only the slippage/minimum-output half of the external report's recommendation is unaddressed here. [5](#0-4) 

### Impact Explanation
The exchange creator (an unprivileged account that permissionlessly created the pool via `ExchangeCreateContract`) can lose value when injecting or withdrawing liquidity if the pool ratio shifts unfavorably between signing and execution: on inject, they may be forced to contribute a token amount they would not have agreed to at execution-time pricing; on withdraw, they may receive a smaller counter-token amount than expected for the token they redeem. This is a direct value-loss/slippage exposure reachable by a single unprivileged signed transaction, matching the audit report's "unfavorable execution" impact class.

### Likelihood Explanation
Any account can create an exchange pool and become its creator, then broadcast `ExchangeInjectContract`/`ExchangeWithdrawContract` transactions directly (bypassing any UI-level slippage checks, exactly as described in the report). Concurrent `ExchangeTransactionContract` swaps from unrelated parties routinely change the pool ratio between transaction broadcast and block inclusion, so the condition is easily triggered without requiring any privileged actor.

### Recommendation
Add an on-chain minimum/maximum bound field to `ExchangeInjectContract` and `ExchangeWithdrawContract` (mirroring `ExchangeTransactionContract.expected`), and validate it in `ExchangeInjectActuator.doValidate()`/`ExchangeWithdrawActuator.doValidate()` against the freshly computed `anotherTokenQuant`, reverting the transaction if the realized ratio falls outside the caller-specified tolerance.

### Proof of Concept
1. Account A creates an exchange pool via `ExchangeCreateContract` (becoming `creatorAddress`) with balances `firstTokenBalance = X`, `secondTokenBalance = Y`.
2. Account A signs and broadcasts an `ExchangeInjectContract` for `tokenQuant` of the first token, expecting `anotherTokenQuant ≈ Y/X * tokenQuant`.
3. Before A's transaction is packaged, Account B broadcasts an `ExchangeTransactionContract` swap that shifts the pool ratio significantly (e.g., large `tokenQuant` swap).
4. When A's inject transaction executes, `ExchangeInjectActuator` recomputes `anotherTokenQuant` from the now-shifted `firstTokenBalance`/`secondTokenBalance` (`ExchangeInjectActuator.java` lines 215-227), silently debiting A a different counter-token amount than intended, with no field/check available to make the transaction revert.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L846-858)
```java
    long transactionExpiration = transactionCapsule.getExpiration();
    long headBlockTime = chainBaseManager.getHeadBlockTimeStamp();
    if (transactionCapsule.isInBlock()
        && chainBaseManager.getDynamicPropertiesStore().allowConsensusLogicOptimization()) {
      transactionCapsule.checkExpiration(chainBaseManager.getNextBlockSlotTime());
    }
    if (transactionExpiration <= headBlockTime
        || transactionExpiration > headBlockTime + Constant.MAXIMUM_TIME_UNTIL_EXPIRATION) {
      throw new TransactionExpirationException(
          String.format(
          "Transaction expiration, transaction expiration time is %d, but headBlockTime is %d",
              transactionExpiration, headBlockTime));
    }
```
