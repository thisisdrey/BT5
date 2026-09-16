### Title
Free, Unbounded Growth of Persistent DelegatedResourceStore/DelegatedResourceAccountIndexStore Entries via Zero-Fee DelegateResourceContract - (File: actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java)

### Summary
`DelegateResourceActuator.calcFee()` always returns `0`, meaning the `DelegateResourceContract` (broadcastable by any account holding frozen `FrozenV2` balance) charges no protocol fee for creating a new permanent state record. [1](#0-0)  Each call with a distinct `receiverAddress` creates a brand-new key in `DelegatedResourceStore` and appends entries in `DelegatedResourceAccountIndexStore`, with no cap on the number of distinct delegation relationships an account may create — unlike the `MarketSellAssetActuator`, which explicitly enforces `MAX_ACTIVE_ORDER_NUM` per account. [2](#0-1) 

### Finding Description
`DelegateResourceActuator.execute()` calls the private `delegateResource()` helper, which unconditionally constructs a `DelegatedResourceCapsule` keyed by `(ownerAddress, receiverAddress, lock)` and persists it, then updates `DelegatedResourceAccountIndexStore` for both the owner and the receiver via `delegateV2()`. [3](#0-2)  The only validation constraint on the number of distinct keys is that `delegateBalance` must be `>= 1 TRX`, and the sum of all delegated amounts must not exceed the account's `FrozenV2BalanceForBandwidth`/`FrozenV2BalanceForEnergy`. [4](#0-3)  There is no limit analogous to `MAX_ACTIVE_ORDER_NUM` restricting how many *distinct* receiver addresses a single owner can delegate to — an owner can split its frozen balance into as many 1-TRX chunks as it likes, each directed at a unique, previously unused receiver address, producing one new `DelegatedResourceCapsule` key and two new `DelegatedResourceAccountIndexCapsule` records per chunk.

Because `calcFee()` returns `0`, and non-locked delegations (`lock=false`) set `expireTime = 0` (i.e., never auto-expire) as seen in `delegateResource()`, these records persist in the chain state indefinitely until the owner explicitly issues an `UnDelegateResourceContract`. [5](#0-4)  The `DelegatedResourceAccountIndexStore` legacy (V1) format stores the full list of delegatees inside a single protobuf record per address and re-serializes/rewrites the entire growing list on every new relationship (`FreezeBalanceActuator.delegateResource()` shows the same list-append pattern), which the codebase itself acknowledges is problematic since it introduced a V2 prefix-keyed scheme (`supportAllowDelegateOptimization`) specifically to avoid the unbounded single-record growth. [6](#0-5) 

This mirrors the CVE-2015-5143 bug class: unauthenticated/low-cost operations create persistent server-side store records keyed by attacker-chosen unique identifiers, with no fee proportional to the storage consumed and no automatic expiration, allowing storage exhaustion (CWE-770).

### Impact Explanation
A transaction broadcaster who freezes even a modest amount of TRX for bandwidth/energy (`FreezeBalanceV2Contract`) can repeatedly broadcast `DelegateResourceContract` transactions to a large number of distinct, cheaply-created receiver accounts, each 1 TRX delegation permanently growing `DelegatedResourceStore` and `DelegatedResourceAccountIndexStore` with zero protocol fee (`calcFee() == 0`) — the attacker only pays ordinary bandwidth/net cost per transaction, not a fee scaled to the amount of persistent state created. Sustained abuse inflates the on-chain state database size (impacting all full nodes' disk usage, snapshot/backup times, and sync performance) without any compensating cost to the attacker, since the underlying TRX is never spent (only temporarily locked and later reclaimable via `UnDelegateResourceContract` and `UnfreezeBalanceV2Contract`).

### Likelihood Explanation
Any account can broadcast `AccountCreateContract` and `FreezeBalanceV2Contract` transactions to prepare many receiver addresses and frozen balance, then broadcast large numbers of `DelegateResourceContract` transactions — all standard, permissionless, low-privilege operations requiring no special roles (no SR/witness/committee privilege), making the attack straightforward to script and repeat.

### Recommendation
- Introduce a per-account cap on the number of distinct outstanding delegation relationships analogous to `MAX_ACTIVE_ORDER_NUM` for market orders.
- Charge a non-zero, storage-proportional fee in `DelegateResourceActuator.calcFee()` (or a dedicated per-new-key fee similar to `getCreateNewAccountFeeInSystemContract()`), so that creating a persistent state record has a real, sustained protocol cost, not just a temporary balance lock.
- Consider requiring/incentivizing expiration (via `lock`/`lockPeriod`) for all delegations rather than allowing indefinite `expireTime = 0` entries, and/or periodically pruning delegation relationships with negligible amounts.

### Proof of Concept
1. Create N receiver accounts via `AccountCreateContract` (cheap, standard flow).
2. Freeze `N * 1 TRX` (plus overhead) via `FreezeBalanceV2Contract` for BANDWIDTH.
3. For each of the N receiver accounts, broadcast a `DelegateResourceContract` with `balance = 1 TRX`, `lock = false`, pointing to a unique `receiverAddress`. Each call succeeds through `DelegateResourceActuator.validate()`/`execute()` with `calcFee() == 0`. [7](#0-6) 
4. Observe N new entries created in `DelegatedResourceStore` (unique key per receiver) and 2N entries in `DelegatedResourceAccountIndexStore`, all persisting with no expiration (`expireTime = 0`) and no protocol fee charged, confirmed by the existing test `testDelegateResourceForBandwidth`/`testMultiFreezeDelegatedBalanceForBandwidth` patterns that already exercise this exact code path at small scale. [8](#0-7)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L44-98)
```java
  @Override
  public boolean execute(Object result) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) result;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    final DelegateResourceContract delegateResourceContract;
    AccountStore accountStore = chainBaseManager.getAccountStore();
    byte[] ownerAddress;
    try {
      delegateResourceContract = this.any.unpack(DelegateResourceContract.class);
      ownerAddress = getOwnerAddress().toByteArray();
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    AccountCapsule ownerCapsule = accountStore
        .get(delegateResourceContract.getOwnerAddress().toByteArray());
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    long delegateBalance = delegateResourceContract.getBalance();
    boolean lock = delegateResourceContract.getLock();
    long lockPeriod = getLockPeriod(dynamicStore.supportMaxDelegateLockPeriod(),
            delegateResourceContract);
    byte[] receiverAddress = delegateResourceContract.getReceiverAddress().toByteArray();

    // delegate resource to receiver
    switch (delegateResourceContract.getResource()) {
      case BANDWIDTH:
        delegateResource(ownerAddress, receiverAddress, true,
            delegateBalance, lock, lockPeriod);

        ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth(delegateBalance);
        ownerCapsule.addFrozenBalanceForBandwidthV2(-delegateBalance);
        break;
      case ENERGY:
        delegateResource(ownerAddress, receiverAddress, false,
            delegateBalance, lock, lockPeriod);

        ownerCapsule.addDelegatedFrozenV2BalanceForEnergy(delegateBalance);
        ownerCapsule.addFrozenBalanceForEnergyV2(-delegateBalance);
        break;
      default:
        logger.debug("Resource Code Error.");
    }

    accountStore.put(ownerCapsule.createDbKey(), ownerCapsule);

    ret.setStatus(fee, code.SUCESS);

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L146-169)
```java

    long delegateBalance = delegateResourceContract.getBalance();
    if (delegateBalance < TRX_PRECISION) {
      throw new ContractValidateException("delegateBalance must be greater than or equal to 1 TRX");
    }

    switch (delegateResourceContract.getResource()) {
      case BANDWIDTH: {
        BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);
        processor.updateUsageForDelegated(ownerCapsule);

        long accountNetUsage = ownerCapsule.getNetUsage();
        if (null != this.getTx() && this.getTx().isTransactionCreate()) {
          accountNetUsage += TransactionUtil.estimateConsumeBandWidthSize(dynamicStore,
                  ownerCapsule.getFrozenV2BalanceForBandwidth());
        }
        long netUsage = (long) (accountNetUsage * TRX_PRECISION * ((double)
            (dynamicStore.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));
        long v2NetUsage = getV2NetUsage(ownerCapsule, netUsage,
            this.disableJavaLangMath());
        if (ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance) {
          throw new ContractValidateException(
              "delegateBalance must be less than or equal to available FreezeBandwidthV2 balance");
        }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L277-280)
```java
  @Override
  public long calcFee() {
    return 0;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L282-325)
```java
  private void delegateResource(byte[] ownerAddress, byte[] receiverAddress, boolean isBandwidth,
                                long balance, boolean lock, long lockPeriod) {
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicPropertiesStore = chainBaseManager.getDynamicPropertiesStore();
    DelegatedResourceStore delegatedResourceStore = chainBaseManager.getDelegatedResourceStore();
    DelegatedResourceAccountIndexStore delegatedResourceAccountIndexStore = chainBaseManager
        .getDelegatedResourceAccountIndexStore();

    // 1. unlock the expired delegate resource
    long now = chainBaseManager.getDynamicPropertiesStore().getLatestBlockHeaderTimestamp();
    delegatedResourceStore.unLockExpireResource(ownerAddress, receiverAddress, now);

    //modify DelegatedResourceStore
    long expireTime = 0;
    if (lock) {
      expireTime = now + lockPeriod * BLOCK_PRODUCED_INTERVAL;
    }
    byte[] key = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, lock);
    DelegatedResourceCapsule delegatedResourceCapsule = delegatedResourceStore.get(key);
    if (delegatedResourceCapsule == null) {
      delegatedResourceCapsule = new DelegatedResourceCapsule(ByteString.copyFrom(ownerAddress),
          ByteString.copyFrom(receiverAddress));
    }

    if (isBandwidth) {
      delegatedResourceCapsule.addFrozenBalanceForBandwidth(balance, expireTime);
    } else {
      delegatedResourceCapsule.addFrozenBalanceForEnergy(balance, expireTime);
    }
    delegatedResourceStore.put(key, delegatedResourceCapsule);

    //modify DelegatedResourceAccountIndexStore
    delegatedResourceAccountIndexStore.delegateV2(ownerAddress, receiverAddress,
        dynamicPropertiesStore.getLatestBlockHeaderTimestamp());

    //modify AccountStore for receiver
    AccountCapsule receiverCapsule = accountStore.get(receiverAddress);
    if (isBandwidth) {
      receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForBandwidth(balance);
    } else {
      receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForEnergy(balance);
    }
    accountStore.put(receiverCapsule.createDbKey(), receiverCapsule);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L232-239)
```java
    // check order num
    MarketAccountOrderCapsule marketAccountOrderCapsule = marketAccountStore
        .getUnchecked(ownerAddress);
    if (marketAccountOrderCapsule != null
        && marketAccountOrderCapsule.getCount() >= MAX_ACTIVE_ORDER_NUM) {
      throw new ContractValidateException(
          "Maximum number of orders exceeded，" + MAX_ACTIVE_ORDER_NUM);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L319-353)
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

    } else {
      // modify DelegatedResourceAccountIndexStore new
      delegatedResourceAccountIndexStore.convert(ownerAddress);
      delegatedResourceAccountIndexStore.convert(receiverAddress);
      delegatedResourceAccountIndexStore.delegate(ownerAddress, receiverAddress,
          dynamicPropertiesStore.getLatestBlockHeaderTimestamp());
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/DelegateResourceActuatorTest.java (L316-374)
```java
  @Test
  public void testDelegateResourceForBandwidth() {
    freezeBandwidthForOwner();
    long delegateBalance = 1_000_000_000L;
    DelegateResourceActuator actuator = new DelegateResourceActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(
        getDelegateContractForBandwidth(OWNER_ADDRESS, RECEIVER_ADDRESS, delegateBalance));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    long totalNetWeightBefore = dbManager.getDynamicPropertiesStore().getTotalNetWeight();
    byte[] owner = ByteArray.fromHexString(OWNER_ADDRESS);
    byte[] receiver = ByteArray.fromHexString(RECEIVER_ADDRESS);
    try {
      actuator.validate();
      actuator.execute(ret);
      assertEquals(code.SUCESS, ret.getInstance().getRet());
      AccountCapsule ownerCapsule =
          dbManager.getAccountStore().get(owner);

      assertEquals(delegateBalance, ownerCapsule.getDelegatedFrozenV2BalanceForBandwidth());
      assertEquals(initBalance - delegateBalance,
          ownerCapsule.getFrozenV2BalanceForBandwidth());
      assertEquals(initBalance, ownerCapsule.getTronPower());

      AccountCapsule receiverCapsule =
          dbManager.getAccountStore().get(receiver);
      assertEquals(delegateBalance,
          receiverCapsule.getAcquiredDelegatedFrozenV2BalanceForBandwidth());
      assertEquals(0L, receiverCapsule.getAcquiredDelegatedFrozenV2BalanceForEnergy());
      assertEquals(0L, receiverCapsule.getTronPower());

      DelegatedResourceCapsule delegatedResourceCapsule = dbManager.getDelegatedResourceStore()
          .get(DelegatedResourceCapsule
              .createDbKeyV2(ByteArray.fromHexString(OWNER_ADDRESS),
                  ByteArray.fromHexString(RECEIVER_ADDRESS), false));

      assertEquals(delegateBalance, delegatedResourceCapsule.getFrozenBalanceForBandwidth());
      long totalNetWeightAfter = dbManager.getDynamicPropertiesStore().getTotalNetWeight();
      assertEquals(totalNetWeightBefore, totalNetWeightAfter);

      //check DelegatedResourceAccountIndex
      DelegatedResourceAccountIndexCapsule ownerIndexCapsule = dbManager
          .getDelegatedResourceAccountIndexStore().getV2Index(owner);
      assertEquals(0, ownerIndexCapsule.getFromAccountsList().size());
      assertEquals(1, ownerIndexCapsule.getToAccountsList().size());
      assertTrue(ownerIndexCapsule.getToAccountsList()
          .contains(ByteString.copyFrom(ByteArray.fromHexString(RECEIVER_ADDRESS))));

      DelegatedResourceAccountIndexCapsule receiveCapsule = dbManager
          .getDelegatedResourceAccountIndexStore().getV2Index(receiver);
      assertEquals(0, receiveCapsule.getToAccountsList().size());
      assertEquals(1, receiveCapsule.getFromAccountsList().size());
      assertTrue(receiveCapsule.getFromAccountsList()
          .contains(ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS))));

    } catch (ContractValidateException | ContractExeException e) {
      fail(e.getMessage());
    }
  }
```
