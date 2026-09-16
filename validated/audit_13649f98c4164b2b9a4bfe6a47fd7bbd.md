Confirmed: `ExchangeInjectActuator` has no `expected`/slippage-bound field for `anotherTokenQuant`, unlike `ExchangeTransactionActuator` which enforces `getExpected()` at [1](#0-0) . This is the closest reachable analog to the HoneyFactory report.

### Title
Missing slippage protection in ExchangeInjectActuator lets pool-ratio changes between broadcast and execution spend more of the counterpart token than the user intended - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java)

### Summary
`ExchangeInjectContract` lets an exchange creator inject `quant` of one token (`tokenID`) into an AMM-style TRX/TRC10 exchange pair; the actuator computes `anotherTokenQuant` — the amount of the *other* token that must simultaneously be pulled from the same account — from the *live* pool ratio at execution time, with no user-supplied bound on that computed value [2](#0-1) . This mirrors the HoneyFactory `mint()` finding: the amount actually charged to the user depends on contract state at execution time rather than at the moment the user signed and broadcast the transaction, and there is no `maxHoneyAmount`-style cap.

### Finding Description
For `ExchangeInjectActuator.execute()`, the user fixes `tokenQuant` for `tokenID`, but `anotherTokenQuant` is derived from `secondTokenBalance * tokenQuant / firstTokenBalance` (or the symmetric ratio) using the exchange pool balances read at execution time [3](#0-2) . Both `tokenQuant` and the freshly computed `anotherTokenQuant` are then deducted from the same account's balance/asset holdings [4](#0-3) .

Between the time the user builds/signs the transaction (observing a given pool ratio) and the time it is actually included and executed, other transactions in the same or preceding blocks — `ExchangeTransactionActuator` trades, or other `ExchangeInjectActuator`/`ExchangeWithdrawActuator` calls — can shift `firstTokenBalance`/`secondTokenBalance`, changing the ratio used to compute `anotherTokenQuant`. The contract exposes no `expected`/`maxAnotherTokenQuant` field to bound this, unlike its sibling `ExchangeTransactionActuator`, which explicitly validates `anotherTokenQuant >= tokenExpected` as slippage protection [1](#0-0) . `ExchangeWithdrawActuator` has the analogous issue for withdrawals, computing `anotherTokenQuant` purely from the live ratio with only a `Not precise enough` deviation check, not a user-supplied cap [5](#0-4) .

The only guard in `ExchangeInjectActuator.validate()` is an `ExchangeBalanceLimit` cap and asset-sufficiency checks against the current, at-validation-time-computed `anotherTokenQuant` — not against a value the user chose — so validation re-derives the same live ratio and offers no real protection against ratio drift between construction and mining [6](#0-5) .

### Impact Explanation
An exchange creator (the only account authorized to call `ExchangeInjectContract`, per the creator check at line 175) can be forced to spend more of the counterpart token (TRX or TRC10 asset) than the ratio implied at the time they built the transaction, because the actual amount pulled is recomputed from pool state at block-execution time. This is a direct, unbounded overspend of the account's own funds triggered purely by ordinary pool activity between broadcast and confirmation — not a hypothetical griefing scenario requiring a malicious actor, since ordinary concurrent trading naturally shifts the ratio.

### Likelihood Explanation
Any active TRX/TRC10 exchange pair with concurrent trading activity will exhibit ratio drift between the time a creator signs an `ExchangeInjectContract` and when it lands on-chain, especially under network congestion or multiple pending transactions targeting the same exchange. No adversarial timing is required beyond normal pool usage, though the affected caller set is limited to the exchange's creator address, and losses are bounded by the account's own asset/TRX balance and the `ExchangeBalanceLimit`.

### Recommendation
Add a caller-supplied bound (e.g., `maxAnotherTokenQuant` or `expected`) to `ExchangeInjectContract`/`ExchangeWithdrawContract`, and validate `anotherTokenQuant <= maxAnotherTokenQuant` (for inject) or `anotherTokenQuant >= minAnotherTokenQuant` (for withdraw) in both `validate()` and `execute()`, mirroring the existing `tokenExpected` slippage check already implemented in `ExchangeTransactionActuator`.

### Proof of Concept
1. Exchange pool has `firstTokenBalance = 1_000_000`, `secondTokenBalance = 1_000_000` (1:1 ratio).
2. Creator builds `ExchangeInjectContract` with `tokenID = first`, `quant = 100_000`, expecting `anotherTokenQuant ≈ 100_000` based on the observed ratio, and signs/broadcasts it.
3. Before this transaction is packed into a block, other users submit `ExchangeTransactionContract` trades against the same pair that shift the ratio to, e.g., 1:5 (`secondTokenBalance` rises to `5_000_000`).
4. When the creator's `ExchangeInjectContract` executes, `ExchangeInjectActuator.execute()` recomputes `anotherTokenQuant = floorDiv(multiplyExact(secondTokenBalance, tokenQuant), firstTokenBalance)` ≈ `500_000` instead of the ~`100_000` the creator expected, and deducts this larger amount from the creator's account with no way to have capped it [7](#0-6) .

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L209-256)
```java
    BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
    BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
    BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
    long newTokenBalance;
    long newAnotherTokenBalance;

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L191-220)
```java
    long tokenQuant = contract.getQuant();

    long anotherTokenQuant;

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }

    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
      throw new ContractValidateException("token is not in exchange");
    }

    if (tokenQuant <= 0) {
      throw new ContractValidateException("withdraw token quant must greater than zero");
    }

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
```
