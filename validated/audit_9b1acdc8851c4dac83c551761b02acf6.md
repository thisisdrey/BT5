### Title
Exchange pool sandwich attack via ExchangeInject/ExchangeWithdraw around victim ExchangeTransaction - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java], [File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java], [File: actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java])

### Summary
Any ordinary account can create a Bancor-style token exchange pool (`ExchangeCreateContract`) and remain its creator. The creator alone is authorized to call `ExchangeInjectContract`/`ExchangeWithdrawContract`, which instantly reprice the pool by directly rewriting `firstTokenBalance`/`secondTokenBalance` with no time delay, fee, or price-impact limit. Because a victim's `ExchangeTransactionContract` only checks a static minimum-received bound (`expected`), the creator can broadcast an `Inject` before the victim's trade and a `Withdraw` right after it within the same block, moving the price against the victim (but staying within their `expected` tolerance) and then reversing it, extracting the price-impact spread as profit — a textbook sandwich attack, directly analogous to the `buydPNM`/`sellDPNM` slippage issue in the report.

### Finding Description
`ExchangeTransactionActuator.execute()` converts tokens using the constant-product/Bancor-like curve in `ExchangeCapsule.transaction()`, which delegates to `ExchangeProcessor.exchange()` / `SafeExchangeProcessor.exchange()`. [1](#0-0) 
The only protection against price movement is the `expected` field validated in `doValidate()`: [2](#0-1) 
This is exactly the "minAmountReceived" mitigation the external report recommends for `dpnm_sc.sol`, but a minimum-received check alone does not prevent a sandwich — it only bounds the victim's worst-case loss while still allowing the attacker to extract the price-impact spread.

The pool balances that determine the exchange rate are freely and instantly adjustable by the pool creator via `ExchangeInjectActuator`/`ExchangeWithdrawActuator`, which recompute `anotherTokenQuant` from the current ratio and directly call `exchangeCapsule.setBalance(...)`: [3](#0-2) [4](#0-3) 
Both actuators require only that the caller be the exchange's creator, which is a normal, unprivileged account (no SR/witness/committee privileges needed) — created previously by that same account via `ExchangeCreateContract`: [5](#0-4) [6](#0-5) 

Attack flow, all as ordinary signed transactions from a single unprivileged account (the pool creator), packed in front of and behind a victim's trade within the same block:
1. Creator sends `ExchangeInjectContract` (or `ExchangeWithdrawContract`) to skew `firstTokenBalance`/`secondTokenBalance` unfavorably for the upcoming victim trade direction.
2. Victim's `ExchangeTransactionContract` executes against the skewed pool via `ExchangeCapsule.transaction()`, receiving less than they would have at the pre-manipulation price (bounded only by their own `expected` value, which they set based on the unmanipulated price they observed).
3. Creator immediately reverses the skew with a matching `ExchangeWithdrawContract`/`ExchangeInjectContract`, restoring the pool and pocketing the spread extracted from the victim.

### Impact Explanation
This allows an unprivileged transaction broadcaster (any pool creator) to systematically extract value from counterparties trading on their exchange, i.e., concrete theft of funds from victims interacting with the `Exchange`/`ExchangeV2` market feature — satisfying the "unauthorized account operation / theft of funds" impact bar. No consensus-level bug, crash, or key disclosure is involved, but real TRX/TRC10 value is transferred from victim to attacker beyond what fair-market execution would produce.

### Likelihood Explanation
Likelihood is high for any user who trusts a third-party-created exchange pool and only relies on the `expected` slippage bound: creating an exchange, injecting/withdrawing liquidity, and executing exchange transactions are all standard, permissionless, low-fee operations (`calcFee()` returns 0 for `ExchangeTransactionActuator`/uses the constant `getExchangeCreateFee`/`getMarketSellFee`-style fees for Inject/Withdraw). An attacker merely needs to control both the pool and get their bracketing transactions ordered around the victim's transaction within the same block, which is achievable by any broadcaster submitting transactions with appropriate timing.

### Recommendation
- Restrict how much `ExchangeInject`/`ExchangeWithdraw` can move the pool ratio within a short time window (e.g., per-block or time-locked rate limits), or require a cooldown between liquidity changes and trades on the same pool.
- Consider using a time-weighted or oracle-anchored price for `expected` validation in `ExchangeTransactionActuator` instead of relying purely on the caller-supplied static minimum, or emit/require a maximum allowable price deviation from the price observed at broadcast time.
- Alternatively, disallow the pool creator from injecting/withdrawing liquidity in the same block as third-party trades against that pool.

### Proof of Concept
Conceptual sequence (mirrors the acknowledged `dpnm_sc.sol` sandwich, but here fully permissionless and reachable through the actuator layer):
1. Attacker A creates exchange pool P via `ExchangeCreateContract` (A is `creatorAddress`).
2. Victim V observes pool P's price and sends `ExchangeTransactionContract` with `expected` computed from that price (protecting against catastrophic slippage only).
3. Immediately before V's transaction is packed, A sends `ExchangeWithdrawContract` (or `ExchangeInjectContract`) skewing `firstTokenBalance`/`secondTokenBalance` to worsen V's execution price while staying above V's `expected`.
4. V's `ExchangeTransactionContract` executes at the skewed price via `ExchangeCapsule.transaction()` — see [7](#0-6) .
5. Immediately after, A sends the reverse `ExchangeInjectContract`/`ExchangeWithdrawContract`, restoring the pool ratio and realizing the extracted spread in A's own balances (both legs update `accountCapsule` balances directly per [8](#0-7) ).

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-69)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L85-99)
```java
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
