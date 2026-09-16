## Title
Unchecked return value of `addAssetAmountV2` in `ExchangeWithdrawActuator` can permanently freeze TRC10 tokens - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
The external report describes tokens like `BNB` getting permanently stuck in `BufferBinaryPool` because the contract calls `transfer`/`transferFrom` and does not properly check the return value, so a failed transfer is silently treated as successful and the accounting state diverges from actual token custody. The analogous bug class in java-tron is a value-mutating balance operation whose boolean success/failure return value is discarded, letting the ledger's internal accounting diverge from the actual balances, which permanently strands value.

`ExchangeWithdrawActuator.execute()` calls `AccountCapsule.addAssetAmountV2(...)` to credit a withdrawing account with TRC10 tokens taken out of the exchange pool, but ignores the boolean return value of that call, exactly the same "unchecked-transfer-success" pattern as the reported issue.

### Finding Description
In `execute()`, the exchange's pooled balances are unconditionally decremented via `exchangeCapsule.setBalance(...)` [1](#0-0) , and then the withdrawn tokens are credited to the caller's account with: [2](#0-1) 

Both calls to `accountCapsule.addAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore)` and `accountCapsule.addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore)` are boolean-returning methods (confirmed by their `public boolean addAssetAmountV2(...)` signatures in `chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java`), but neither call result is checked. If the addition would overflow the account's existing asset balance (`Long.MAX_VALUE` boundary) or otherwise fails internally, `addAssetAmountV2` returns `false` without throwing, and the method silently no-ops.

Critically, `doValidate()` in this actuator performs no pre-check that crediting `tokenQuant`/`anotherTokenQuant` into the owner account's *existing* asset balance won't overflow — it only validates exchange-pool arithmetic (`bigFirstTokenBalance`, `bigSecondTokenBalance`, precision) [3](#0-2) . Contrast this with `TransferAssetActuator.validate()`, which explicitly guards the receiver-side overflow with `addExact(assetBalance, amount)` before committing [4](#0-3)  — that overflow protection is absent for the withdrawing account in `ExchangeWithdrawActuator`.

As a result, the exchange pool's balance is already decremented (irreversibly, since `Commons.putExchangeCapsule` persists the reduced state right after) while the corresponding credit to the account can silently fail, causing the withdrawn tokens to vanish from both the exchange and the account.

### Impact Explanation
This causes a concrete, permanent loss/freezing of TRC10 asset balances: value is debited from the `ExchangeCapsule` pool but never lands in any account, with no error surfaced to the caller (the transaction still returns `SUCESS`). This matches the "permanent freezing of funds" impact bar for this analog class, driven by the same root cause pattern as the source report — a value-transfer success/failure signal being ignored.

### Likelihood Explanation
Reachable by any unprivileged account that is the creator of an `Exchange` and calls `ExchangeWithdrawContract` via a signed transaction — no special privilege required beyond owning the exchange pair, and any user can create an exchange pair themselves via `ExchangeCreateContract`. Triggering the overflow requires the withdrawing account to already hold a very large TRC10 balance close to `Long.MAX_VALUE` for the specific token id, which is achievable by the attacker pre-loading their own account with large asset balances they issued/acquired, then triggering a withdraw that pushes the addition past the overflow boundary.

### Recommendation
Check the boolean return values of both `addAssetAmountV2` calls in `ExchangeWithdrawActuator.execute()` and throw a `ContractExeException` (rolling back the exchange balance mutation) if either fails, mirroring the pattern already used for `reduceAssetAmountV2` in `TransferAssetActuator`. Additionally, add an explicit overflow pre-check in `doValidate()` for the receiving account's post-credit asset balance, analogous to the `addExact` check performed in `TransferAssetActuator.validate()`.

### Proof of Concept
1. Attacker account `A` acquires/issues a TRC10 token `T` and accumulates an asset balance for `T` close to `Long.MAX_VALUE`.
2. `A` creates an `Exchange` pairing `T` with TRX (or another token) via `ExchangeCreateContract`, seeding the pool with some `T` and TRX.
3. `A` sends `ExchangeWithdrawContract` requesting withdrawal of `T` (or the paired token) in an amount such that `A`'s existing `T` balance plus the withdrawn `tokenQuant`/`anotherTokenQuant` overflows `Long`.
4. `execute()` unconditionally reduces the `ExchangeCapsule`'s pooled balance [5](#0-4)  and then calls the unchecked `addAssetAmountV2` [2](#0-1) , which silently fails to credit `A`'s account due to overflow.
5. Transaction still commits with `code.SUCESS`; the withdrawn tokens are gone from the exchange pool and never appear in `A`'s (or anyone's) account balance — permanently frozen/lost.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L93-104)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L209-272)
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

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L177-185)
```java
      assetBalance = toAccount.getAsset(dynamicStore, ByteArray.toStr(assetName));
      if (assetBalance != null) {
        try {
          assetBalance = addExact(assetBalance, amount); //check if overflow
        } catch (Exception e) {
          logger.debug(e.getMessage(), e);
          throw new ContractValidateException(e.getMessage());
        }
      }
```
