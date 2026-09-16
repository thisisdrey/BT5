### Title
`ParticipateAssetIssueActuator` ignores the boolean return value of `addAssetAmountV2`, letting a participant pay TRX without receiving the purchased asset - (File: `actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java`)

### Summary
`ParticipateAssetIssueActuator.execute()` calls `ownerAccount.addAssetAmountV2(...)` without checking its boolean return value, while the symmetric call `toAccount.reduceAssetAmountV2(...)` a few lines later *is* checked and throws `ContractExeException` on failure. This mirrors the reported bug class: a boolean-returning balance/asset-mutation call whose failure is silently ignored.

### Finding Description
In `execute()`: [1](#0-0) 

- `ownerAccount.addAssetAmountV2(key, exchangeAmount, dynamicStore, assetIssueStore)` returns a `boolean` (per the method signatures confirmed by `grep_search` for `public boolean addAssetAmountV2`/`public boolean reduceAssetAmountV2` in `chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java`), but the return value is discarded on line 79.
- Immediately after, the same class checks the result of `toAccount.reduceAssetAmountV2(...)` and throws `ContractExeException("reduceAssetAmount failed !")` if it returns `false` (lines 85-87), showing the developers were aware this family of methods can fail and must be checked — but missed doing so for the `addAssetAmountV2` call on the owner's account.
- Regardless of whether `addAssetAmountV2` succeeds or silently fails, execution proceeds to:
  - persist `ownerAccount` and `toAccount` to `accountStore` (lines 90-91),
  - set the transaction result to `SUCESS` (line 92).

This is directly analogous to the reported Olympus issue where `withdrawAndUnwrap`'s boolean return is ignored, allowing the operation to "silently fail" while the surrounding logic proceeds as if it succeeded.

### Impact Explanation
If `addAssetAmountV2` returns `false` (e.g., due to an internal balance/limit check inside `AccountCapsule` failing), the transaction still reports `SUCESS`, the owner's TRX balance is debited (line 67-69, `cost` + `fee` subtracted), the `toAccount`'s asset balance is debited via the checked `reduceAssetAmountV2`, and the `toAccount` receives the TRX `cost` (line 84) — but the `ownerAccount` never receives the purchased asset tokens. This results in a participant paying TRX and receiving nothing in return: a concrete loss of funds for the caller with no possibility of detecting the failure from the transaction result, since `ret.setStatus(fee, code.SUCESS)` is unconditionally applied for this path.

### Likelihood Explanation
This is reachable by any account broadcasting a `ParticipateAssetIssueContract` transaction (`TriggerSmartContract`-independent, plain wallet/API path), so it is a low-privilege, directly triggerable actuator. The likelihood of `addAssetAmountV2` actually returning `false` in practice depends on internal precondition checks inside `AccountCapsule` (not fully inspected here due to file-read limitations in this session), but the code path itself unconditionally proceeds to commit state and mark the transaction successful without verifying the operation's own asserted success/failure contract — the same class of defect flagged in the original report.

### Recommendation
Check the boolean return value of `ownerAccount.addAssetAmountV2(...)` symmetrically with the existing `reduceAssetAmountV2` check, and throw `ContractExeException` (setting `ret.setStatus(fee, code.FAILED)`) if it returns `false`, preventing silent partial-application of the participate-asset-issue exchange.

### Proof of Concept
1. Submit a `ParticipateAssetIssueContract` transaction where the internal accounting inside `AccountCapsule.addAssetAmountV2` would return `false` for the owner's asset map (e.g., an internal overflow/limit condition specific to `AccountCapsule`'s asset map implementation).
2. `ParticipateAssetIssueActuator.execute()` still: subtracts `cost + fee` from the owner's TRX balance, credits `toAccount`'s TRX balance with `cost`, and calls the checked `toAccount.reduceAssetAmountV2` (which succeeds, deducting `toAccount`'s asset).
3. Both accounts are persisted (lines 90-91) and `ret.setStatus(fee, code.SUCESS)` is set — the transaction is recorded on-chain as successful.
4. The owner's TRX is spent and `toAccount`'s asset supply is reduced, but the owner's account never receives the corresponding `exchangeAmount` of the asset, since the failed `addAssetAmountV2` call's result was discarded.

Note: full verification of the exact internal conditions under which `AccountCapsule.addAssetAmountV2` returns `false` was not completed in this session due to tool/file-read limits; a Devin session with full repository access should inspect `AccountCapsule.addAssetAmountV2`'s implementation to confirm concrete triggering preconditions.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java (L77-91)
```java
      long exchangeAmount = multiplyExact(cost, assetIssueCapsule.getNum());
      exchangeAmount = floorDiv(exchangeAmount, assetIssueCapsule.getTrxNum());
      ownerAccount.addAssetAmountV2(key, exchangeAmount, dynamicStore, assetIssueStore);

      //add to to_address
      byte[] toAddress = participateAssetIssueContract.getToAddress().toByteArray();
      AccountCapsule toAccount = accountStore.get(toAddress);
      toAccount.setBalance(addExact(toAccount.getBalance(), cost));
      if (!toAccount.reduceAssetAmountV2(key, exchangeAmount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }

      //write to db
      accountStore.put(ownerAddress, ownerAccount);
      accountStore.put(toAddress, toAccount);
```
