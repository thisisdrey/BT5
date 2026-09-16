Confirmed: both `ExchangeInjectActuator` and `ExchangeWithdrawActuator` restrict the operation to the exchange's creator account, but that creator is an ordinary, unprivileged account that anyone can become simply by calling `ExchangeCreateContract`. Both actuators lack any user-specified min/max bound field, unlike `ExchangeTransactionContract`, which explicitly has an `expected` field checked at [1](#0-0) .

### Title
Missing slippage/price-bound protection in ExchangeInject/ExchangeWithdraw actuators enables sandwich-style value extraction from liquidity providers - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
The TRX/TRC10 bancor-style exchange (AMM) pool supports three operations: `ExchangeTransactionContract` (trade), `ExchangeInjectContract` (add liquidity) and `ExchangeWithdrawContract` (remove liquidity). Only the trade path lets the caller bound the outcome via the `expected` field [2](#0-1) , which is enforced in `doValidate` at [1](#0-0) . The inject and withdraw contracts carry only `owner_address`, `exchange_id`, `token_id`, and `quant` with no min/max counterpart-amount or ratio bound field, as documented in the protobuf spec [3](#0-2) .

### Finding Description
`ExchangeInjectActuator.execute` computes `anotherTokenQuant` purely from the pool's current on-chain balances at execution time using a straight ratio (`secondTokenBalance * tokenQuant / firstTokenBalance`), with no way for the submitter to constrain the result [4](#0-3) . The validation path (`doValidate`) recomputes the same ratio but never compares it against any caller-supplied bound — there is no `expected`/`minAnotherTokenQuant` field to check [5](#0-4) . The same pattern holds for `ExchangeWithdrawActuator`, whose `doValidate` only checks a fixed 0.01% internal rounding "precision" bound, not a caller-specified acceptable range [6](#0-5) .

Because `ExchangeTransactionContract` can be broadcast by any account at any time and directly mutates `firstTokenBalance`/`secondTokenBalance` via `ExchangeCapsule.setBalance` in `ExchangeTransactionActuator.execute` [7](#0-6) , the pool ratio the creator observed when constructing an inject/withdraw transaction can be altered by an attacker's trade inserted immediately before it is packed into a block. This is functionally analogous to the mai-protocol-v2 `AMM.addLiquidity`/`removeLiquidity` issue: the counterparty amount is "inferred contextually" with no caller-supplied acceptable range or deadline.

### Impact Explanation
A pool creator submitting `ExchangeInjectContract`/`ExchangeWithdrawContract` has no on-chain way to bound how much of the counterpart asset they will actually contribute or receive. An attacker who can front-run the creator's transaction (e.g., by submitting `ExchangeTransactionContract` trades against the same `exchange_id` right before it, then reversing after) can shift the pool ratio to force the creator to inject a disproportionately large amount of the second asset, or withdraw a disproportionately small amount, extracting value from the creator on each such operation.

### Likelihood Explanation
Exploitation requires an attacker to observe a pending `ExchangeInjectContract`/`ExchangeWithdrawContract` in the mempool and race a trade transaction ahead of it — feasible for any node operator or SR that can order transactions, and generally feasible for any actor monitoring the public mempool given java-tron's block-production timing. This does not require any special privilege beyond being able to broadcast a signed transaction on the exchange.

### Recommendation
Add caller-specified bounds to `ExchangeInjectContract` and `ExchangeWithdrawContract` analogous to the `expected` field already present in `ExchangeTransactionContract` (e.g., `expected` minimum for the calculated counterpart amount on inject, and a minimum/maximum acceptable counterpart amount on withdraw), and enforce them in `ExchangeInjectActuator.doValidate`/`ExchangeWithdrawActuator.doValidate` before mutating pool balances.

### Proof of Concept
1. Attacker monitors the mempool for a pool creator's `ExchangeInjectContract` for `exchange_id=X` injecting `tokenQuant` of `firstTokenID`.
2. Attacker broadcasts `ExchangeTransactionContract` trades against exchange `X` that shift `firstTokenBalance`/`secondTokenBalance` far from the ratio the creator expected, timed to land in the block immediately before the inject transaction (see ratio computation at [4](#0-3) ).
3. The creator's inject executes against the now-skewed ratio, computing a much larger `anotherTokenQuant` deduction than intended, with no `expected`/bound check to reject it.
4. Attacker reverses their trade afterward, pocketing the difference extracted from the creator's liquidity contribution.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-96)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L173-175)
```java
    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();
    long tokenExpected = contract.getExpected();
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-230)
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
