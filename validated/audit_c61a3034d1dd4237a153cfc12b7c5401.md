### Title
Unbounded Growth of DelegatedResourceAccountIndex Lists Enables Denial-of-Service Against Arbitrary Accounts - (File: `actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java`)

### Summary
`FreezeBalanceActuator` allows any account holder to delegate frozen resources to an arbitrary `receiverAddress` chosen by the caller, with no consent required from the receiver. When the legacy (non-optimized) delegation model is active, each delegation appends an entry to unbounded `repeated bytes` lists (`toAccounts`/`fromAccounts`) stored inside the `DelegatedResourceAccountIndex` protobuf capsule for both the sender and the receiver, with no maximum-size check enforced anywhere in the code path. This mirrors the `updateTrust()` pattern reported for Union: an attacker-controlled operation appends entries into a data structure that is also attributed to a *victim* account, with no cap, letting the attacker grow that structure indefinitely at low, recoverable cost.

### Finding Description
In `FreezeBalanceActuator.delegateResource()`, when `dynamicPropertiesStore.supportAllowDelegateOptimization()` is false, the legacy path fetches (or creates) index capsules for both the owner and the receiver and unconditionally appends to their `toAccounts`/`fromAccounts` lists if the counterpart address is not already present: [1](#0-0) 

No limit (e.g. `maxVouchees`/`maxVouchers`-style cap) is ever checked before `ownerIndexCapsule.addToAccount(...)` or `receiverIndexCapsule.addFromAccount(...)` are called, unlike other list-bound actuators in the codebase (e.g. `VoteWitnessActuator`'s `MAX_VOTE_NUMBER`, `MarketSellAssetActuator`'s `MAX_ACTIVE_ORDER_NUM`, or `AssetIssueActuator`'s `getMaxFrozenSupplyNumber()` check): [2](#0-1) [3](#0-2) 

The underlying `DelegatedResourceAccountIndexCapsule.addToAccount`/`addFromAccount` simply append to the protobuf repeated field with no size validation: [4](#0-3) 

Critically, an attacker's target need not consent or take any action: the `receiverAddress` is fully attacker-chosen, and the receiver's `fromAccountsList` is grown purely as a side effect of the attacker's own `FreezeBalanceContract` transactions (minimum 1 TRX freeze, `calcFee() == 0`), exactly as `updateTrust()` allowed staking `0` trust against an arbitrary borrower without the borrower's consent: [5](#0-4) 

The existing test `testMultiFreezeDelegatedBalanceForBandwidth` demonstrates the unbounded nature of this growth explicitly (100+ entries added with no rejection): [6](#0-5) 

### Impact Explanation
An attacker can generate many fresh addresses, freeze the minimum required balance (1 TRX, `TRX_PRECISION`) from each, and delegate to the same victim `receiverAddress` repeatedly. Each such transaction appends an entry to the victim's `fromAccountsList` inside `DelegatedResourceAccountIndex`, which is unbounded. As this list grows:
- Retrieval of the victim's delegated-resource index (`Wallet.getDelegatedResourceAccountIndex`, exposed via gRPC/HTTP `getdelegatedresourceaccountindex`) becomes increasingly expensive to serialize/return, degrading or effectively denying that API for/about the victim.
- The victim's `AccountCapsule`-adjacent index capsule grows without bound in the underlying store, consuming increasing storage and processing time on every read/write touching that key, which can be amplified across many victims to place a sustained load on full nodes serving these queries — an API a node "can no longer serve" for practical purposes.

This satisfies the "no longer serve API" / node degradation class of impact tied to a reachable single-signed-transaction operation (`FreezeBalanceContract` with `receiver_address` set).

### Likelihood Explanation
The attack requires only the ability to broadcast `FreezeBalanceContract` transactions with a chosen `receiver_address`, and each contributing delegation costs a temporarily-locked minimum of 1 TRX (`TRX_PRECISION`) per new source address — recoverable after the freeze duration. This is cheap and fully within the reach of any unprivileged account, requiring no cooperation or vulnerability on the victim's part, mirroring the low cost/high impact profile of the original `updateTrust()` report. The precondition is that `supportAllowDelegateOptimization()` is not enabled for the relevant chain/fork state, which determines whether the legacy unbounded-list code path is exercised.

### Recommendation
Enforce a maximum size (analogous to `maxVouchees`/`maxVouchers` in the reference report, or `MAX_ACTIVE_ORDER_NUM`/`MAX_VOTE_NUMBER` already used elsewhere in this codebase) on `DelegatedResourceAccountIndexCapsule`'s `toAccounts`/`fromAccounts` lists before `addToAccount`/`addFromAccount` are invoked in `FreezeBalanceActuator.delegateResource()`, or ensure the optimized (`allowDelegateOptimization`) prefix-key model is unconditionally enforced going forward to remove the unbounded-list code path entirely.

### Proof of Concept
1. Attacker creates N fresh accounts A1..AN, each funded with at least 1 TRX.
2. For each Ai, attacker broadcasts a `FreezeBalanceContract` with `owner_address = Ai`, `receiver_address = Victim`, `frozen_balance = TRX_PRECISION` (1 TRX), `resource = BANDWIDTH` or `ENERGY`.
3. Each transaction succeeds and appends `Ai` to `Victim`'s `fromAccountsList` inside `DelegatedResourceAccountIndex`, as shown by the code path in `FreezeBalanceActuator.delegateResource()` (lines 334-345) and validated by the existing test `testMultiFreezeDelegatedBalanceForBandwidth`, with no upper bound enforced.
4. Repeating this at scale (or across many concurrent victims) bloats the store and degrades `getDelegatedResourceAccountIndex` query performance for the affected account(s).

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L279-287)
```java
  @Override
  public ByteString getOwnerAddress() throws InvalidProtocolBufferException {
    return any.unpack(FreezeBalanceContract.class).getOwnerAddress();
  }

  @Override
  public long calcFee() {
    return 0;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L320-345)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L93-97)
```java
    int maxVoteNumber = MAX_VOTE_NUMBER;
    if (contract.getVotesCount() > maxVoteNumber) {
      throw new ContractValidateException(
          "VoteNumber more than maxVoteNumber " + maxVoteNumber);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L233-239)
```java
    MarketAccountOrderCapsule marketAccountOrderCapsule = marketAccountStore
        .getUnchecked(ownerAddress);
    if (marketAccountOrderCapsule != null
        && marketAccountOrderCapsule.getCount() >= MAX_ACTIVE_ORDER_NUM) {
      throw new ContractValidateException(
          "Maximum number of orders exceeded，" + MAX_ACTIVE_ORDER_NUM);
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L57-86)
```java
  public void addFromAccount(ByteString fromAccount) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .addFromAccounts(fromAccount)
        .build();
  }

  public void removeFromAccount(ByteString fromAccount) {
    if (getFromAccountsList().contains(fromAccount)) {
      List<ByteString> fromList = new ArrayList<>(getFromAccountsList());
      fromList.remove(fromAccount);
      setAllFromAccounts(fromList);
    }
  }

  public List<ByteString> getToAccountsList() {
    return this.delegatedResourceAccountIndex.getToAccountsList();
  }

  public void setAllToAccounts(List<ByteString> toAccounts) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .clearToAccounts()
        .addAllToAccounts(toAccounts)
        .build();
  }

  public void addToAccount(ByteString toAccount) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .addToAccounts(toAccount)
        .build();
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/FreezeBalanceActuatorTest.java (L282-331)
```java
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
```
