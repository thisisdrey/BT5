### Title
Missing slippage protection on ExchangeInject/ExchangeWithdraw allows sandwich attacks on liquidity providers - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
Both `ExchangeInjectActuator` and `ExchangeWithdrawActuator`, which let the pool creator add/remove liquidity to a TRX/TRC10 `Exchange` bonding-curve pair, compute the counter-asset amount from the *current* on-chain pool ratio at execution time, with no user-supplied minimum/maximum bound. This mirrors the reported `sellCredits()` slippage class: a pending transaction can be sandwiched by another trade that shifts the pool ratio before it executes, causing the victim to give up (inject) or receive (withdraw) an unintended amount of the counter asset.

### Finding Description
`ExchangeInjectActuator.doValidate()`/`execute()` computes `anotherTokenQuant` as a function of `firstTokenBalance`/`secondTokenBalance` read from the `ExchangeCapsule` at execution time: [1](#0-0) 
and both `tokenID` and the derived `anotherTokenID` amounts are deducted from the caller's balance in `execute()`: [2](#0-1) 

There is no parameter analogous to `expected` (as exists on `ExchangeTransactionContract`) bounding `anotherTokenQuant`; validation only checks it is `> 0`: [3](#0-2) 

Similarly, `ExchangeWithdrawActuator` computes the counter-asset amount to return to the withdrawer from the live pool ratio with no expected/min bound: [4](#0-3) 

By contrast, `ExchangeTransactionActuator` (the buy/sell path) already includes an `expected` field in `ExchangeTransactionContract` and enforces it in validation, exactly matching the report's recommendation: [5](#0-4) 

Because `ExchangeTransactionContract` allows any address to trade against the pool (not just the creator), an attacker can submit a trade transaction (via `wallet/exchangetransaction`) immediately before a pending inject/withdraw transaction from the pool creator to shift `firstTokenBalance`/`secondTokenBalance`, then reverse the trade after, sandwiching the creator's inject/withdraw and causing them to deposit more or withdraw less of the counter-asset than intended.

### Impact Explanation
This affects only the exchange creator's inject/withdraw operations (per `doValidate()`, only the creator may call these), so the actionable victim set is narrower than the original report, but the loss is real and quantifiable: an attacker with two ordinary signed transactions (front-run trade, back-run trade) can force an unbacked/mispriced transfer of TRX or TRC10 tokens out of the pool creator's account during inject, or force a smaller-than-expected payout during withdraw — a concrete transfer-of-value bug reachable purely through the actuator/validate-execute path, matching the "unauthorized ... theft ... of funds" acceptance bar.

### Likelihood Explanation
Likelihood is medium: it requires an attacker to observe a pending `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction in the mempool and race two of their own `ExchangeTransactionContract` transactions around it, and it is only exploitable against the specific `Exchange` pool's creator (a bounded set of accounts). This is the same generic mempool-visibility precondition as the original report's `sellCredits()` finding.

### Recommendation
Add a `expected_another_token_quant` (min-out for inject, min-received for withdraw) or symmetric `max_another_token_quant` field to `ExchangeInjectContract`/`ExchangeWithdrawContract`, and enforce it in `ExchangeInjectActuator.doValidate()`/`ExchangeWithdrawActuator.doValidate()` the same way `ExchangeTransactionActuator` already checks `anotherTokenQuant < tokenExpected` (line 219 of `ExchangeTransactionActuator.java`).

### Proof of Concept
1. Pool creator submits `ExchangeInjectContract` intending to inject `tokenQuant` of tokenA expecting to also spend `X` of tokenB, based on the current pool ratio.
2. Before this transaction is mined, attacker submits `ExchangeTransactionContract` selling a large amount of tokenA into the pool, shifting the ratio so tokenB is now relatively cheaper/more expensive.
3. The creator's inject transaction executes using the now-skewed ratio in `ExchangeInjectActuator.execute()` (lines 71-83), causing `anotherTokenQuant` to differ substantially from `X`, deducting more (or less) tokenB from the creator's account than intended.
4. Attacker reverses their trade with a second `ExchangeTransactionContract`, restoring the ratio and pocketing the value lost by the creator due to the un-bounded conversion. The same pattern applies to `ExchangeWithdrawActuator`, where the creator can receive less of the counter-asset than expected.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L88-99)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L228-230)
```java

    if (anotherTokenQuant <= 0) {
      throw new ContractValidateException("the calculated token quant  must be greater than 0");
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L209-222)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }

    BigDecimal bigFirstTokenBalance = new BigDecimal(String.valueOf(firstTokenBalance));
    BigDecimal bigSecondTokenBalance = new BigDecimal(String.valueOf(secondTokenBalance));
    BigDecimal bigTokenQuant = new BigDecimal(String.valueOf(tokenQuant));
    final boolean allowHarden = allowHarden();
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
