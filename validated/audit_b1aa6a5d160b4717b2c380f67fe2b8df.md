### Title
`ExchangeInjectActuator` and `ExchangeWithdrawActuator` lack slippage control for the paired-token amount, unlike `ExchangeTransactionActuator` - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
`ExchangeTransactionContract` includes an `expected` field that lets the caller bound the minimum amount of tokens received, protecting against front-running of the AMM-style `ExchangeCapsule` pool. `ExchangeInjectContract` and `ExchangeWithdrawContract`, however, only carry `owner_address`, `exchange_id`, `token_id` and `quant` — there is no field to bound the corresponding "another token" amount that is computed from the *live* pool ratio at execution time. This mirrors exactly the sNOTE `_mintFromAssets()` bug class: an operation whose secondary output/input amount is derived from current pool state with no caller-supplied bound, hard-coded to "accept whatever the pool gives/takes."

### Finding Description
In `ExchangeInjectActuator.doValidate()`/`execute()`, the caller specifies `tokenID` and `tokenQuant` for one side of the pair; `anotherTokenQuant` is computed on the fly from the current `firstTokenBalance`/`secondTokenBalance` ratio: [1](#0-0) 
There is no contract field allowing the exchange creator to cap `anotherTokenQuant` (e.g. a `maxAnotherTokenQuant`), so whatever the pool ratio is *at execution time* is unconditionally accepted, and that amount is debited from the creator's balance: [2](#0-1) 

The protobuf message confirms no bound field exists: [3](#0-2) 

The same pattern exists in `ExchangeWithdrawActuator`, where the amount of the paired token returned to the creator is computed from live reserves with only a "Not precise enough" consistency check, not a caller-chosen minimum: [4](#0-3) [5](#0-4) 

By contrast, `ExchangeTransactionActuator` explicitly validates that the amount out cannot fall below the caller-supplied `expected` bound: [6](#0-5) 

This asymmetry means the "swap" path in java-tron's exchange module was hardened against front-running/slippage, but the "inject liquidity" and "withdraw liquidity" paths were not.

### Impact Explanation
An exchange creator broadcasting `ExchangeInjectContract` to add liquidity at an intended ratio can be front-run by anyone submitting an `ExchangeTransactionContract` (swap) against the same `exchange_id` immediately before the inject transaction is packed. This shifts `firstTokenBalance`/`secondTokenBalance`, so the `anotherTokenQuant` computed and debited from the creator at execution time can be far larger than the ratio the creator intended when constructing the transaction, causing them to spend materially more of the paired asset than expected — an unintended, unrecoverable loss of funds. Symmetrically, front-running an `ExchangeWithdrawContract` can shrink the paired-token amount the creator receives back below their expectation. Both cases satisfy the "theft/permanent freezing of funds via unbacked economic loss" bar since the loss is enforced by consensus-level actuator logic with no way for the signer to cap their exposure.

### Likelihood Explanation
The `exchange_id` used by `ExchangeInjectContract`/`ExchangeWithdrawContract` is public information, and any account can submit an `ExchangeTransactionContract` swap against that same pair in a preceding transaction within the same block or a prior block in the same round, since Exchange pools are not creator-restricted for trading (only inject/withdraw require the creator). This is a straightforward mempool-visible front-run reachable by any account without special privileges, making exploitation practical whenever an exchange creator injects or withdraws liquidity.

### Recommendation
Add explicit slippage-bound fields to `ExchangeInjectContract` and `ExchangeWithdrawContract` (e.g., `expected` / `max_another_quant` for inject, `min_another_quant` for withdraw), and enforce them in `ExchangeInjectActuator.doValidate()` and `ExchangeWithdrawActuator.doValidate()` the same way `ExchangeTransactionActuator` already enforces `tokenExpected` against `anotherTokenQuant`.

### Proof of Concept
1. Exchange creator `C` holds pool `exchange_id=1` with reserves `A:B` and builds `ExchangeInjectContract{token_id=A, quant=Q}` expecting to also spend `~Q*B/A` of token `B` based on current reserves, signs and broadcasts it.
2. Attacker observes `C`'s pending transaction and broadcasts an `ExchangeTransactionContract` swap against the same `exchange_id` with a higher energy/priority fee so it is packed first, shifting the `A:B` ratio unfavorably.
3. When `C`'s `ExchangeInjectContract` is packed, `ExchangeInjectActuator.execute()` recomputes `anotherTokenQuant` from the now-skewed reserves ( [7](#0-6) ), silently debiting far more of token `B` from `C`'s account than intended, with no field in `ExchangeInjectContract` for `C` to have bounded this amount in advance.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L68-99)
```java
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

**File:** Tron protobuf protocol document.md (L1394-1401)
```markdown
      ```java
      message ExchangeInjectContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
      }
      ```
```

**File:** Tron protobuf protocol document.md (L1403-1420)
```markdown
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-243)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
