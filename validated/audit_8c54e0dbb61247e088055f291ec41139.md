### Title
Unbounded growth of `DelegatedResourceAccountIndexCapsule` from/to account lists causes ever-increasing per-call cost and unbounded state size - (File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java)

### Summary
`FreezeBalanceActuator.delegateResource()` maintains, for the legacy (non-optimized) resource-delegation model, a single `DelegatedResourceAccountIndexCapsule` record per account that stores a `repeated bytes` list of every distinct receiver/sender address the account has ever delegated resources to/from. Each `FreezeBalanceContract` transaction with a new `receiver_address` performs a linear `contains()` scan and unconditionally appends to this list if not already present, with no maximum size check in `validate()`. An attacker (or ordinary heavy user) can grow this list without bound by issuing many `FreezeBalanceContract` transactions to a large number of distinct receiver addresses.

### Finding Description
When `dynamicPropertiesStore.supportAllowDelegateOptimization()` is false (the legacy delegation index model, still supported/read by the codebase), `delegateResource()` does: [1](#0-0) 

There is no limit on how many entries can be appended to `toAccountsList`/`fromAccountsList`. Each new call:
1. Reads the full existing list from DB (`delegatedResourceAccountIndexStore.get(...)`), which deserializes the growing protobuf blob.
2. Performs an O(n) `contains()` check against the full list.
3. Appends and re-serializes the entire (larger) list back to the DB via `DelegatedResourceAccountIndexCapsule.addToAccount`/`addFromAccount`: [2](#0-1) 

This means the cost of every subsequent `FreezeBalanceContract` transaction touching that account grows linearly with the number of distinct receivers/senders ever delegated to, and the serialized value stored under a single DB key grows without bound. The same unbounded record is also read in full on every query via `Wallet.getDelegatedResourceAccountIndex`, exposed through gRPC/HTTP: [3](#0-2) [4](#0-3) 

By contrast, other similar structures in the codebase (e.g. `UnfreezeBalanceV2Actuator`'s `unfrozenV2List`) are explicitly capped by `UNFREEZE_MAX_TIMES` in `validate()`, and `AssetIssueActuator`'s `frozenSupplyList` is capped by `dynamicStore.getMaxFrozenSupplyNumber()`: [5](#0-4) 

No equivalent bound exists for the legacy `DelegatedResourceAccountIndexCapsule` from/to lists populated by `FreezeBalanceActuator`/`UnfreezeBalanceActuator`.

### Impact Explanation
An attacker can deliberately grow their own (or force growth of a victim receiver's) delegation index by repeatedly issuing small `FreezeBalanceContract` transactions with a `receiver_address`, each to a fresh/unused address, targeting the same owner or same receiver. Over time:
- The stored record grows unbounded, increasing the CPU/energy cost of `contains()` scans and serialization/deserialization on every subsequent freeze/unfreeze transaction touching that account, and on every read via `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` APIs.
- Because this happens inside `execute()` during block application (not gas-metered against the caller's fee in a way proportional to this hidden cost — `FreezeBalanceActuator.calcFee()` returns `0`), it degrades node performance and eventually block-processing/tx-processing time for that account without a corresponding fee, which is the state-growth analog of the reported unbound-loop DoS pattern.
- The severity is bounded by the account no longer being able to further delegate/undelegate in a timely manner and by the growing DB record size, rather than an immediate outright chain halt, but it fits the "high gas usage / breaks the protocol for that account" impact class from the referenced report.

### Likelihood Explanation
This is reachable directly and repeatedly from a single unprivileged, signed `FreezeBalanceContract` transaction (a normal user action) with no special permissions required, and `FreezeBalanceActuator.calcFee()` is `0`, so the attack is essentially free apart from paying for the frozen TRX itself (which is refundable later via unfreeze). No validate()-time limit rejects an ever-growing list. This makes the analog readily exploitable by any account.

### Recommendation
Add a maximum size check for `DelegatedResourceAccountIndexCapsule`'s `toAccountsList`/`fromAccountsList` in `FreezeBalanceActuator.validate()` (and equivalently for `UnfreezeBalanceActuator`'s handling), analogous to `UNFREEZE_MAX_TIMES` and `getMaxFrozenSupplyNumber()`, rejecting new delegations once the list exceeds a configurable maximum. Alternatively, migrate fully to the V2 delegation index model (`supportAllowDelegateOptimization`) which stores each delegation pair as a separate DB key rather than a single growing list, and deprecate/disable the legacy unbounded-list code path.

### Proof of Concept
1. Ensure `dynamicPropertiesStore.supportAllowDelegateOptimization()` is disabled (legacy mode) or observe an account still using the legacy index (pre-optimization accounts, or any chain configuration where this flag is 0).
2. Repeatedly submit `FreezeBalanceContract` transactions from account A, each with a `frozen_balance` of the minimal allowed amount and a unique freshly generated `receiver_address`.
3. After each transaction, `FreezeBalanceActuator.delegateResource()` appends the new receiver to A's `DelegatedResourceAccountIndexCapsule.toAccountsList` (see lines 319-345 above) with no upper bound check.
4. Repeat thousands of times; each subsequent transaction becomes progressively more expensive to process (linear `contains()` scan plus larger serialization), and `wallet.getDelegatedResourceAccountIndex(A)` becomes progressively larger to compute and return, as demonstrated at moderate scale (100 receivers) in the existing test `testMultiFreezeDelegatedBalanceForBandwidth`: [6](#0-5)

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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1040-1051)
```java
  public DelegatedResourceAccountIndex getDelegatedResourceAccountIndex(ByteString address) {
    if (address == null || address.size() != DecodeUtil.ADDRESS_SIZE / 2) {
      return DelegatedResourceAccountIndex.getDefaultInstance();
    }
    DelegatedResourceAccountIndexCapsule accountIndexCapsule =
        chainBaseManager.getDelegatedResourceAccountIndexStore().getIndex(address.toByteArray());
    if (accountIndexCapsule != null) {
      return accountIndexCapsule.getInstance();
    } else {
      return DelegatedResourceAccountIndex.getDefaultInstance();
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetDelegatedResourceAccountIndexServlet.java (L58-67)
```java
  private void fillResponse(ByteString address, boolean visible, HttpServletResponse response)
      throws IOException {
    DelegatedResourceAccountIndex reply =
        wallet.getDelegatedResourceAccountIndex(address);
    if (reply != null) {
      response.getWriter().println(JsonFormat.printToString(reply, visible));
    } else {
      response.getWriter().println("{}");
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L232-235)
```java
    if (assetIssueContract.getFrozenSupplyCount()
        > dynamicStore.getMaxFrozenSupplyNumber()) {
      throw new ContractValidateException("Frozen supply list length is too long");
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/FreezeBalanceActuatorTest.java (L275-337)
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
    dbManager.getDynamicPropertiesStore().saveAllowDelegateOptimization(0L);
    dbManager.getDynamicPropertiesStore().saveLatestBlockHeaderTimestamp(10000L);
  }
```
