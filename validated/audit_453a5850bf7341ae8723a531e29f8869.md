Found it — `BandwidthProcessor.updateUsage(AccountCapsule)` is a strong, currently-reachable analog to the dMute `RedeemTo`/`GetUnderlyingTokens` unbounded-array DoS: an attacker can grow a victim's `assetV2` map without bound, and that map is *fully iterated* on **every** bandwidth-consuming transaction the victim ever submits — a direct parallel to the original bug's pattern (attacker-grown per-account collection + mandatory full iteration in a critical, funds-adjacent path).

### Title
Attacker-inflated `assetV2`/`freeAssetNetUsageV2` map on a victim account causes unbounded iteration in `BandwidthProcessor.updateUsage`, degrading/blocking normal transaction processing for that account - (File: `chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java`)

### Summary
`BandwidthProcessor.updateUsage(AccountCapsule accountCapsule)` iterates over the account's full `assetV2` map (merged with `freeAssetNetUsageV2` map) on every bandwidth-relevant transaction: `chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java:66-78`. Any unprivileged account can grow another account's `assetV2` map by issuing a new TRC10 token and sending a `TransferAssetContract` naming that new token to the victim, because `TransferAssetActuator.execute()` unconditionally credits the receiver via `toAccountCapsule.addAssetAmountV2(...)` with no permission check on the receiver (`actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java:82-84`), which in turn calls `AccountCapsule.addAssetAmountV2` and `putAssetV2(tokenID, ...)` (`chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java:733-763`), unconditionally inserting a brand-new key into the victim's map for every distinct token id. This mirrors the original dMute bug class: an attacker-controlled push operation into another user's persistent collection, followed by mandatory, unbounded iteration of that collection in a path the victim depends on.

### Finding Description
- Root cause 1 (attacker-controlled growth): `TransferAssetActuator.execute()` credits `toAddress`'s `assetV2` map for any valid, existing asset id with no ownership/whitelist check on the receiver [1](#0-0) . `AccountCapsule.addAssetAmountV2` inserts a new map entry keyed by token id whenever the key doesn't already exist [2](#0-1) .
- Root cause 2 (mandatory full iteration): `BandwidthProcessor.updateUsage(AccountCapsule)` builds `map` from the account's entire `assetV2` map plus all `freeAssetNetUsageV2` entries, then iterates every entry with `map.forEach(...)`, performing a map lookup/write per entry, on every transaction that consumes bandwidth for that account [3](#0-2) . This method is invoked from the bandwidth-consumption path used for ordinary transfers (`consume`, `useAccountNet`/`useFreeNet` call trees) which every transaction from that account must go through [4](#0-3) .
- Any account issuing many distinct TRC10 assets (`AssetIssueActuator`, cost is a fixed system fee, not scaled to entries added later) and sending 1 unit of each to a victim address grows the victim's `assetV2` map linearly with the number of transactions the attacker is willing to send, at a cost of only the transfer bandwidth/fee for TransferAssetContract (no proportional cost tied to the size of the resulting victim-side map) [5](#0-4) .
- Unlike the `UnFreezeV2`/`FreezeV2` lists, there is no cap comparable to `UNFREEZE_MAX_TIMES` that bounds the number of distinct assets held by an account.

### Impact Explanation
Every transaction the victim submits that touches bandwidth accounting re-iterates the full, attacker-inflated map inside `updateUsage`, and a very similar full map construction/iteration also occurs in `useAssetAccountNet` for `latestAssetOperationTime`/`freeAssetNetUsage` handling [6](#0-5) . As the map grows unbounded, CPU cost per transaction from the victim's account grows linearly, degrading the node's ability to timely process that account's transactions block-after-block — a persistent, attacker-imposed processing tax on a specific victim that mirrors the "redemption blocked forever" impact class from the original report (mandatory iteration of an attacker-grown collection in a path the victim cannot avoid). Because Java-tron actuators are not gas-metered per opcode (unlike the EVM contract in the original report), this does not cause an outright revert/failure by itself, but it is a genuine, unbounded resource-consumption vector directly reachable by any unprivileged account against any other account, with the potential to materially slow down processing of the victim's transactions across the whole node.

### Likelihood Explanation
High reachability: `TransferAssetContract` and `AssetIssueContract` are ordinary, unprivileged, broadcastable transaction types requiring no special permission on the receiver side. The only cost to the attacker is issuing new assets (a fixed system fee per asset) and paying ordinary bandwidth/fee for the transfer transactions; there is no cost scaling with how large the victim's resulting map becomes. This can be repeated indefinitely by a single attacker account (or sybil accounts issuing many assets) against any target address on the network.

### Recommendation
- Bound the number of distinct TRC10 assets a single account may hold (analogous to `UNFREEZE_MAX_TIMES` for `UnFreezeV2`), and validate this bound in `TransferAssetActuator.validate()` before allowing a new asset entry to be created for the receiver.
- Alternatively/additionally, avoid rebuilding/iterating the entire asset map on every bandwidth consumption; track and update only the assets referenced by the current transaction, and lazily migrate any legacy full-map bookkeeping (similar to how `AccountAssetStore`/`importAsset` already externalizes per-key balances) so `updateUsage` no longer needs `map.forEach` over the full account asset set.
- Consider charging a per-new-asset-holder fee or bandwidth cost proportional to the number of distinct assets already held by the receiver, to make growing another account's map economically costly for the attacker.

### Proof of Concept
1. Attacker account `A` issues `N` distinct TRC10 tokens via `AssetIssueContract` (paying the standard system asset-issuance fee for each).
2. For each of the `N` tokens, `A` sends `TransferAssetContract(assetName=token_i, amount=1, toAddress=Victim)`.
3. Each transfer succeeds via `TransferAssetActuator.execute()` and calls `Victim.addAssetAmountV2(token_i, 1, ...)`, inserting a new key into `Victim`'s `assetV2` map [2](#0-1) .
4. After repeating for thousands of distinct tokens, any subsequent transaction submitted by `Victim` triggers `BandwidthProcessor.updateUsage(Victim)`, which performs `map.forEach` over the now-large merged asset map [3](#0-2) , incurring CPU/time cost proportional to the number of assets the attacker injected, on every one of the victim's future transactions.

**Uncertainty note:** I was not able to fully verify, within available tool budget, whether any additional global/per-account cap on distinct TRC10 asset holdings exists elsewhere in validation logic (e.g., in `AssetIssueActuator.validate()` or account-creation limits) that might already bound this vector, nor did I measure the actual wall-clock/CPU cost of `map.forEach` at scale to determine the practical entry count needed to produce a "High" vs. "Medium" severity DoS. A Devin session with full repo/test access could confirm whether such a cap exists and benchmark the realistic impact.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L72-84)
```java
      ByteString assetName = transferAssetContract.getAssetName();
      long amount = transferAssetContract.getAmount();

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

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L733-763)
```java
  public boolean addAssetAmountV2(byte[] key, long amount,
      DynamicPropertiesStore dynamicPropertiesStore, AssetIssueStore assetIssueStore) {
    importAsset(key);
    boolean disableJavaLangMath = dynamicPropertiesStore.disableJavaLangMath();
    //key is token name
    if (dynamicPropertiesStore.getAllowSameTokenName() == 0) {
      Map<String, Long> assetMap = this.account.getAssetMap();
      AssetIssueCapsule assetIssueCapsule = assetIssueStore.get(key);
      String tokenID = assetIssueCapsule.getId();
      String nameKey = ByteArray.toStr(key);
      Long currentAmount = assetMap.get(nameKey);
      if (currentAmount == null) {
        currentAmount = 0L;
      }
      this.account = this.account.toBuilder()
          .putAsset(nameKey, addExact(currentAmount, amount, disableJavaLangMath))
          .putAssetV2(tokenID, addExact(currentAmount, amount, disableJavaLangMath))
          .build();
    }
    //key is token id
    if (dynamicPropertiesStore.getAllowSameTokenName() == 1) {
      String tokenIDStr = ByteArray.toStr(key);
      Map<String, Long> assetMapV2 = this.account.getAssetV2Map();
      Long currentAmount = assetMapV2.get(tokenIDStr);
      if (currentAmount == null) {
        currentAmount = 0L;
      }
      this.account = this.account.toBuilder()
          .putAssetV2(tokenIDStr, addExact(currentAmount, amount, disableJavaLangMath))
          .build();
    }
```

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L47-79)
```java
  public void updateUsage(AccountCapsule accountCapsule) {
    long now = chainBaseManager.getHeadSlot();
    long oldNetUsage = accountCapsule.getNetUsage();
    long latestConsumeTime = accountCapsule.getLatestConsumeTime();
    accountCapsule.setNetUsage(increase(accountCapsule, BANDWIDTH,
            oldNetUsage, 0, latestConsumeTime, now));
    long oldFreeNetUsage = accountCapsule.getFreeNetUsage();
    long latestConsumeFreeTime = accountCapsule.getLatestConsumeFreeTime();
    accountCapsule.setFreeNetUsage(increase(oldFreeNetUsage, 0, latestConsumeFreeTime, now));

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

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L331-343)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L55-122)
```java
  @Override
  public boolean execute(Object result) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) result;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    AssetIssueStore assetIssueStore = chainBaseManager.getAssetIssueStore();
    AssetIssueV2Store assetIssueV2Store = chainBaseManager.getAssetIssueV2Store();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    try {
      AssetIssueContract assetIssueContract = any.unpack(AssetIssueContract.class);
      byte[] ownerAddress = assetIssueContract.getOwnerAddress().toByteArray();
      AssetIssueCapsule assetIssueCapsule = new AssetIssueCapsule(assetIssueContract);
      AssetIssueCapsule assetIssueCapsuleV2 = new AssetIssueCapsule(assetIssueContract);
      long tokenIdNum = dynamicStore.getTokenIdNum();
      tokenIdNum++;
      assetIssueCapsule.setId(Long.toString(tokenIdNum));
      assetIssueCapsuleV2.setId(Long.toString(tokenIdNum));
      dynamicStore.saveTokenIdNum(tokenIdNum);

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
      accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
          .addAllFrozenSupply(frozenList).build());

      accountStore.put(ownerAddress, accountCapsule);
```
