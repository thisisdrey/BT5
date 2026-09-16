### Title
Missing Slippage Protection in ExchangeInject/ExchangeWithdraw Actuators - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java], [File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java])

### Summary
`ExchangeInjectActuator` and `ExchangeWithdrawActuator` compute the counter-asset amount (`anotherTokenQuant`) purely from the exchange pool's live balances at execution time, with no user-supplied minimum/maximum bound to protect against pool-ratio manipulation between transaction submission and block inclusion. This is the same root-cause class as the reported Curve `remove_liquidity_one_coin(..., 0)` issue: a liquidity operation is executed without any slippage bound, so the counterparty amount can be forced arbitrarily unfavorable by manipulating pool state right before execution.

### Finding Description
`ExchangeTransactionContract`, used for trading against the pool, has a dedicated `expected` field and the actuator explicitly enforces it: [1](#0-0) 

In contrast, `ExchangeInjectContract` and `ExchangeWithdrawContract` carry no analogous `expected`/min/max field: [2](#0-1) 

`ExchangeInjectActuator.execute` recomputes `anotherTokenQuant` from the exchange's current balances at execution time and immediately debits that exact amount from the caller's account with no floor/ceiling check against what the caller intended when they built the transaction: [3](#0-2) 

Likewise, `ExchangeWithdrawActuator.execute` recomputes `anotherTokenQuant` from live pool balances and credits that (possibly manipulated) amount back to the caller: [4](#0-3) 

The `doValidate()` methods of both actuators only check that balances are sufficient and that the pool isn't empty; there is no check bounding `anotherTokenQuant` against a value the caller expected: [5](#0-4) [6](#0-5) 

Reachability: any unprivileged account can become an exchange "creator" simply by broadcasting an `ExchangeCreateContract` transaction (no special permission required beyond paying the create fee and holding the token balances), and only the creator address is permitted to call Inject/Withdraw: [7](#0-6) [8](#0-7) [9](#0-8) 

Any other unprivileged account can broadcast an `ExchangeTransactionContract` against the same exchange pair (no special permission needed, only sufficient token/TRX balance) to shift `firstTokenBalance`/`secondTokenBalance` immediately before the creator's Inject/Withdraw transaction lands in the same block, changing the exchange ratio used by the Inject/Withdraw computation.

### Impact Explanation
Since `anotherTokenQuant` is derived from the live, attacker-manipulable pool ratio with no user-enforced bound:
- On `ExchangeWithdraw`, an attacker can skew the ratio so the creator receives far less of the counter-asset than the pool state implied when the transaction was built — a direct, unrecoverable loss of funds for the creator.
- On `ExchangeInject`, an attacker can skew the ratio so the creator is forced to debit far more of the counter-asset than intended to complete the deposit — again an unrecoverable loss of funds.

This matches "theft or permanent freezing of funds" for the affected account, since the debited/credited amounts are final once the transaction executes and there is no recourse (`ret.setExchangeInjectAnotherAmount` / `ret.setExchangeWithdrawAnotherAmount` are set and balances committed unconditionally on success).

### Likelihood Explanation
Likelihood is moderate-to-high in a scenario where the exchange creator performs routine liquidity management (inject/withdraw). It requires an attacker (any unprivileged account) to observe the creator's pending transaction in the mempool and race an `ExchangeTransactionContract` call into the same or earlier block — a standard front-running/sandwich pattern that java-tron's public mempool broadcasting allows, analogous to a public AMM without slippage bounds.

### Recommendation
Add an `expected`-style field (e.g., `expected_another_amount` with min/max semantics as appropriate) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, and enforce it in `ExchangeInjectActuator.doValidate()` / `ExchangeWithdrawActuator.doValidate()` the same way `ExchangeTransactionActuator` enforces `tokenExpected` against `anotherTokenQuant`, rejecting the transaction if the computed counter-asset amount falls outside the caller-specified bound.

### Proof of Concept
1. Attacker observes creator C's pending `ExchangeWithdrawContract` transaction (exchange id `E`, `tokenId = A`, `quant = Q`) in the mempool, computed at submission time to yield `anotherTokenQuant ≈ X` based on current pool ratio.
2. Attacker submits and gets included first an `ExchangeTransactionContract` trading a large amount of token B into the same exchange `E`, shifting `firstTokenBalance`/`secondTokenBalance` significantly.
3. C's `ExchangeWithdrawContract` executes next in the same block, and `ExchangeWithdrawActuator.execute` recomputes `anotherTokenQuant` from the now-skewed balances: [10](#0-9) 
4. C receives a materially different (attacker-controlled) `anotherTokenQuant` than expected when the transaction was built, with no on-chain check to reject the unfavorable outcome — resulting in loss of funds for C. The identical pattern applies to `ExchangeInjectActuator`, where C is forced to debit more of the counter-asset than intended.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L68-104)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L218-272)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L145-182)
```java
  private boolean doValidate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (!this.any.is(ExchangeCreateContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [ExchangeCreateContract],real type[" + any
              .getClass() + "]");
    }
    final ExchangeCreateContract contract;
    try {
      contract = this.any.unpack(ExchangeCreateContract.class);
    } catch (InvalidProtocolBufferException e) {
      throw new ContractValidateException(e.getMessage());
    }

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
      throw new ContractValidateException("No enough balance for exchange create fee!");
    }

```
