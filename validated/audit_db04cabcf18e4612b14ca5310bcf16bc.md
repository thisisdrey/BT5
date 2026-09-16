### Title
Unbounded per-account asset iteration in `BandwidthProcessor.updateUsage` / `AccountCapsule.getAssetMapV2` enables a DoS via TRC10 token spam - (File: `chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java`)

### Summary
`BandwidthProcessor.updateUsage(AccountCapsule)` calls `accountCapsule.getAssetMapV2()` on every invocation, which internally triggers a full, unbounded scan of every distinct TRC10 asset ever held by that account. An attacker can inflate the number of distinct asset denominations associated with a victim account by issuing many TRC10 tokens and transferring small amounts of each to the target address, then force expensive re-computation of that account's resource state on subsequent transactions/queries — the same class of bug as the Canto `GetAllBalances`/`GetPoolBalances` finding, where an attacker inflates a balances array to blow up gas/CPU cost of an "iterate all tokens for an address" routine.

### Finding Description
`BandwidthProcessor.updateUsage`: [1](#0-0) 
unconditionally calls `accountCapsule.getAssetMapV2()`, which (per `AccountCapsule.java`) invokes `importAllAsset()` before returning the map: [2](#0-1) 

`importAllAsset()` (present in `AccountCapsule.java`, confirmed by 3 references) is backed by `AccountAssetStore.getAllAssets`, which performs a full `prefixQuery` scan over the on-disk `account-asset` column family for the account's address, iterating one DB entry per distinct token denomination the account has ever held (when asset-optimization is enabled): [3](#0-2) 

This is structurally identical to the Cosmos SDK's `GetAllBalances`, which iterates every coin denomination held by an address without bound. In java-tron, the "denominations" are TRC10 token IDs held in an account's asset map. An unprivileged attacker can:
1. Issue many distinct TRC10 tokens via `AssetIssueContract` (each costing the configured asset-issue fee, no whitelist restriction blocks arbitrary new denominations, unlike Canto's coinswap pools).
2. Transfer a minimal amount of each token to a target/victim account via `TransferAssetContract`, inflating the victim account's `AssetV2` map / `account-asset` store entries to an arbitrarily large size.
3. Any subsequent code path that calls `getAssetMapV2()`/`importAllAsset()` on the victim account — including `BandwidthProcessor.updateUsage(AccountCapsule)`, which is invoked from `Wallet.java` on account/bandwidth query APIs (`getAccountNet`, etc.) as well as from `UnDelegateResourceActuator`, `VMActuator`, and `UnDelegateResourceProcessor` during transaction execution — must perform an iteration/DB scan whose cost scales linearly with the number of denominations the attacker planted, with no upper bound enforced anywhere in the call chain.

### Impact Explanation
Because the iteration cost is unbounded and is triggered both by ordinary transaction processing (bandwidth accounting during `TransferAssetContract`/`UnDelegateResource` operations touching the polluted account) and by read-only gRPC/HTTP query paths in `Wallet.java`, an attacker can degrade or stall processing of transactions involving the victim account and/or exhaust node time/CPU when the node answers balance/account queries for that address. This can disrupt block processing throughput for transactions touching the account and degrade API service availability for that address, mirroring the "swap/RemoveLiquidity failure" impact assessed as Medium in the original Canto report.

### Likelihood Explanation
No special privilege is required: any account holder can issue TRC10 assets (subject only to the standard, non-prohibitive asset-issue fee) and freely transfer arbitrary amounts of many distinct tokens to any address via ordinary signed transactions. There is no limit in the codebase on the number of distinct asset denominations an account may accumulate, and the affected iteration is unconditionally executed by common account/bandwidth code paths, so the attack is straightforward and repeatable, only bounded by the attacker's willingness to pay cumulative asset-issue fees.

### Recommendation
Avoid unconditionally iterating over the full asset map of an account in hot paths such as `BandwidthProcessor.updateUsage`. Where per-asset free-bandwidth accounting is not strictly required for every held denomination, restrict the iteration to relevant/queried denominations only (analogous to Canto's mitigation of only fetching specific coin balances instead of `GetAllBalances`). Consider capping the number of distinct TRC10 denominations an account may hold, or paginating/limiting `importAllAsset`/`getAllAssets` scans, and auditing all callers of `getAssetMapV2()`/`importAllAsset()` (`Wallet.java`, `BandwidthProcessor.java`, `AssetUtil.java`, `MUtil.java`) for unbounded cost exposure.

### Proof of Concept
1. Attacker issues N (e.g., tens of thousands) distinct TRC10 assets via repeated `AssetIssueContract` transactions.
2. Attacker sends `TransferAssetContract` transactions delivering 1 unit of each of the N assets to victim address `V`.
3. `V`'s on-disk `account-asset` entries (and/or `AssetV2Map`) now contain N distinct denominations.
4. Any transaction touching `V` that triggers `BandwidthProcessor.updateUsage(accountCapsule)` — e.g., a `TransferAssetContract` sent by `V`, or an `UnDelegateResource` operation — now performs an O(N) map/DB iteration via `getAssetMapV2()`/`importAllAsset()`/`AccountAssetStore.getAllAssets`, as well as any gRPC/HTTP query in `Wallet.java` that reads `V`'s account/bandwidth state, causing measurable CPU/time cost scaling with N and no enforced upper bound.

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L57-79)
```java
    if (chainBaseManager.getDynamicPropertiesStore().getAllowSameTokenName() == 0) {
      Map<String, Long> assetMap = accountCapsule.getAssetMap();
      assetMap.forEach((assetName, balance) -> {
        long oldFreeAssetNetUsage = accountCapsule.getFreeAssetNetUsage(assetName);
        long latestAssetOperationTime = accountCapsule.getLatestAssetOperationTime(assetName);
        accountCapsule.putFreeAssetNetUsage(assetName,
            increase(oldFreeAssetNetUsage, 0, latestAssetOperationTime, now));
      });
    }
    Map<String, Long> assetMapV2 = accountCapsule.getAssetMapV2();
    Map<String, Long> map = new HashMap<>(assetMapV2);
    accountCapsule.getAllFreeAssetNetUsageV2().forEach((k, v) -> {
      if (!map.containsKey(k)) {
        map.put(k, 0L);
      }
    });
    map.forEach((assetName, balance) -> {
      long oldFreeAssetNetUsage = accountCapsule.getFreeAssetNetUsageV2(assetName);
      long latestAssetOperationTime = accountCapsule.getLatestAssetOperationTimeV2(assetName);
      accountCapsule.putFreeAssetNetUsageV2(assetName,
          increase(oldFreeAssetNetUsage, 0, latestAssetOperationTime, now));
    });
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L878-885)
```java
  public Map<String, Long> getAssetMapV2() {
    importAllAsset();
    Map<String, Long> assetMap = this.account.getAssetV2Map();
    if (assetMap.isEmpty()) {
      assetMap = Maps.newHashMap();
    }
    return assetMap;
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/AccountAssetStore.java (L97-109)
```java
  public Map<String, Long> getAllAssets(Protocol.Account account) {
    Map<String, Long> assets = new HashMap<>();
    if (account.getAssetOptimized()) {
      Map<WrappedByteArray, byte[]> map = prefixQuery(account.getAddress().toByteArray());
      map.forEach((k, v) -> {
        byte[] assetID = ByteArray.subArray(k.getBytes(),
                account.getAddress().toByteArray().length, k.getBytes().length);
        assets.put(ByteArray.toStr(assetID), Longs.fromByteArray(v));
      });
    }
    account.getAssetV2Map().forEach((k, v) -> assets.put(k, v));
    return assets;
  }
```
