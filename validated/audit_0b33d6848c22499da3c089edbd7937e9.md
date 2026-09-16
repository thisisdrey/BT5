### Title
Ignored `reduceAssetAmountV2` return value in `ExchangeTransactionActuator.execute()` allows execution to proceed after a failed asset debit - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java)

### Summary
`ExchangeTransactionActuator.execute()` calls `AccountCapsule.reduceAssetAmountV2(...)` (and `addAssetAmountV2(...)`) but discards the boolean success/failure result, exactly the "ignored return value" pattern described in the external report for `Amp.sol`'s `transferFrom`. Unlike the sibling `TransferAssetActuator`, which explicitly checks the return value and throws `ContractExeException` on failure, `ExchangeTransactionActuator` assumes the debit always succeeds and continues to credit the exchange pool and commit account/exchange state regardless.

### Finding Description
In `execute()`: [1](#0-0) 
the debit call `accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);` and the credit call `accountCapsule.addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);` are both invoked for their side effects only — their boolean return values are never inspected. Execution unconditionally proceeds to `accountStore.put(...)` and `Commons.putExchangeCapsule(...)`, and reports `code.SUCESS` with `ret.setExchangeReceivedAmount(anotherTokenQuant)`.

Contrast this with `TransferAssetActuator`, which treats the same API's return value as authoritative: [2](#0-1) 
There, a `false` return from `reduceAssetAmountV2` throws `ContractExeException("reduceAssetAmount failed !")`, aborting the transfer. `ExchangeTransactionActuator` has no equivalent guard.

The `validate()` path does check `assetBalanceEnoughV2` before execution: [3](#0-2) 
which is presumably why `reduceAssetAmountV2` "usually" succeeds. However, I could not locate/verify the implementation of `AccountCapsule.reduceAssetAmountV2`/`addAssetAmountV2` in the indexed codebase (the file `chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java` did not return matches for these method names via search, likely due to index truncation), so I cannot confirm every condition under which these methods can return `false` independent of the balance check already performed in `doValidate()` (e.g., asset-count limits, `AllowSameTokenName`-related dual V1/V2 bookkeeping divergence, or other internal store-consistency checks inside the capsule). This is the key uncertainty: whether there exists a reachable state where `validate()` passes but `execute()`'s `reduceAssetAmountV2`/`addAssetAmountV2` calls fail.

### Impact Explanation
If any code path allows `reduceAssetAmountV2` to return `false` while `validate()` still passed (e.g. due to a TOCTOU issue between validate and execute, or a different failure condition than the plain balance check), the actuator would silently skip debiting the sender's real asset balance while still crediting `anotherTokenQuant` to the sender and updating the exchange pool balances as if the trade succeeded. This is a classic unbacked-balance/mint pattern — the same bug class as the reported `Amp.sol` issue — and would corrupt exchange pool accounting and/or grant the caller assets without paying for them.

### Likelihood Explanation
Given `doValidate()` performs an explicit `assetBalanceEnoughV2` check just before `execute()` runs within the same transaction processing (no untrusted reentrancy window in java-tron's synchronous actuator model), the most obvious failure mode is not directly reachable through the ordinary path. Since I could not verify `AccountCapsule.reduceAssetAmountV2`'s exact implementation and all its failure conditions, I cannot confirm a concretely exploitable divergent path exists. This significantly lowers confidence that this is an exploitable, reachable bug versus a defensive-coding gap.

### Recommendation
Check the return values of `reduceAssetAmountV2` and `addAssetAmountV2` in `ExchangeTransactionActuator.execute()`, mirroring `TransferAssetActuator`'s pattern: throw `ContractExeException` (and set `ret.setStatus(fee, code.FAILED)`) if either call returns `false`, rather than assuming success and proceeding to persist account/exchange state.

### Proof of Concept
Not established — I was unable to confirm a validate()-passes-but-execute()-fails divergence path due to incomplete visibility into `AccountCapsule.reduceAssetAmountV2`/`addAssetAmountV2` internals in the available index. A background Devin session with full repository access would be needed to inspect `chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java` in full and determine whether any reachable state causes these calls to fail post-validation.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L80-93)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L207-221)
```java
    if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(tokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(tokenID, tokenQuant, dynamicStore)) {
        throw new ContractValidateException("token balance is not enough");
      }
    }

    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L75-83)
```java
      AccountCapsule ownerAccountCapsule = accountStore.get(ownerAddress);
      if (!ownerAccountCapsule
          .reduceAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
      accountStore.put(ownerAddress, ownerAccountCapsule);

      toAccountCapsule
          .addAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore);
```
