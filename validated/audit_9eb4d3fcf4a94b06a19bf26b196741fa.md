### Title
Unauthenticated NullPointerException in `UpdateAssetActuator.execute` crashes/reverts transaction processing due to missing V2-store existence check - (File: `actuator/src/main/java/org/tron/core/actuator/UpdateAssetActuator.java`)

### Summary
`UpdateAssetActuator.execute()` unconditionally fetches an `AssetIssueCapsule` from `AssetIssueV2Store` using the caller-controlled account's `AssetIssuedID` and immediately dereferences it, without checking for `null`, regardless of which `AllowSameTokenName` branch was taken in `validate()`. Because `validate()` only verifies existence in `AssetIssueV2Store` when `AllowSameTokenName == 1` (and only verifies `AssetIssueStore` (V1) when `AllowSameTokenName == 0`), an account whose V2 index entry is missing or stale can pass validation and then trigger a `NullPointerException` in `execute()`.

### Finding Description
In `UpdateAssetActuator.execute()`: [1](#0-0) 
the code does:
```java
AccountCapsule accountCapsule = accountStore.get(ownerAddress);
...
assetIssueCapsuleV2 = assetIssueStoreV2.get(accountCapsule.getAssetIssuedID().toByteArray());
assetIssueCapsuleV2.setFreeAssetNetLimit(newLimit);   // NPE if assetIssueCapsuleV2 == null
```
This fetch-and-dereference of the V2 capsule happens *before* and *outside* the `dynamicStore.getAllowSameTokenName() == 0` branch check, i.e. it always runs.

Compare this to `validate()`: [2](#0-1) 
```java
if (dynamicStore.getAllowSameTokenName() == 0) {
  if (account.getAssetIssuedName().isEmpty()) { throw ...; }
  if (assetIssueStore.get(account.getAssetIssuedName().toByteArray()) == null) { throw ...; }
} else {
  if (account.getAssetIssuedID().isEmpty()) { throw ...; }
  if (assetIssueV2Store.get(account.getAssetIssuedID().toByteArray()) == null) { throw ...; }
}
```
When `AllowSameTokenName == 0`, `validate()` only checks the **V1** `AssetIssueStore` for the asset named by `AssetIssuedName`; it never checks whether `AssetIssueV2Store.get(AssetIssuedID)` returns a non-null result. If an account's `AssetIssuedID` is empty or points to an entry absent from `AssetIssueV2Store` (e.g. legacy accounts issued before the dual-write logic, or any state where V1/V2 stores diverge), `validate()` still succeeds, and `execute()` then dereferences a `null` `AssetIssueCapsule`, throwing an uncaught `NullPointerException`.

The actuator's own `catch` block only handles `InvalidProtocolBufferException`: [3](#0-2) 
so the `NullPointerException` propagates as an unchecked `RuntimeException` out of `Actuator.execute()`, called directly from `RuntimeImpl.execute()`: [4](#0-3) 
and from `Manager.processTransaction()` / `processBlock()` without any generic `RuntimeException`/`NullPointerException` catch clause in the block-application path.

### Impact Explanation
This is directly analogous to the CVE-2020-25692 pattern: an update/rename-style request (`UpdateAssetContract`, which updates asset limits/URL/description) triggers a NULL pointer dereference in server-side processing due to a missing existence check, causing a crash of the core service. In java-tron's block/transaction-processing path, an uncaught `NullPointerException` thrown from an actuator during `processTransaction` inside `processBlock` is not one of the checked exceptions caught by `Manager.processBlock`'s catch clauses (`ValidateSignatureException | ContractValidateException | ContractExeException | ...`), nor `TronNetDelegate.processBlock`'s catch clauses: [5](#0-4) 
so it can propagate and crash the node processing thread, potentially halting block production/application (a Denial-of-Service on the node), consistent with the "High" severity of the source advisory (unauthenticated crash of the daemon).

### Likelihood Explanation
Reaching this state requires an account whose `AssetIssuedID` does not resolve to an entry in `AssetIssueV2Store` while `AllowSameTokenName == 0`. On current TRON mainnet, `AllowSameTokenName` has long been activated (`== 1`), and `AssetIssueActuator.execute()` always populates both `AssetIssueStore` and `AssetIssueV2Store` and always sets `AssetIssuedID` for newly issued assets: [6](#0-5) 
so on a fully-synced, already-activated mainnet the divergent state is unlikely for newly issued assets. However, on any private/test network deployed from this codebase where `AllowSameTokenName` has not yet been activated (its default/genesis value), or where accounts carry legacy/migrated data (e.g. via `AssetUpdateHelper`, which rebuilds `AssetIssueV2Store` from scratch and could produce an ID mismatch if run against inconsistent state), an unprivileged asset-issuer account could trigger this NPE by simply broadcasting an `UpdateAssetContract` transaction. I could not fully verify from static analysis alone whether such an inconsistent state is reachable on a live, fully-activated mainnet chain — this would require confirming that `AssetIssuedID`/`AssetIssueV2Store` are always kept perfectly consistent across every code path (including old migrated snapshots and any admin/reset tooling), which the index does not let me fully audit.

### Recommendation
In `UpdateAssetActuator.execute()`, add a null check on `assetIssueCapsuleV2` (and `assetIssueCapsule`, when accessed) mirroring `validate()`, and fail with a `ContractExeException` rather than dereferencing a possibly-null capsule — i.e., re-validate/guard state read in `execute()` exactly as done in `validate()`, or restructure `execute()` to only fetch/write the V2 capsule inside the same `if (dynamicStore.getAllowSameTokenName() == 0) { ... } else { ... }` branching used by `validate()`.

### Proof of Concept
1. Deploy/operate a java-tron network (or private chain from this codebase) where `AllowSameTokenName == 0` (default pre-activation state).
2. Issue an asset via `AssetIssueContract` from account A, then arrange (e.g., through a state where `AssetIssueV2Store` entry for A's `AssetIssuedID` is absent — such as inconsistent data introduced by asset-store migration/reset tooling, or an account whose `AssetIssuedID` was never populated) so that `AssetIssueV2Store.get(A.getAssetIssuedID())` returns `null` while `AssetIssueStore.get(A.getAssetIssuedName())` still returns non-null.
3. Broadcast an `UpdateAssetContract` transaction signed by account A (any owner-controlled request, e.g. via `wallet/updateasset` HTTP API or gRPC).
4. `UpdateAssetActuator.validate()` passes (V1 store check succeeds). `UpdateAssetActuator.execute()` calls `assetIssueStoreV2.get(...)` returning `null`, then `assetIssueCapsuleV2.setFreeAssetNetLimit(newLimit)` throws `NullPointerException`, propagating uncaught through `RuntimeImpl.execute()` and `Manager.processTransaction()`/`processBlock()`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAssetActuator.java (L51-62)
```java
      AccountCapsule accountCapsule = accountStore.get(ownerAddress);

      AssetIssueCapsule assetIssueCapsule;
      AssetIssueCapsule assetIssueCapsuleV2;

      AssetIssueStore assetIssueStoreV2 = assetIssueV2Store;
      assetIssueCapsuleV2 = assetIssueStoreV2.get(accountCapsule.getAssetIssuedID().toByteArray());

      assetIssueCapsuleV2.setFreeAssetNetLimit(newLimit);
      assetIssueCapsuleV2.setPublicFreeAssetNetLimit(newPublicLimit);
      assetIssueCapsuleV2.setUrl(newUrl);
      assetIssueCapsuleV2.setDescription(newDescription);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAssetActuator.java (L81-85)
```java
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAssetActuator.java (L131-149)
```java
    if (dynamicStore.getAllowSameTokenName() == 0) {
      if (account.getAssetIssuedName().isEmpty()) {
        throw new ContractValidateException("Account has not issued any asset");
      }

      if (assetIssueStore.get(account.getAssetIssuedName().toByteArray())
          == null) {
        throw new ContractValidateException("Asset is not existed in AssetIssueStore");
      }
    } else {
      if (account.getAssetIssuedID().isEmpty()) {
        throw new ContractValidateException("Account has not issued any asset");
      }

      if (assetIssueV2Store.get(account.getAssetIssuedID().toByteArray())
          == null) {
        throw new ContractValidateException("Asset is not existed in AssetIssueV2Store");
      }
    }
```

**File:** framework/src/main/java/org/tron/common/runtime/RuntimeImpl.java (L52-60)
```java
    if (actuator2 != null) {
      actuator2.validate(context);
      actuator2.execute(context);
    } else {
      for (Actuator act : actuatorList) {
        act.validate();
        act.execute(context.getProgramResult().getRet());
      }
    }
```

**File:** framework/src/main/java/org/tron/core/net/TronNetDelegate.java (L295-312)
```java
      } catch (ValidateSignatureException
          | ContractValidateException
          | ContractExeException
          | UnLinkedBlockException
          | ValidateScheduleException
          | AccountResourceInsufficientException
          | TaposException
          | TooBigTransactionException
          | TooBigTransactionResultException
          | DupTransactionException
          | TransactionExpirationException
          | BadNumberBlockException
          | BadBlockException
          | NonCommonBlockException
          | ReceiptCheckErrException
          | VMIllegalException
          | ZksnarkException
          | EventBloomException e) {
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L78-118)
```java
      if (dynamicStore.getAllowSameTokenName() == 0) {
        assetIssueCapsuleV2.setPrecision(0);
        assetIssueStore
            .put(assetIssueCapsule.createDbKey(), assetIssueCapsule);
        assetIssueV2Store
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      } else {
        assetIssueV2Store
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      }

      adjustBalance(accountStore, ownerAddress, -fee);
      if (dynamicStore.supportBlackHoleOptimization()) {
        dynamicStore.burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);//send to blackhole
      }
      AccountCapsule accountCapsule = accountStore.get(ownerAddress);
      List<FrozenSupply> frozenSupplyList = assetIssueContract.getFrozenSupplyList();
      Iterator<FrozenSupply> iterator = frozenSupplyList.iterator();
      long remainSupply = assetIssueContract.getTotalSupply();
      List<Frozen> frozenList = new ArrayList<>();
      long startTime = assetIssueContract.getStartTime();

      while (iterator.hasNext()) {
        FrozenSupply next = iterator.next();
        long expireTime = startTime + next.getFrozenDays() * FROZEN_PERIOD;
        Frozen newFrozen = Frozen.newBuilder()
            .setFrozenBalance(next.getFrozenAmount())
            .setExpireTime(expireTime)
            .build();
        frozenList.add(newFrozen);
        remainSupply -= next.getFrozenAmount();
      }

      if (dynamicStore.getAllowSameTokenName() == 0) {
        accountCapsule.addAsset(assetIssueCapsule.createDbKey(), remainSupply);
      }
      accountCapsule.setAssetIssuedName(assetIssueCapsule.createDbKey());
      accountCapsule.setAssetIssuedID(assetIssueCapsule.createDbV2Key());
      accountCapsule.addAssetV2(assetIssueCapsuleV2.createDbV2Key(), remainSupply);
```
