### Title
Unmetered iteration over an account's TRC10 asset map in `BandwidthProcessor` allows low-cost spam-token flooding to inflate per-transaction validation cost - (File: `chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java`)

### Summary
`BandwidthProcessor.updateUsage(AccountCapsule)` iterates over the *entire* set of TRC10 asset balances held by an account (`getAssetMapV2()` / `getAllFreeAssetNetUsageV2()`) every time bandwidth accounting is performed for that account, and this map is populated on-demand by any address able to send it TRC10 tokens (`TransferAssetContract`). This mirrors the reported bug class: an unmetered, unbounded loop over a balance/asset collection that any unprivileged user can grow arbitrarily large by sending many distinct token denominations to a victim address, causing the collection-scanning code to become progressively more expensive without a corresponding fee.

### Finding Description
`BandwidthProcessor.updateUsage(AccountCapsule accountCapsule)` builds a combined map of all TRC10 assets ever associated with the account and iterates over it with `forEach`, once per usage-update call: [1](#0-0) 

`getAssetMapV2()` forces a full reconstruction of every asset entry the account holds via `importAllAsset()`: [2](#0-1) 

`importAllAsset()` in turn calls `AccountAssetStore.getAllAssets`, which does a raw key-prefix scan of the account-asset database keyed by the account's address, returning one entry per distinct TRC10 asset the account has ever received: [3](#0-2) [4](#0-3) 

Because a `TransferAssetContract` transaction lets *any* account send an arbitrary existing TRC10 token to *any* recipient address (no recipient opt-in is required, and only ordinary bandwidth/fee costs apply to the sender), an attacker can cheaply send many different existing TRC10 denominations, one unit each, to a single victim address. Each such transfer adds one more persistent entry to that address's asset-balance database. `BandwidthProcessor.updateUsage()` is invoked as part of `consume()`, which every full node must execute while validating/applying *any subsequent transaction originating from that flooded address* (e.g., a normal TRX transfer): [5](#0-4) 

The cost of this scan grows linearly with the number of distinct assets the target account has ever received, but the bandwidth/energy fee charged for a subsequent transaction from that account (`bytesSize` in `consume()`) is fixed and independent of the asset-map size, so the additional CPU/DB-scan work is effectively unmetered.

### Impact Explanation
Every full node in the network must perform the unbounded asset-map scan when validating any transaction sent from a spam-flooded address, since transaction validation/application in `Manager`'s block-application path calls `BandwidthProcessor.consume()`/`updateUsage()` for the tx's owner account. An attacker who cheaply floods one or more addresses (their own, or targeted addresses that will later transact) with many distinct pre-existing TRC10 assets can force disproportionate, unmetered work on all validating/full nodes for transactions touching those addresses, degrading throughput/liveness — the same "unmetered iteration over balances triggered on-demand by spam tokens" bug class as the reported finding, though the amplification is bounded by the number of pre-existing distinct TRC10 assets on the network rather than being fully attacker-controlled to the size seen in the original PoC (which used a permissionless native-token/coin registry).

### Likelihood Explanation
Sending TRC10 assets via `TransferAssetContract` is a normal, unprivileged, low-cost operation available to any signed transaction sender, and the number of already-issued distinct TRC10 tokens on a mature TRON-like chain can be in the thousands, giving a moderately achievable degree of amplification without needing to pay per-asset issuance fees. Any subsequent transaction from a flooded address (including one broadcast by the attacker themself) reliably triggers the unbounded scan on every node applying that transaction.

### Recommendation
Avoid reconstructing/iterating the entire per-account TRC10 asset map inside `BandwidthProcessor.updateUsage()`. Instead, only look up and update the specific asset(s) relevant to the transaction being processed (e.g., via `AccountAssetStore.getBalance`/targeted key lookups) rather than calling `getAssetMapV2()`/`getAllFreeAssetNetUsageV2()`, which force a full prefix scan of the account's asset store. Where a full free-net-usage decay must be recorded, prefer lazy, targeted updates keyed by the specific asset touched by the current contract instead of eagerly touching every asset ever received by the account.

### Proof of Concept
Conceptual PoC (cannot be executed here, but derivable from the cited code paths):
1. Identify or generate many distinct existing TRC10 token IDs on the network (`AssetIssueStore`/`AssetIssueV2Store` entries already present from prior issuances).
2. For each token ID, broadcast a low-value `TransferAssetContract` transaction sending 1 unit of that token to a target victim address `V`. This is a standard unprivileged operation costing only ordinary bandwidth fees, and each transfer adds a persistent row to `AccountAssetStore` keyed by `V`'s address (`chainbase/src/main/java/org/tron/core/store/AccountAssetStore.java` `getAssets`/`putAccount`).
3. Once thousands of distinct assets have accumulated on `V`, any transaction originating from `V` (e.g., a simple TRX transfer) will cause `BandwidthProcessor.consume()` → `updateUsage(accountCapsule)` to reconstruct and iterate the full asset map (`chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java:47-79`, backed by the prefix scan in `AccountAssetStore.getAllAssets`), imposing O(N) unmetered work on every node validating/applying that block, for a transaction paying only the fixed, size-based bandwidth fee.

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L57-78)
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
```

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L96-138)
```java
  @Override
  public void consume(TransactionCapsule trx, TransactionTrace trace)
      throws ContractValidateException, AccountResourceInsufficientException,
      TooBigTransactionResultException, TooBigTransactionException {
    List<Contract> contracts = trx.getInstance().getRawData().getContractList();
    long resultSizeWithMaxContractRet = trx.getResultSizeWithMaxContractRet();
    boolean optimizeTxs = !trx.isInBlock() || chainBaseManager
        .getDynamicPropertiesStore().allowConsensusLogicOptimization();
    if (!trx.isInBlock() && resultSizeWithMaxContractRet >
        Constant.MAX_RESULT_SIZE_IN_TX * contracts.size()) {
      throw new TooBigTransactionResultException(String.format(
          "Too big transaction result, TxId %s, the result size is %d bytes, maxResultSize %d",
          trx.getTransactionId(), resultSizeWithMaxContractRet, Constant.MAX_RESULT_SIZE_IN_TX));
    }
    if (trx.getResultSerializedSize() > Constant.MAX_RESULT_SIZE_IN_TX * contracts.size()) {
      throw new TooBigTransactionResultException();
    }

    long bytesSize;

    if (chainBaseManager.getDynamicPropertiesStore().supportVM()) {
      bytesSize = trx.getInstance().toBuilder().clearRet().build().getSerializedSize();
    } else {
      bytesSize = trx.getSerializedSize();
    }

    for (Contract contract : contracts) {
      if (contract.getType() == ShieldedTransferContract) {
        continue;
      }
      if (chainBaseManager.getDynamicPropertiesStore().supportVM()) {
        bytesSize += Constant.MAX_RESULT_SIZE_IN_TX;
      }

      logger.debug("TxId {}, bandwidth cost: {}.", trx.getTransactionId(), bytesSize);
      trace.setNetBill(bytesSize, 0);
      byte[] address = TransactionCapsule.getOwner(contract);
      AccountCapsule accountCapsule = chainBaseManager.getAccountStore().get(address);
      if (accountCapsule == null) {
        throw new ContractValidateException(String.format("account [%s] does not exist",
            StringUtil.encode58Check(address)));
      }
      long now = chainBaseManager.getHeadSlot();
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

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/AssetUtil.java (L46-52)
```java
  public static Account importAllAsset(Account account) {
    if (!isAllowAssetOptimization()) {
      return account;
    }
    Map<String, Long> map = accountAssetStore.getAllAssets(account);
    return account.toBuilder().clearAssetV2().putAllAssetV2(map).build();
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
