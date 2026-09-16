Confirmed: `ExchangeInjectContract` and `ExchangeWithdrawContract` only carry `token_id` and `quant` — there is no `expected`/minimum-output field, unlike `ExchangeTransactionContract`, which explicitly carries an `expected` field [1](#0-0) . This confirms the analog is real and specific to `ExchangeInjectActuator`/`ExchangeWithdrawActuator`.

### Title
Missing slippage/minimum-output protection in `ExchangeWithdrawActuator` and `ExchangeInjectActuator` lets bancor-pool liquidity operations execute at an unexpectedly worse ratio - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
The Reserve Protocol finding points out that `StaticATokenLM._withdraw` converts an amount using the live exchange rate with no ability for the caller to bound the acceptable output, so timing/ordering of the transaction can silently change how much the user actually receives. Java-tron's Bancor-style TRC10 `Exchange` mechanism has the equivalent gap: `ExchangeTransactionContract` (a trade) has an explicit `expected` field that is validated against the computed counter-amount, but the two other operations that move value based on the same live pool ratio — withdraw and inject — carry no such field.

### Finding Description
`ExchangeWithdrawContract` and `ExchangeInjectContract` only carry `token_id` and `quant`; there is no minimum/maximum bound field [2](#0-1) . By contrast, `ExchangeTransactionContract` carries an `expected` field which is enforced in `ExchangeTransactionActuator.doValidate`, rejecting the transaction if the computed `anotherTokenQuant` is less than the caller's expectation [3](#0-2) .

In `ExchangeWithdrawActuator`, the amount of the "another token" returned to the caller is derived purely from the current on-chain pool ratio (`firstTokenBalance`/`secondTokenBalance`) at the moment of execution, with no user-supplied bound to reject an unfavorable outcome [4](#0-3) . The only checks in `doValidate` are that the exchange balances are non-zero and that the computed integer ratio is "precise enough" — there is no check against a caller-supplied minimum acceptable `anotherTokenQuant` [5](#0-4) .

Similarly, `ExchangeInjectActuator` computes and force-debits `anotherTokenQuant` (the paired token amount required to add liquidity at the current ratio) without allowing the caller to cap that amount [6](#0-5) ; validation only checks balances and bounds, not a caller-supplied max [7](#0-6) .

Because both `Withdraw` and `Inject` transactions are ordinary broadcastable transactions processed in block/tx order, and the exchange pool ratio can shift between the time a user signs/broadcasts the transaction and when it is packed into a block (e.g. due to other trade transactions front-running/back-running it, or being reordered by an SR), the amount actually withdrawn or the amount actually required to be paid on inject can differ materially from what the caller expected when signing — with no on-chain mechanism to abort if the ratio has moved unfavorably.

### Impact Explanation
An exchange creator withdrawing liquidity can receive a materially different (lower) amount of the "another token" than intended if the pool ratio shifts before execution, and there's no way to bound this loss on-chain. Symmetrically, on `Inject`, the actual paired-token cost debited from the account can be higher than the creator anticipated. This is a direct, protocol-level value-transfer defect analogous to the reported StaticATokenLM issue, though the trade path (`ExchangeTransactionActuator`) already has the correct protection via `expected`. This is scoped to Medium since it affects fund amounts received/paid rather than causing fund loss beyond the exchange's own liquidity, and only exchange creators (who can freely create their own exchange pools) are exposed for withdraw/inject.

### Likelihood Explanation
Any user can call `ExchangeCreateContract` to become a pool creator, then call `ExchangeInjectContract`/`ExchangeWithdrawContract` themselves, or interact with an existing pool's inject/withdraw if they are its creator. Given TRC10 exchanges are actively tradable by anyone via `ExchangeTransactionContract` in the same block window, an adversarial or even accidental trade sequence between signing and packing can shift the ratio, making the scenario realistically triggerable without any special privilege.

### Recommendation
Add an `expected`/minimum-output field to `ExchangeWithdrawContract` (minimum acceptable `anotherTokenQuant`) and a maximum-cost field to `ExchangeInjectContract` (maximum acceptable `anotherTokenQuant`), and enforce these bounds in `ExchangeWithdrawActuator.doValidate`/`ExchangeInjectActuator.doValidate`, mirroring the existing `expected` check already present in `ExchangeTransactionActuator.doValidate` [3](#0-2) .

### Proof of Concept
1. User A creates an exchange pool via `ExchangeCreateContract`, becoming its creator.
2. User A signs an `ExchangeWithdrawContract` withdrawing `tokenQuant` of the first token, expecting `anotherTokenQuant` computed at the current pool ratio (e.g. 2:1) [8](#0-7) .
3. Before this transaction is packed, another party (or User A themselves in a race) submits `ExchangeTransactionContract` trades against the same pool, shifting the ratio (e.g. to 3:1).
4. When the withdraw executes, `anotherTokenQuant` is recomputed from the now-shifted ratio and paid out — lower than User A originally expected — with no validation to reject this outcome, since no `expected`-style field exists on `ExchangeWithdrawContract` [9](#0-8) .

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-251)
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

```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L65-83)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-256)
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

    if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(tokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(tokenID, tokenQuant, dynamicStore)) {
        throw new ContractValidateException("token balance is not enough");
      }
    }

    if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(anotherTokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(anotherTokenID, anotherTokenQuant, dynamicStore)) {
        throw new ContractValidateException("another token balance is not enough");
      }
    }
```
