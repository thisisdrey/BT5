## Analysis Result



### Title
Missing slippage protection in `ExchangeInjectContract`/`ExchangeWithdrawContract` allows front-running via unrestricted `ExchangeTransactionContract` swaps - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeInjectActuator` and `ExchangeWithdrawActuator` compute the counter-token amount (`anotherTokenQuant`) purely from the exchange's current on-chain reserve ratio at execution time, with no user-supplied minimum/maximum bound to protect against price movement between transaction submission and execution.

### Finding Description
`ExchangeInjectContract` and `ExchangeWithdrawContract` only carry `owner_address`, `exchange_id`, `token_id`, and `quant` — unlike `ExchangeTransactionContract`, which additionally carries an `expected` field used as a slippage guard [1](#0-0) .

In `ExchangeTransactionActuator.doValidate()`, the resulting swap amount is checked against the caller-supplied `expected` and the transaction reverts if it is unfavorable: [2](#0-1) .

By contrast, `ExchangeInjectActuator.doValidate()` computes `anotherTokenQuant` directly from the live pool ratio (`firstTokenBalance`/`secondTokenBalance`) with no bound check other than `anotherTokenQuant <= 0` and the balance limit: [3](#0-2) .

`ExchangeWithdrawActuator.doValidate()` similarly derives `anotherTokenQuant` from the current ratio with only a "not precise enough" and balance-sufficiency check, but no user-defined minimum acceptable amount: [4](#0-3) .

Critically, while `ExchangeInject`/`ExchangeWithdraw` are restricted to the exchange creator (`if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress()))`) [5](#0-4) , the counter-party operation that moves the ratio — `ExchangeTransactionContract` (a swap) — is callable by **any** unprivileged account, since `ExchangeTransactionActuator` has no ownership/creator restriction, only balance checks [6](#0-5) . This makes it trivial for an unprivileged transaction broadcaster to observe a pending `ExchangeInject`/`ExchangeWithdraw` in the mempool and submit a swap transaction ordered right before it (or via same-block ordering influence) to shift `firstTokenBalance`/`secondTokenBalance`, changing the resulting `anotherTokenQuant` the creator's inject/withdraw will compute — with no revert mechanism available to the victim to cap their loss.

### Impact Explanation
An attacker who is not privileged in any way (any account able to submit a signed `ExchangeTransactionContract` trade) can manipulate the exchange pool ratio immediately before a creator's inject/withdraw executes, causing the creator to either deposit more value than intended for a given liquidity share, or withdraw a counter-token amount worse than anticipated. This is a value-extraction/loss-of-funds bug reachable purely through the ordering of independently-submitted, otherwise-valid transactions.

### Likelihood Explanation
Any account can call `ExchangeTransactionContract` to move the ratio; transaction ordering within or across blocks (front-running) is generally achievable by unprivileged parties, and the vulnerable inject/withdraw path has no mitigation whatsoever (no `expected`/slippage field), so exploitation requires no special privileges beyond normal transaction broadcasting.

### Recommendation
Add a caller-supplied minimum/maximum bound (analogous to `expected` in `ExchangeTransactionContract`) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, and enforce it in `ExchangeInjectActuator.doValidate()` / `ExchangeWithdrawActuator.doValidate()`, reverting the transaction if the computed `anotherTokenQuant` falls outside the caller's tolerance.

### Proof of Concept
1. Exchange creator submits `ExchangeWithdrawContract` expecting `anotherTokenQuant` ≈ X based on the currently observed `firstTokenBalance`/`secondTokenBalance`.
2. An unrelated, unprivileged attacker observes this pending transaction and submits an `ExchangeTransactionContract` swap that shifts the pool ratio, ordered to execute first.
3. When the creator's `ExchangeWithdrawContract` executes, `ExchangeWithdrawActuator` recomputes `anotherTokenQuant` from the now-manipulated ratio (`actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java` lines 218-243), yielding a value worse than X, with no field available to the creator to bound or revert this outcome.

### Citations

**File:** Tron protobuf protocol document.md (L1394-1442)
```markdown
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L142-157)
```java
    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

    if (!accountStore.has(ownerAddress)) {
      throw new ContractValidateException("account[" + readableOwnerAddress + NOT_EXIST_STR);
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);

    if (accountCapsule.getBalance() < calcFee()) {
      throw new ContractValidateException("No enough balance for exchange transaction fee!");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-236)
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

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (newTokenBalance > balanceLimit || newAnotherTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
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
