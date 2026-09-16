### Title
`UpdateAssetActuator` Lets an Asset Issuer Set `new_public_limit`/`new_limit` Bandwidth Caps Without Validating Against Existing Consumption State - (File: actuator/src/main/java/org/tron/core/actuator/UpdateAssetActuator.java)

### Summary
The Buffer finding describes `setMaxLiquidity()` accepting a new limit value without checking it against the pool's actual current state (`totalTokenXBalance`), which can brick normal operation. The closest reachable analog in java-tron is `UpdateAssetActuator`, an actuator invoked by an unprivileged, single-signed `UpdateAssetContract` transaction (any TRC10 asset issuer can call it), which sets `free_asset_net_limit` and `public_free_asset_net_limit` on an `AssetIssueCapsule` while only bounding the new values against a fixed global constant (`getOneDayNetLimit()`), never against the asset's already-accrued `public_free_asset_net_usage` / `free_asset_net_usage`.

### Finding Description
`UpdateAssetActuator.doValidate()` checks only: [1](#0-0) 
i.e. `0 <= newLimit < oneDayNetLimit` and `0 <= newPublicLimit < oneDayNetLimit`. It never reads or compares against the asset's current `getPublicFreeAssetNetUsage()`/`getFreeAssetNetUsage()` fields, which are the running counters consumed by `TransferAssetContract` bandwidth accounting in `BandwidthProcessor.useAssetAccountNet`: [2](#0-1) 

`execute()` then blindly overwrites the limit fields on both the V1 and V2 `AssetIssueCapsule` records: [3](#0-2) 

This mirrors the Buffer bug class exactly: a governance-like limit setter, callable by a single unprivileged party (the token issuer, not an SR/committee — this is a self-service actuator on their own asset), that adjusts a resource cap in isolation from the live consumption/state it is supposed to bound.

### Impact Explanation
Because `newPublicFreeAssetNetLimit`/`newFreeAssetNetLimit` can be set to any value within `[0, oneDayNetLimit)` irrespective of the asset's already-consumed usage for the current window, `useAssetAccountNet` computes `bytes > (publicFreeAssetNetLimit - newPublicFreeAssetNetUsage)` and `bytes > (freeAssetNetLimit - newFreeAssetNetUsage)`; if the issuer lowers the limit below (or even to) the currently accrued usage, the subtraction yields a non-positive remaining allowance, permanently rejecting the free-bandwidth path for that asset's transfers for the rest of the accounting window regardless of how the asset was actually intended to be configured — i.e. users can no longer transfer that TRC10 token for free even though the issuer/holders expect bandwidth availability, forcing fallback to paid TRX bandwidth or outright `AccountResourceInsufficientException` failures if the account has no other resources. This is a self-inflicted but still protocol-observable denial-of-service on that asset's free-transfer path; it does not (from what I can verify) allow theft or double-spend of funds — the actuator only touches bandwidth accounting fields, not balances. Given the code I reviewed, I could not find any additional monetary-loss vector chained from this (e.g. no interaction with `total_supply` or account balances), so the concrete confirmed impact is resource-DoS on a specific asset's public/free bandwidth allotment, not the broader "unbacked balance" tier described in the validation rules.

### Likelihood Explanation
Any account that has issued a TRC10 asset (`AssetIssueContract`, reachable via a normal broadcast transaction) can call `UpdateAssetContract` at will and immediately misconfigure their own asset's limits — no special privilege, proposal, or committee approval is required, unlike `EXCHANGE_BALANCE_LIMIT` (a `DynamicPropertiesStore` value only settable via `ProposalApproveContract`, which requires SR votes and is out of scope here). This makes the likelihood high for an asset issuer to trigger, though the blast radius is limited to that specific asset's bandwidth economics, not the whole network.

### Recommendation
In `UpdateAssetActuator.doValidate()`, before accepting `newLimit`/`newPublicLimit`, fetch the current `AssetIssueCapsule` and cross-check the proposed limits against `getPublicFreeAssetNetUsage()` / `getFreeAssetNetUsage()` (after applying the same time-decay `increase()` logic used in `BandwidthProcessor`), rejecting or clamping updates that would immediately zero-out or invert available bandwidth for existing consumers, analogous to how `ExchangeCreateActuator`/`ExchangeInjectActuator` validate `newTokenBalance`/`newAnotherTokenBalance` against `dynamicStore.getExchangeBalanceLimit()` before persisting new pool balances ( [4](#0-3) ).

### Proof of Concept
1. Attacker issues a TRC10 asset via `AssetIssueContract` (any address can do this) with `public_free_asset_net_limit = X`, `free_asset_net_limit = Y`.
2. Multiple holders transfer the asset via `TransferAssetContract`; `BandwidthProcessor.useAssetAccountNet` accrues `publicFreeAssetNetUsage` toward `X` and per-account `freeAssetNetUsage` toward `Y` ( [5](#0-4) ).
3. The issuer broadcasts `UpdateAssetContract` with `new_public_limit = 0` (or any value ≤ the already-accrued usage). `UpdateAssetActuator.doValidate()` only checks `0 <= newPublicLimit < oneDayNetLimit`, so this passes ( [6](#0-5) ), and `execute()` immediately writes the new limit to both V1/V2 capsules.
4. Subsequent `TransferAssetContract` calls for that asset now fail the `bytes > (publicFreeAssetNetLimit - newPublicFreeAssetNetUsage)` check unconditionally (since the difference is negative/zero) and fall through to paid bandwidth or `AccountResourceInsufficientException`, denying free transfers for the asset's holders until the usage window resets — demonstrating the DoS analog to Buffer's `maxLiquidity` misconfiguration.

Note: I was unable to fully verify whether any downstream consumer of `public_free_asset_net_limit`/`free_asset_net_limit` treats a "misconfigured" (too-low) value as anything beyond a DoS on free bandwidth (e.g., no fund-loss path was found in the indexed portions of `BandwidthProcessor.java` and `UpdateAssetActuator.java`); this assessment is based on the code sections retrieved and may be incomplete if additional interactions exist elsewhere in the codebase not surfaced by search.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAssetActuator.java (L59-78)
```java
      assetIssueCapsuleV2.setFreeAssetNetLimit(newLimit);
      assetIssueCapsuleV2.setPublicFreeAssetNetLimit(newPublicLimit);
      assetIssueCapsuleV2.setUrl(newUrl);
      assetIssueCapsuleV2.setDescription(newDescription);

      if (dynamicStore.getAllowSameTokenName() == 0) {
        assetIssueCapsule = assetIssueStore.get(accountCapsule.getAssetIssuedName().toByteArray());
        assetIssueCapsule.setFreeAssetNetLimit(newLimit);
        assetIssueCapsule.setPublicFreeAssetNetLimit(newPublicLimit);
        assetIssueCapsule.setUrl(newUrl);
        assetIssueCapsule.setDescription(newDescription);

        assetIssueStore
            .put(assetIssueCapsule.createDbKey(), assetIssueCapsule);
        assetIssueStoreV2
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      } else {
        assetIssueV2Store
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAssetActuator.java (L159-166)
```java
    if (newLimit < 0 || newLimit >= dynamicStore.getOneDayNetLimit()) {
      throw new ContractValidateException("Invalid FreeAssetNetLimit");
    }

    if (newPublicLimit < 0 || newPublicLimit >=
        dynamicStore.getOneDayNetLimit()) {
      throw new ContractValidateException("Invalid PublicFreeAssetNetLimit");
    }
```

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L317-353)
```java
    long publicFreeAssetNetLimit = assetIssueCapsule.getPublicFreeAssetNetLimit();
    long publicFreeAssetNetUsage = assetIssueCapsule.getPublicFreeAssetNetUsage();
    long publicLatestFreeNetTime = assetIssueCapsule.getPublicLatestFreeNetTime();

    long newPublicFreeAssetNetUsage = increase(publicFreeAssetNetUsage, 0,
        publicLatestFreeNetTime, now);

    if (bytes > (publicFreeAssetNetLimit - newPublicFreeAssetNetUsage)) {
      logger.debug("The {} public free bandwidth is not enough."
              + " Bytes: {}, publicFreeAssetNetLimit: {}, newPublicFreeAssetNetUsage: {}.",
          tokenID, bytes, publicFreeAssetNetLimit,  newPublicFreeAssetNetUsage);
      return false;
    }

    long freeAssetNetLimit = assetIssueCapsule.getFreeAssetNetLimit();

    long freeAssetNetUsage;
    long latestAssetOperationTime;
    if (chainBaseManager.getDynamicPropertiesStore().getAllowSameTokenName() == 0) {
      freeAssetNetUsage = accountCapsule
          .getFreeAssetNetUsage(tokenName);
      latestAssetOperationTime = accountCapsule
          .getLatestAssetOperationTime(tokenName);
    } else {
      freeAssetNetUsage = accountCapsule.getFreeAssetNetUsageV2(tokenID);
      latestAssetOperationTime = accountCapsule.getLatestAssetOperationTimeV2(tokenID);
    }

    long newFreeAssetNetUsage = increase(freeAssetNetUsage, 0,
        latestAssetOperationTime, now);

    if (bytes > (freeAssetNetLimit - newFreeAssetNetUsage)) {
      logger.debug("The {} free bandwidth is not enough."
              + " Bytes: {}, freeAssetNetLimit: {}, newFreeAssetNetUsage:{}.",
          tokenID, bytes, freeAssetNetLimit, newFreeAssetNetUsage);
      return false;
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L233-236)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (newTokenBalance > balanceLimit || newAnotherTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```
