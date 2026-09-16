### Title
Missing slippage/minimum-output protection in Exchange liquidity operations enables sandwich-attack fund loss - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
`ExchangeInjectContract` and `ExchangeWithdrawContract` — the bancor-style AMM liquidity add/remove operations in java-tron's built-in Exchange — carry no user-specified minimum-output ("slippage") field at all, unlike `ExchangeTransactionContract` which has an `expected` field that is checked in `ExchangeTransactionActuator`. Any unprivileged account can broadcast an `ExchangeInjectContract` or `ExchangeWithdrawContract` transaction and have it executed at whatever pool ratio exists at inclusion time, with zero ability to bound the resulting paired-token amount. This mirrors the reported bug class ("slippage protection only partially/conditionally applied") but is even more complete: for these two entrypoints the protection is entirely absent, not merely conditional.

### Finding Description
`ExchangeTransactionContract` includes an `expected` field [1](#0-0)  which `ExchangeTransactionActuator.doValidate()` enforces against the computed `anotherTokenQuant` before execution [2](#0-1) .

In contrast, `ExchangeInjectContract` and `ExchangeWithdrawContract` only carry `owner_address`, `exchange_id`, `token_id`, and `quant` — no minimum/expected counterpart amount field exists in the protocol definition [3](#0-2) .

`ExchangeInjectActuator.execute()` computes the paired-token amount (`anotherTokenQuant`) purely from the current on-chain pool ratio via `floorDiv(multiplyExact(...))`/`BigInteger` division and immediately debits both amounts from the caller — there is no check comparing `anotherTokenQuant` to any caller-supplied floor [4](#0-3) . The validate path (`doValidate`) similarly only rechecks balances/limits, never a slippage bound supplied by the caller [5](#0-4) .

`ExchangeWithdrawActuator` behaves the same way: `anotherTokenQuant` is derived solely from the live pool ratio in both `execute()` [6](#0-5)  and `doValidate()` [7](#0-6) , with only a rounding/"precision" sanity check (`Not precise enough`), never a user-defined minimum acceptable amount.

Because the Exchange balances (`firstTokenBalance`/`secondTokenBalance`) can be shifted arbitrarily between the time a victim signs/broadcasts an Inject or Withdraw transaction and the time it is packed into a block — e.g., by any other account broadcasting an `ExchangeTransactionContract` that swaps a large quantity through the same `exchange_id` — the victim's Inject/Withdraw executes against a manipulated ratio with no recourse, since the field to reject unfavorable execution does not exist in the contract schema.

### Impact Explanation
This is a "market order handling" surface reachable by any unprivileged account: an anonymous order placer only needs to know a victim's pending `ExchangeInjectContract`/`ExchangeWithdrawContract` (observable in the mempool) and race a same-exchange `ExchangeTransactionContract` ahead of it, or simply rely on natural price movement in a volatile pool between signing and confirmation. The victim's liquidity deposit/withdrawal is then executed at an economically unfavorable ratio, permanently transferring value to whoever moved the price (classic sandwich extraction), i.e., concrete theft/permanent loss of user funds with no way for the transaction author to protect themselves.

### Likelihood Explanation
Every parameter needed (`exchange_id`, `token_id`, `quant`) is attacker/observer-visible from the broadcast transaction itself, and TRON's mempool/block-production timing makes front-running/back-running trivial for any account willing to pay the (fee-free, `calcFee()==0`) Exchange transactions. No special privilege, witness collusion, or contract deployment is required — only ordinary signed transactions against an existing Exchange pair.

### Recommendation
Add an `expected`/minimum-output field to `ExchangeInjectContract` and `ExchangeWithdrawContract` (analogous to `ExchangeTransactionContract.expected`), and enforce it in `ExchangeInjectActuator`/`ExchangeWithdrawActuator`'s `doValidate()` by rejecting the transaction if the computed `anotherTokenQuant` falls below (for inject) or above (for withdraw) the caller-specified bound, mirroring the check already present in `ExchangeTransactionActuator.doValidate()` at [2](#0-1) .

### Proof of Concept
1. Attacker observes a pending `ExchangeInjectContract` from victim V targeting `exchange_id=E` injecting `quant` of `tokenID` into pool with `firstTokenBalance`/`secondTokenBalance`.
2. Attacker immediately broadcasts an `ExchangeTransactionContract` on the same `exchange_id=E` that shifts the pool ratio unfavorably for V's upcoming inject direction (e.g., sells a large amount of the token V is injecting, or the paired token, to skew the ratio).
3. If the attacker's transaction is confirmed first (same block ordering by SR, or an earlier block since Exchange txs have 0 fee and no ordering guarantee benefits the victim), V's `ExchangeInjectActuator.execute()` computes `anotherTokenQuant` from the now-skewed `firstTokenBalance`/`secondTokenBalance` [8](#0-7) , causing V to receive proportionally less value than intended, with no `expected` field present to have blocked the unfavorable execution.
4. Attacker then reverses their position (e.g., via a follow-up `ExchangeTransactionContract` or `ExchangeWithdrawContract`), realizing the value extracted from V's forced unfavorable injection — the same mechanism applies symmetrically to `ExchangeWithdrawActuator`.

### Citations

**File:** Tron protobuf protocol document.md (L1384-1420)
```markdown
     - message `ExchangeInjectContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to inject.
    
       `quant`: token amount to inject.
    
      ```java
      message ExchangeInjectContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
      }
      ```
    
     - message `ExchangeWithdrawContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to withdraw.
    
       `quant`: token amount to withdraw.
    
      ```java
      message ExchangeWithdrawContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
      }
      ```
```

**File:** Tron protobuf protocol document.md (L1422-1442)
```markdown
     - message `ExchangeTransactionContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to sell.
    
       `quant`: token amount to sell.
    
       `expected`: expected minimum number of tokens.
    
      ```java
      message ExchangeTransactionContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
          int64 expected = 5;
      }
      ```
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L65-99)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-272)
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

    } else {
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigSecondTokenBalance).longValueExact();
      if (secondTokenBalance < tokenQuant || firstTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }

      if (allowHarden) {
        BigDecimal remainder = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }
    }
```
