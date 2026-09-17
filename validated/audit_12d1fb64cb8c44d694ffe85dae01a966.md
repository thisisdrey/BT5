Confirmed: `ExchangeInjectContract` and `ExchangeWithdrawContract` protobuf messages have no `expected` field, unlike `ExchangeTransactionContract` which does [1](#0-0) . This confirms the analog vulnerability.

### Title
Missing slippage protection in `ExchangeInjectActuator` and `ExchangeWithdrawActuator` allows front-running liquidity providers - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeTransactionContract` (bancor-style TRC10 swap) correctly implements slippage protection via an `expected` field that is validated against the computed output before executing the trade [2](#0-1) . However, the sibling actuators `ExchangeInjectActuator` (add liquidity) and `ExchangeWithdrawActuator` (remove liquidity, creator-only) compute the `anotherTokenQuant` counter-asset amount purely from the exchange pool's current on-chain ratio at execution time, with no user-supplied minimum/maximum bound to protect against ratio changes between signing and inclusion.

### Finding Description
In `ExchangeInjectActuator.doValidate()`, the paired asset amount to be pulled from the account is derived directly from the live pool ratio: `anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance)` (or the symmetric branch) [3](#0-2) . The only check performed is `anotherTokenQuant <= 0`, with no way for the caller to specify an upper bound they're willing to pay [4](#0-3) .

Similarly, `ExchangeWithdrawActuator.doValidate()` computes the counter-asset amount the withdrawer will receive from the live ratio, with no minimum-output bound the withdrawer can enforce [5](#0-4) .

The underlying `ExchangeInjectContract` and `ExchangeWithdrawContract` protobuf messages simply lack an `expected`-style field entirely, unlike `ExchangeTransactionContract` [1](#0-0) . Because any unprivileged account can broadcast an `ExchangeTransactionContract` against the same pool (`ExchangeTransactionActuator.execute`, reachable via `AbstractExchangeActuator`) and shift `firstTokenBalance`/`secondTokenBalance` before an inject/withdraw transaction is packed into a block, an attacker can front-run a pending `ExchangeInjectContract` or `ExchangeWithdrawContract` transaction to change the effective exchange rate the victim gets.

### Impact Explanation
- For `ExchangeInjectActuator`: an attacker can trade against the pool immediately before the victim's inject transaction executes, so the victim is forced to contribute a disadvantageous ratio of `token`/`anotherToken`, receiving less economic value out of the deposited pair than intended (loss of funds for the liquidity provider) [6](#0-5) .
- For `ExchangeWithdrawActuator`: because withdrawal is restricted to the exchange's creator account (`accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())`), the exploitation path is that any third party can front-run the creator's withdraw with an `ExchangeTransactionContract` trade to manipulate the ratio and cause the creator to receive fewer of the counter asset than expected when the withdrawal was submitted [7](#0-6) .

This is loss of funds for the transaction sender (either the liquidity depositor or the exchange creator), matching the "theft or permanent freezing of funds" impact bar, since java-tron's block producers (or any observer relaying to a producer) can order transactions to extract value from unaware exchange participants.

### Likelihood Explanation
Both `ExchangeInjectContract` and `ExchangeWithdrawContract` are ordinary broadcastable transaction types processed by unprivileged actuators reachable from any signed transaction; no special privilege is required to submit the front-running `ExchangeTransactionContract`. The precondition is simply that a target exchange pool with meaningful liquidity/quant sizes exists and that the attacker can observe pending inject/withdraw transactions in the mempool (standard MEV/front-running assumption), making this readily exploitable whenever TRC10 bancor exchanges are actively used.

### Recommendation
Add an `expected`-style bound to `ExchangeInjectContract` (e.g., `max_another_token_quant` — a cap on what the caller is willing to contribute) and to `ExchangeWithdrawContract` (e.g., `min_another_token_quant` — a floor on what the caller expects to receive), mirroring the pattern already used in `ExchangeTransactionContract.expected` and validated in `ExchangeTransactionActuator.doValidate()` (`anotherTokenQuant < tokenExpected` check) [2](#0-1) . Then add the corresponding bound checks in `ExchangeInjectActuator.doValidate()` and `ExchangeWithdrawActuator.doValidate()` before mutating pool state.

### Proof of Concept
1. Attacker observes a pending `ExchangeWithdrawContract` from the exchange creator in the mempool, requesting `tokenQuant` of `firstTokenID`, expecting roughly `anotherTokenQuant` of `secondTokenID` based on the current pool ratio.
2. Attacker submits (and gets included first, e.g., via higher energy/bandwidth priority or same-block ordering) an `ExchangeTransactionContract` that sells a large amount of `secondTokenID` into the pool, shifting `firstTokenBalance`/`secondTokenBalance` unfavorably for a subsequent withdrawal of `firstTokenID` [8](#0-7) .
3. The victim's `ExchangeWithdrawActuator.execute` then computes `anotherTokenQuant` from the now-manipulated pool balances via the same ratio formula [9](#0-8) , so the victim receives a materially different (lower) amount of `secondTokenID` than expected when they signed the transaction, with no on-chain check to reject the trade — because no `expected`/minimum field exists to enforce it.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-75)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L38-103)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    ExchangeStore exchangeStore = chainBaseManager.getExchangeStore();
    ExchangeV2Store exchangeV2Store = chainBaseManager.getExchangeV2Store();
    AssetIssueStore assetIssueStore = chainBaseManager.getAssetIssueStore();
    try {
      final ExchangeInjectContract exchangeInjectContract = this.any
          .unpack(ExchangeInjectContract.class);
      AccountCapsule accountCapsule = accountStore
          .get(exchangeInjectContract.getOwnerAddress().toByteArray());

      ExchangeCapsule exchangeCapsule;
      exchangeCapsule = Commons.getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeInjectContract.getExchangeId()));
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

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
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L229-231)
```java
    if (anotherTokenQuant <= 0) {
      throw new ContractValidateException("the calculated token quant  must be greater than 0");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L77-89)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L217-227)
```java
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
```
