### Title
Permissionless delegation lets any attacker grow another account's `DelegatedResourceAccountIndex` list without bound - ([File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java])

### Summary
`DelegateResourceContract`/`FreezeBalanceContract` (delegated-resource path) let *any* caller add entries to a *third‑party* (receiver) account's `DelegatedResourceAccountIndex` `fromAccounts`/`toAccounts` list by simply delegating ≥1 TRX of bandwidth/energy to that receiver. There is no cap on the number of distinct entries that can be pushed into a single account's index list in the legacy (non-optimized) storage path, and no permission check restricting who may write into another account's index. This mirrors the OpenQ bug class: an unprivileged, low-cost transaction fills a shared list belonging to another party, with no size limit enforced at the point of insertion.

### Finding Description
When `dynamicPropertiesStore.supportAllowDelegateOptimization()` is false, `delegateResource()` appends the caller's address to the receiver's `fromAccountsList` (and vice versa) inside a single `DelegatedResourceAccountIndexCapsule` protobuf object keyed by the receiver's address: [1](#0-0) 

The append operation performs only a linear `contains()` de-duplication check and calls `addFromAccount`/`addToAccount` with no upper bound on list size: [2](#0-1) 

Any address can be the `ownerAddress` (caller/attacker) as long as it owns an account with ≥1 TRX frozen for bandwidth/energy; the receiver need not consent or even be aware: [3](#0-2) [4](#0-3) 

A test explicitly exercises unbounded growth of this list (100+ distinct receiver addresses appended to one owner's `toAccountsList` with no rejection): [5](#0-4) 

By creating many low-cost accounts (each freezing the 1-TRX minimum) and delegating to the same victim receiver address repeatedly, an attacker can inflate a *single* protobuf-encoded database value (`DelegatedResourceAccountIndex` for the victim) without limit. Because this object is read wholesale by `DelegatedResourceAccountIndexStore.get()`/`getIndex()` and returned via the `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` gRPC/HTTP APIs, an attacker-controlled, unbounded growth of this record can degrade or block those query paths for the affected account (large deserialization/response cost) and bloat chain state indefinitely, at very low cost to the attacker (repeated 1-TRX freezes across throwaway accounts).

### Impact Explanation
This does not directly cause fund theft, but it allows an unprivileged actor to unboundedly grow state tied to a victim account they do not control, at minimal cost, degrading a query API (`getDelegatedResourceAccountIndex`) for that specific account and inflating the chain database indefinitely. This matches the "API the node can no longer serve" / permanent state-bloat class described in the validation criteria for the targeted account, though it does not directly freeze existing funds.

### Likelihood Explanation
Likelihood is limited by two factors I could not fully confirm from the index:
1. The vulnerable code path is only reached `!dynamicPropertiesStore.supportAllowDelegateOptimization()`. I could not confirm from the available index whether this optimization flag defaults to enabled in the current mainnet chain parameters (the `DynamicPropertiesStore.java` default-initialization block for this specific flag was not found by search, so I cannot state with certainty whether the legacy unbounded-list path is reachable in production today).
2. Attack cost scales with the number of entries desired (one funded account + 1 TRX freeze per entry), so filling a very large list requires proportional TRX outlay, unlike the OpenQ case where fake ERC20 deployment is essentially free.

Given this uncertainty about whether the legacy path is currently active, I cannot assert this as a definitively exploitable Medium/High issue on the current network without further confirmation of the `AllowDelegateOptimization` default and rollout status. If the legacy path is disabled network-wide, the newer `delegate()`/`getWithPrefix()` V2 index (which computes lists on-demand from prefix-scanned keys) would need separate confirmation of whether it enforces any cap — this was not found in the reviewed code, so unbounded growth may also apply there.

### Recommendation
- Enforce a maximum size on `fromAccounts`/`toAccounts` (and the V2 prefix-scanned equivalent) per account, rejecting further `DelegateResourceContract` calls once a receiver's index list reaches the cap, or
- Require the resource delegation index update to be independent of an attacker-chosen receiver by decoupling index-size growth from a single writable list, and
- Confirm and, if necessary, force-enable `AllowDelegateOptimization` network-wide, and verify the optimized index path also enforces a bound on per-account entries returned by `getWithPrefix`.

### Proof of Concept
1. Confirm on the target network whether `supportAllowDelegateOptimization()` is false (legacy path active).
2. Attacker creates N throwaway accounts, each funded with the minimum TRX to freeze 1 TRX of bandwidth via `FreezeBalanceV2Contract`.
3. From each throwaway account, attacker issues a `DelegateResourceContract` with `receiverAddress` = victim's address and `balance` = 1 TRX (minimum allowed per `DelegateResourceActuator.validate`, `DelegateResourceActuator.java:147-150`).
4. Each call appends a new unique entry to the victim's `DelegatedResourceAccountIndexCapsule.fromAccountsList` via `FreezeBalanceActuator.delegateResource` (`FreezeBalanceActuator.java:319-345`), which is unbounded (`DelegatedResourceAccountIndexCapsule.java:46-61`).
5. Repeating this across many throwaway accounts causes the victim's index record to grow without limit, degrading `getDelegatedResourceAccountIndex` queries for that account and permanently bloating state, at a total cost proportional to N × 1 TRX (recoverable by the attacker later via `UnfreezeBalanceV2`/`UnDelegateResource`, making the attack largely capital-neutral aside from resource costs and fees).

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L319-345)
```java
    //modify DelegatedResourceAccountIndexStore
    if (!dynamicPropertiesStore.supportAllowDelegateOptimization()) {

      DelegatedResourceAccountIndexCapsule ownerIndexCapsule =
          delegatedResourceAccountIndexStore.get(ownerAddress);
      if (ownerIndexCapsule == null) {
        ownerIndexCapsule = new DelegatedResourceAccountIndexCapsule(
            ByteString.copyFrom(ownerAddress));
      }
      List<ByteString> toAccountsList = ownerIndexCapsule.getToAccountsList();
      if (!toAccountsList.contains(ByteString.copyFrom(receiverAddress))) {
        ownerIndexCapsule.addToAccount(ByteString.copyFrom(receiverAddress));
      }
      delegatedResourceAccountIndexStore.put(ownerAddress, ownerIndexCapsule);

      DelegatedResourceAccountIndexCapsule receiverIndexCapsule
          = delegatedResourceAccountIndexStore.get(receiverAddress);
      if (receiverIndexCapsule == null) {
        receiverIndexCapsule = new DelegatedResourceAccountIndexCapsule(
            ByteString.copyFrom(receiverAddress));
      }
      List<ByteString> fromAccountsList = receiverIndexCapsule
          .getFromAccountsList();
      if (!fromAccountsList.contains(ByteString.copyFrom(ownerAddress))) {
        receiverIndexCapsule.addFromAccount(ByteString.copyFrom(ownerAddress));
      }
      delegatedResourceAccountIndexStore.put(receiverAddress, receiverIndexCapsule);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L46-61)
```java
  public List<ByteString> getFromAccountsList() {
    return this.delegatedResourceAccountIndex.getFromAccountsList();
  }

  public void setAllFromAccounts(List<ByteString> fromAccounts) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .clearFromAccounts()
        .addAllFromAccounts(fromAccounts)
        .build();
  }

  public void addFromAccount(ByteString fromAccount) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .addFromAccounts(fromAccount)
        .build();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L147-150)
```java
    long delegateBalance = delegateResourceContract.getBalance();
    if (delegateBalance < TRX_PRECISION) {
      throw new ContractValidateException("delegateBalance must be greater than or equal to 1 TRX");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L191-210)
```java
    byte[] receiverAddress = delegateResourceContract.getReceiverAddress().toByteArray();

    if (!DecodeUtil.addressValid(receiverAddress)) {
      throw new ContractValidateException("Invalid receiverAddress");
    }


    if (Arrays.equals(receiverAddress, ownerAddress)) {
      throw new ContractValidateException(
          "receiverAddress must not be the same as ownerAddress");
    }

    AccountCapsule receiverCapsule = accountStore.get(receiverAddress);
    if (receiverCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(receiverAddress);
      throw new ContractValidateException(
          ActuatorConstant.ACCOUNT_EXCEPTION_STR
              + readableOwnerAddress + NOT_EXIST_STR);
    }

```

**File:** framework/src/test/java/org/tron/core/actuator/FreezeBalanceActuatorTest.java (L275-334)
```java
  @Test
  public void testMultiFreezeDelegatedBalanceForBandwidth() {
    dbManager.getDynamicPropertiesStore().saveAllowDelegateResource(1);
    dbManager.getDynamicPropertiesStore().saveAllowDelegateOptimization(1L);
    dbManager.getDynamicPropertiesStore().saveLatestBlockHeaderTimestamp(10000L);
    long frozenBalance = 1_000_000_000L;
    long duration = 3;
    final int RECEIVE_COUNT = 100;
    String[] RECEIVE_ADDRESSES = new String[RECEIVE_COUNT + 1];

    DelegatedResourceAccountIndexCapsule ownerIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(
            ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS)));
    for (int i = 0; i < RECEIVE_COUNT + 1; i++) {
      ECKey ecKey = new ECKey(Utils.getRandom());
      RECEIVE_ADDRESSES[i] = ByteArray.toHexString(ecKey.getAddress());
      if (i != RECEIVE_COUNT) {
        ownerIndexCapsule.addToAccount(ByteString.copyFrom(ecKey.getAddress()));
      }
    }
    dbManager.getDelegatedResourceAccountIndexStore().put(
        ByteArray.fromHexString(OWNER_ADDRESS), ownerIndexCapsule);
    AccountCapsule receiverCapsule =
        new AccountCapsule(
            ByteString.copyFromUtf8("receiver"),
            ByteString.copyFrom(ByteArray.fromHexString(RECEIVE_ADDRESSES[RECEIVE_COUNT])),
            AccountType.Normal,
            initBalance);
    dbManager.getAccountStore().put(receiverCapsule.getAddress().toByteArray(), receiverCapsule);

    TransactionResultCapsule ret = new TransactionResultCapsule();
    FreezeBalanceActuator actuator = new FreezeBalanceActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getDelegatedContractForBandwidth(
            OWNER_ADDRESS, RECEIVE_ADDRESSES[RECEIVE_COUNT], frozenBalance, duration));
    try {
      ownerIndexCapsule = dbManager
          .getDelegatedResourceAccountIndexStore().getIndex(ByteArray.fromHexString(OWNER_ADDRESS));
      List<ByteString> beforeList = ownerIndexCapsule.getToAccountsList();
      actuator.validate();
      actuator.execute(ret);

      //check DelegatedResourceAccountIndex convert
      ownerIndexCapsule = dbManager
          .getDelegatedResourceAccountIndexStore().get(ByteArray.fromHexString(OWNER_ADDRESS));
      Assert.assertNull(ownerIndexCapsule);

      ownerIndexCapsule = dbManager
          .getDelegatedResourceAccountIndexStore().getIndex(ByteArray.fromHexString(OWNER_ADDRESS));
      Assert.assertEquals(0, ownerIndexCapsule.getFromAccountsList().size());
      List<ByteString> tmpList = ownerIndexCapsule.getToAccountsList();
      Assert.assertEquals(RECEIVE_COUNT + 1, ownerIndexCapsule.getToAccountsList().size());
      for (int i = 0; i < RECEIVE_COUNT; i++) {
        Assert.assertEquals(beforeList.get(i), tmpList.get(i));
      }
      Assert.assertEquals(RECEIVE_ADDRESSES[RECEIVE_COUNT],
          ByteArray.toHexString(tmpList.get(RECEIVE_COUNT).toByteArray()));
    } catch (ContractValidateException | ContractExeException e) {
      Assert.fail("con not reach here");
    }
```
