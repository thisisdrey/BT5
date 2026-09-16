### Title
ExchangeInject/ExchangeWithdraw lack slippage protection, enabling sandwich attacks via transaction ordering - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
`ExchangeInjectContract` and `ExchangeWithdrawContract` compute the counter-asset amount to be moved based on the *current* pool ratio (`firstTokenBalance`/`secondTokenBalance`) at execution time, and unlike `ExchangeTransactionContract`, they carry no `expected` (minimum-received / maximum-paid) field to bound this computation. [1](#0-0) 

### Finding Description
`ExchangeTransactionContract` includes an `expected` field that is checked against the computed `anotherTokenQuant` in `doValidate`, giving the caller slippage protection against pool-ratio manipulation between signing and execution. [2](#0-1) [3](#0-2) 

By contrast, `ExchangeInjectContract` and `ExchangeWithdrawContract` only carry `token_id`/`quant`, with no bound on the resulting `anotherTokenQuant`. `ExchangeInjectActuator.execute` recomputes `anotherTokenQuant` from whatever `firstTokenBalance`/`secondTokenBalance` exist in the `ExchangeCapsule` at execution time and immediately debits/credits the account with that value, with no post-hoc check that it matches what the signer expected when constructing the transaction. [4](#0-3) 

The same pattern exists in `ExchangeWithdrawActuator.execute`, which mutates the pool balances and moves `anotherTokenQuant` of the paired asset based on the pool ratio read at execution time. [5](#0-4) 

Since `ExchangeTransactionContract` is callable by any unprivileged account and directly mutates the same `ExchangeCapsule.firstTokenBalance`/`secondTokenBalance` via `ExchangeCapsule.transaction`, any attacker can submit ordinary buy/sell transactions immediately before (and after) an exchange creator's `ExchangeInject`/`ExchangeWithdraw` transaction to shift the pool ratio, extract value from the creator's inject/withdraw at a manipulated rate, and then reverse the ratio shift in a back-run — a classic sandwich attack enabled purely by transaction ordering, mirroring the report's core bug class (state read by one operation can be manipulated by intervening transactions with no bound/expectation check). [6](#0-5) 

Both `ExchangeInject` and `ExchangeWithdraw` are restricted to the exchange's creator address (`doValidate` checks `accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())`), so the attack surface is the creator being sandwiched by any other market participant's ordinary trades, not a fully permissionless self-service theft. [7](#0-6) [8](#0-7) 

### Impact Explanation
An exchange creator who submits `ExchangeInjectContract`/`ExchangeWithdrawContract` can have their expected amount of the paired asset silently diverge because a block producer (or any user racing transactions) can place buy/sell (`ExchangeTransactionContract`) transactions immediately before the inject/withdraw executes, moving the pool ratio unfavorably, and then reverse it right after — extracting value from the creator equal to the ratio shift. This constitutes concrete theft of funds (unbacked value transfer between the sandwiching attacker and the exchange creator) without requiring any special privilege beyond ordinary transaction broadcasting.

### Likelihood Explanation
Likelihood is moderate: it requires the attacker to observe a pending `ExchangeInject`/`ExchangeWithdraw` transaction in the mempool (or collude with/be a block producer) and race two transactions around it within the same block or adjacent blocks. This is realistically achievable by anyone monitoring the mempool or by a witness controlling transaction ordering within its produced block, similar to the general "transaction ordering" theme of the source report, but it is limited to the Bancor-style TRC10 `Exchange` feature rather than a broadly-reachable path across the whole protocol.

### Recommendation
Add an `expected`/slippage-bound field (or min/max amount) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, and enforce it in `ExchangeInjectActuator`/`ExchangeWithdrawActuator.execute` (or `doValidate`, re-verified at execute time) analogous to the check already present in `ExchangeTransactionActuator`, so that pool-ratio manipulation between signing and execution causes the transaction to fail rather than silently transferring value.

### Proof of Concept
Not independently verified with a runnable exploit; based on static code review only. Conceptually:
1. Exchange creator signs `ExchangeWithdrawContract` for `exchangeId` expecting `anotherTokenQuant = X` based on the pool ratio at signing time.
2. Attacker observes the pending transaction and submits an `ExchangeTransactionContract` (buy) that shifts `firstTokenBalance`/`secondTokenBalance` unfavorably, ordered immediately before the withdraw in the same block via fee/ordering.
3. `ExchangeWithdrawActuator.execute` recomputes `anotherTokenQuant` from the now-shifted pool state, delivering less value than `X` to the creator, with no field to detect/reject this deviation.
4. Attacker submits a reverse `ExchangeTransactionContract` immediately after to restore the ratio, netting the difference extracted from the creator's withdraw.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-169)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

    long buyTokenQuant = 0;
    long firstTokenBalance = this.exchange.getFirstTokenBalance();
    long secondTokenBalance = this.exchange.getSecondTokenBalance();
    long newFirstTokenBalance;
    long newSecondTokenBalance;

    if (this.exchange.getFirstTokenId().equals(ByteString.copyFrom(sellTokenID))) {
      buyTokenQuant = processor.exchange(firstTokenBalance,
          secondTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(firstTokenBalance, sellTokenQuant)
          : firstTokenBalance + sellTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(secondTokenBalance, buyTokenQuant)
          : secondTokenBalance - buyTokenQuant;

    } else {
      buyTokenQuant = processor.exchange(secondTokenBalance,
          firstTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(firstTokenBalance, buyTokenQuant)
          : firstTokenBalance - buyTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(secondTokenBalance, sellTokenQuant)
          : secondTokenBalance + sellTokenQuant;

    }

    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

    return buyTokenQuant;
  }
```
