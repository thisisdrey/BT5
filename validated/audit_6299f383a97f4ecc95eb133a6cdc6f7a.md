### Title
Unchecked `AssetIssueStore` lookup in `ExchangeCreateActuator.execute()` can throw an uncaught `NullPointerException` - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java)

### Summary
`ExchangeCreateActuator` (reachable by any account broadcasting an `ExchangeCreateContract` transaction) does not validate, in `validate()`, that `firstTokenID`/`secondTokenID` correspond to an existing entry in `AssetIssueStore` when `AllowSameTokenName == 0`. `execute()` later dereferences that lookup directly, mirroring the reported pattern of casting/using an unvalidated identifier and assuming it references a valid, existing object.

### Finding Description
In `doValidate()`, the token-id checks are gated behind `dynamicStore.getAllowSameTokenName() == 1`: [1](#0-0) 
When same-token-name is closed (`== 0`), there is no explicit check that `firstTokenID`/`secondTokenID` exist in `AssetIssueStore`; the code only relies on `assetBalanceEnoughV2`, which checks the *owner's* recorded asset balance map rather than confirming the token id itself resolves to a live `AssetIssueCapsule`: [2](#0-1) 
In `execute()`, when `AllowSameTokenName == 0`, the code performs an unguarded lookup and immediately calls `.getId()` on the result without a null check: [3](#0-2) 
If `assetIssueStore.get(firstTokenID)` (or `secondTokenID`) returns `null`, this throws a `NullPointerException`, which is not among the caught exception types in `execute()` (`BalanceInsufficientException | InvalidProtocolBufferException | ArithmeticException`): [4](#0-3) 
This exactly parallels the reported bug class: an externally supplied identifier (there, a boosted-position address; here, an asset token id) is cast/dereferenced against an expected interface/object without first validating its existence, leading to a runtime failure.

### Impact Explanation
An uncaught `NullPointerException` thrown from an actuator's `execute()` during block application is not handled by the actuator's own catch clauses and propagates upward through transaction processing in `Manager`. Depending on how the caller (block application path) handles unexpected runtime exceptions from actuators, this can interrupt processing of the block for that node, a potential node halt/crash condition for any node that must re-apply the same transaction (i.e., a chain-halting, universally-reproducible fault triggered by a single crafted transaction), rather than a normal `ContractValidateException`/`ContractExeException` failure path that would just fail the transaction gracefully.

### Likelihood Explanation
I was not able to fully confirm, within the available index, whether `AccountCapsule.assetBalanceEnoughV2` (in `chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java`) can return `true`/report a positive balance for a `firstTokenID`/`secondTokenID` that no longer resolves in `AssetIssueStore` (e.g., through asset-map bookkeeping inconsistencies across the V1/V2 asset store migration, or through an account's asset map being populated by paths other than a direct `AssetIssueStore` reference). Because of index size limits, I could not read the full body of `assetBalanceEnoughV2`/`reduceAssetAmountV2` to conclusively prove a normal-account path that reaches `assetIssueStore.get(tokenId) == null` in `execute()`. This uncertainty means the likelihood of an unprivileged transaction broadcaster reliably triggering the null path is unconfirmed and should be verified by a deeper code walk (including the account asset map invariants) before treating this as a fully proven exploitable path.

### Recommendation
- In `ExchangeCreateActuator.doValidate()`, unconditionally verify that `firstTokenID` and `secondTokenID` (when not `TRX_SYMBOL_BYTES`) resolve to a non-null `AssetIssueCapsule` via `AssetIssueStore`/`Commons.getAssetIssueStoreFinal`, regardless of `AllowSameTokenName`, mirroring the pattern already used in `MarketSellAssetActuator.validate()`: [5](#0-4) 
- In `ExchangeCreateActuator.execute()`, add a null check before calling `.getId()` on the `assetIssueStore.get(...)` result and fail safely (e.g., via a checked exception already caught in `execute()`), rather than allowing an NPE to escape uncaught.

### Proof of Concept
Conceptual (not fully verified against `assetBalanceEnoughV2` internals due to index limitations):
1. Attacker broadcasts an `ExchangeCreateContract` with `AllowSameTokenName == 0`, `firstTokenId` set to a byte value that is not `TRX_SYMBOL_BYTES` but does not correspond to any real `AssetIssueCapsule` in `AssetIssueStore`.
2. If the attacker's account asset map nonetheless reports a positive balance for that `firstTokenId` (path not fully confirmed here), `doValidate()` passes because `assetBalanceEnoughV2` only inspects the account's own asset ledger, not `AssetIssueStore` existence.
3. `execute()` reaches `assetIssueStore.get(firstTokenID).getId()` at line 95, and since the lookup returns `null`, a `NullPointerException` is thrown, uncaught by the surrounding `catch (BalanceInsufficientException | InvalidProtocolBufferException | ArithmeticException e)` block, propagating out of the actuator during block/transaction application. [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L93-101)
```java
        //save to new asset store
        if (!Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
          String firstTokenRealID = assetIssueStore.get(firstTokenID).getId();
          firstTokenID = firstTokenRealID.getBytes();
        }
        if (!Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
          String secondTokenRealID = assetIssueStore.get(secondTokenID).getId();
          secondTokenID = secondTokenRealID.getBytes();
        }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L127-132)
```java
    } catch (BalanceInsufficientException | InvalidProtocolBufferException
        | ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L188-199)
```java
    if (dynamicStore.getAllowSameTokenName() == 1) {
      if (!Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES) && !isNumber(firstTokenID)) {
        throw new ContractValidateException("first token id is not a valid number");
      }
      if (!Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES) && !isNumber(secondTokenID)) {
        throw new ContractValidateException("second token id is not a valid number");
      }
    }

    if (Arrays.equals(firstTokenID, secondTokenID)) {
      throw new ContractValidateException("cannot exchange same tokens");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L210-228)
```java
    if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(firstTokenBalance, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(firstTokenID, firstTokenBalance, dynamicStore)) {
        throw new ContractValidateException("first token balance is not enough");
      }
    }

    if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(secondTokenBalance, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(secondTokenID, secondTokenBalance, dynamicStore)) {
        throw new ContractValidateException("second token balance is not enough");
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L254-273)
```java
        AssetIssueCapsule assetIssueCapsule = Commons
            .getAssetIssueStoreFinal(dynamicStore, assetIssueStore, assetIssueV2Store)
            .get(sellTokenID);
        if (assetIssueCapsule == null) {
          throw new ContractValidateException("No sellTokenId !");
        }
        if (!ownerAccount.assetBalanceEnoughV2(sellTokenID, sellTokenQuantity,
            dynamicStore)) {
          throw new ContractValidateException("SellToken balance is not enough !");
        }
      }

      if (!Arrays.equals(buyTokenID, "_".getBytes())) {
        // Whether have the token
        AssetIssueCapsule assetIssueCapsule = Commons
            .getAssetIssueStoreFinal(dynamicStore, assetIssueStore, assetIssueV2Store)
            .get(buyTokenID);
        if (assetIssueCapsule == null) {
          throw new ContractValidateException("No buyTokenId !");
        }
```
