Confirmed: `ExchangeInjectContract` and `ExchangeWithdrawContract` lack any `expected`/min-return field, while `ExchangeTransactionContract` explicitly has one (`expected`) that is enforced in `ExchangeTransactionActuator.doValidate()`. [1](#0-0) 

### Title
Exchange Inject/Withdraw lack minimum-return checks, allowing front-run sandwiching of the exchange creator's rebalancing transactions - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeInjectActuator` and `ExchangeWithdrawActuator` compute the counterpart token amount (`anotherTokenQuant`) from the exchange pool's *current* balances at execution time, using the classic AMM ratio formula, but the contracts (`ExchangeInjectContract`, `ExchangeWithdrawContract`) provide no user-supplied minimum/maximum bound to protect against that ratio changing between transaction submission and execution. This is the exact bug class described in the report: a value that depends on pool state calculated at execution time, with no slippage/expected-amount guard. `ExchangeTransactionContract` was hardened against this by adding an `expected` field, checked in `ExchangeTransactionActuator.doValidate()`, but the analogous inject/withdraw paths were not.

### Finding Description
In `ExchangeInjectActuator.execute()`, the creator specifies only `tokenQuant` of one token; the `anotherTokenQuant` they must also supply is derived from the pool's live ratio: [2](#0-1) 
This ratio can be changed by anyone who submits an `ExchangeTransactionContract` trade against the same exchange before the creator's inject transaction is packed into a block — an ordinary, permissionless action reachable via `/wallet/exchangetransaction` or gRPC. [3](#0-2) 

The same issue exists in `ExchangeWithdrawActuator`, where the split between the two withdrawn tokens is likewise computed from the live pool ratio with no minimum-return parameter, only a "not precise enough" self-consistency check unrelated to slippage protection: [4](#0-3) [5](#0-4) 

By contrast, `ExchangeTransactionContract` includes an `expected` field and `ExchangeTransactionActuator.doValidate()` explicitly rejects the trade if the computed output falls below it: [6](#0-5) 
This confirms the developers were aware of, and mitigated, this exact class of issue for `ExchangeTransactionContract`, but never extended the same protection to `ExchangeInjectContract`/`ExchangeWithdrawContract`.

### Impact Explanation
An unprivileged, anonymous transaction broadcaster can submit `ExchangeTransactionContract` trades that shift the pool's first/second token ratio immediately before a pending inject or withdraw transaction executes. This forces the exchange creator to:
- On inject: supply an unexpectedly large amount of the counterpart token (`anotherTokenQuant`) for the same `tokenQuant`, effectively donating value at an adverse, sandwiched price.
- On withdraw: receive an unexpectedly unfavorable split of the two tokens back, worth less than intended.

This is a direct, unbacked-value-transfer/loss-of-funds scenario functionally identical to the reported `LPIssuer.deposit` issue: value is moved based on stale expectations versus actual execution-time pool state, with no on-chain guard to prevent it.

### Likelihood Explanation
Likelihood is high for active/liquid exchanges: any account can submit `ExchangeTransactionContract` transactions and time them (mempool-sandwiching or simply naturally occurring rebalances) around a known pending inject/withdraw transaction, since `ExchangeCapsule.transaction()`, `ExchangeCapsule.setBalance()` are all called on shared, publicly visible mutable pool state. [7](#0-6)  The only restriction is that inject/withdraw callers must be the exchange's creator (validated at line 175-176/181-182 in each actuator), but this does not limit who can move the ratio via ordinary trades, so the sandwich attacker does not need any special privilege.

### Recommendation
Add an `expected` (min-return) or `expected_min`/`expected_max` field to `ExchangeInjectContract` and `ExchangeWithdrawContract`, analogous to `ExchangeTransactionContract.expected`, and enforce it in `ExchangeInjectActuator.doValidate()`/`ExchangeWithdrawActuator.doValidate()` by comparing the freshly computed `anotherTokenQuant` against the caller-supplied bound before allowing execution.

### Proof of Concept
1. Exchange creator constructs an `ExchangeInjectContract` transaction for `tokenQuant` of token A, expecting `anotherTokenQuant` of token B based on the pool ratio observed at construction time (e.g., pool `[1000, 1000]` ⇒ expects `anotherTokenQuant = 1000`).
2. Before the creator's transaction is included, any other account broadcasts an `ExchangeTransactionContract` trade that shifts the pool ratio (e.g., to `[2000, 500]`).
3. The creator's inject transaction executes against the new ratio: `anotherTokenQuant = secondTokenBalance * tokenQuant / firstTokenBalance` now yields a drastically different value (e.g., `250` instead of `1000`, or vice versa depending on direction), silently debiting the creator's account for a different amount than intended, with no validation rejecting the trade. [2](#0-1) 
4. The same sandwich pattern applies to `ExchangeWithdrawActuator`, where the returned token split changes unfavorably without any check the creator can use to bound it. [4](#0-3)

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-99)
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
