## Finding [1](#0-0) 

The reported bug class — ERC20 `transfer`/`transferFrom` boolean return values not being checked (per the `Vesting.sol` report) — has a direct structural analog in java-tron's `TransferAssetActuator.execute()`.

### Title
Unchecked boolean return value of `addAssetAmountV2` in `TransferAssetActuator.execute()` can silently drop TRC10 token credit while debit is still applied - (File: `actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java`)

### Summary
`TransferAssetActuator.execute()` handles a `TransferAssetContract`, a broadcastable contract type any signed transaction sender can submit. It debits the sender's TRC10 asset balance via `reduceAssetAmountV2(...)` and checks the boolean return value, throwing `ContractExeException` on failure [2](#0-1) . Immediately afterward, it credits the receiver via `toAccountCapsule.addAssetAmountV2(...)` but discards the boolean result entirely [3](#0-2) . This is the same class of defect described in the external report: a balance-mutating call that can indicate failure via its return value is treated as if it always succeeds.

### Finding Description
`reduceAssetAmountV2` and `addAssetAmountV2` are both boolean-returning methods on `AccountCapsule`, mirroring the ERC20 `transfer` pattern of "return false on failure instead of throwing." The actuator correctly gates on the sender-side reduction result but not on the receiver-side addition result. If `addAssetAmountV2` returns `false` for any reason (e.g. an internal overflow/limit check on the receiver's TRC10 balance map that can diverge from what was checked during `validate()`), the debit from the sender has already been persisted (via `accountStore.put(ownerAddress, ownerAccountCapsule)` at line 80) while the credit to the receiver never lands, and the actuator still reports `code.SUCESS`.

### Impact Explanation
If the unchecked path can be triggered, the effect is token amount removed from the sender's balance without being credited anywhere — i.e., permanent loss/freezing of TRC10 asset supply and an inconsistent total-issued vs. sum-of-balances invariant for that asset. This matches the "permanent freezing of funds / unbacked balance" impact bar required by the analog rules.

### Likelihood Explanation
`validate()` does perform an overflow check on the receiver's projected asset balance at validation time using `addExact` [4](#0-3) , which reduces — but does not by inspection fully eliminate — the chance that `addAssetAmountV2` diverges and returns `false` at execute() time (e.g. edge cases around `AllowSameTokenName`/V1-vs-V2 asset ID resolution, or any additional internal validation inside `addAssetAmountV2` not mirrored in `validate()`). I was not able to fully inspect the body of `AccountCapsule.addAssetAmountV2` within the available tool budget to enumerate every condition under which it returns `false`, so the exact reachability of a divergence between the `validate()`-time check and the `execute()`-time check is unconfirmed. This uncertainty affects the likelihood assessment; the root-cause code pattern (unchecked boolean return of a balance-mutating call, asymmetric with the sibling checked call two lines above) is nonetheless clearly and concretely present in the code.

### Recommendation
Check the return value of `addAssetAmountV2` the same way `reduceAssetAmountV2` is checked, and throw `ContractExeException` (rolling back the transaction) if it returns `false`, so that the debit is never persisted without a matching credit.

### Proof of Concept
Not fully constructible without confirming a concrete condition under which `addAssetAmountV2` returns `false` after `validate()` has already passed its own overflow check — this would require reading `AccountCapsule.addAssetAmountV2`'s full implementation, which I could not complete before running out of tool calls. The code-level defect (asymmetric error handling between the two sibling calls) is verified directly from the actuator source cited above.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L75-84)
```java
      AccountCapsule ownerAccountCapsule = accountStore.get(ownerAddress);
      if (!ownerAccountCapsule
          .reduceAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
      accountStore.put(ownerAddress, ownerAccountCapsule);

      toAccountCapsule
          .addAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore);
      accountStore.put(toAddress, toAccountCapsule);
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
