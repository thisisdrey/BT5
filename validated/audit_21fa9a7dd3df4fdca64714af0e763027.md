### Title
Unbounded, attacker-controlled growth of `DelegatedResourceAccountIndex.fromAccounts`/`toAccounts` lists via legacy `FreezeBalanceContract` delegation enables griefing DoS on victim accounts - (File: `actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java`)

### Summary
The legacy resource-delegation path (`FreezeBalanceContract` with a `receiverAddress`, used when `supportAllowDelegateOptimization` is not enabled) maintains per-account index lists (`fromAccountsList` / `toAccountsList`) inside `DelegatedResourceAccountIndexCapsule` that grow linearly with the number of distinct counterparties a victim has ever delegated with. Any unprivileged account can force entries into a victim's `fromAccounts` list merely by delegating the minimum 1 TRX of bandwidth/energy to that victim from many distinct sender addresses, mirroring the RabbitHole `Quest.claim` pattern where a griefer can inflate an unbounded, per-user collection that is later scanned/mutated in full by an unrelated legitimate operation.

### Finding Description
`FreezeBalanceActuator.delegateResource` appends to the receiver's `fromAccountsList` and the sender's `toAccountsList` whenever a new (owner, receiver) delegation pair is created: [1](#0-0) 

This path is reachable by any signer who submits a `FreezeBalanceContract` with `frozenBalance >= 1 TRX` and an arbitrary `receiverAddress`, validated only for basic address sanity and "not equal to owner" / "not a contract account": [2](#0-1) 

Later, when the delegation is torn down via `UnfreezeBalanceActuator`, the same legacy branch performs a full linear scan/copy/remove on both the owner's and receiver's index lists: [3](#0-2) 

`DelegatedResourceAccountIndexCapsule` itself performs `contains()`/copy/`remove()` (all O(n)) on the full list every time an entry is added or removed: [4](#0-3) 

Because a griefer can create as many distinct (attacker-address, victim-address) delegation pairs as desired — each requiring only the 1 TRX minimum stake and normal transaction fees — the victim's stored `DelegatedResourceAccountIndex` record for its `fromAccounts` field can be inflated without bound and without the victim's consent, exactly analogous to the RabbitHole griefer sending receipt NFTs to bloat a victim's token list that `Quest.claim`/`getOwnedTokenIdsOfQuest` must fully scan.

### Impact Explanation
Every subsequent delegation/undelegation touching the victim address (`FreezeBalanceActuator.delegateResource`, `UnfreezeBalanceActuator` legacy branch) must deserialize, scan (`contains`), and rewrite the entire bloated list, and any API caller reading `getDelegatedResourceAccountIndex` for the victim pays growing deserialization/response cost. As the list size grows, this increases the storage footprint of the victim account indefinitely and increases the fixed transaction cost/latency any legitimate counterparty pays to interact with the victim's delegation records, which is a resource-inflation griefing vector directly analogous to the reported RabbitHole DoS class (attacker-inflatable per-user collection scanned/mutated in full by unrelated legitimate operations). This is scoped to the legacy (`supportAllowDelegateOptimization == false`) delegation model only; the newer map/index-based delegate model (`delegate`/`unDelegate`/`convert`) does not use these unbounded per-account lists.

### Likelihood Explanation
Reachable by any account holding the 1 TRX minimum freeze amount, requiring no special privilege, and repeatable indefinitely from many distinct addresses at low cost, provided the chain instance has not yet enabled `supportAllowDelegateOptimization`. On chains where this optimization is already active by default, this path is not reachable, reducing real-world likelihood.

### Recommendation
Bound the size of `fromAccounts`/`toAccounts` lists (or eagerly deduplicate/limit them) in `DelegatedResourceAccountIndexCapsule`, or require the new indexed delegation model (`supportAllowDelegateOptimization`) unconditionally, removing the legacy list-scan branches from `FreezeBalanceActuator`/`UnfreezeBalanceActuator` to eliminate the unbounded, attacker-controlled list growth entirely.

### Proof of Concept
1. On a chain instance where `supportAllowDelegateOptimization` is disabled, attacker controls N distinct funded addresses A1..AN.
2. Each Ai submits a `FreezeBalanceContract` with `frozenBalance = 1 TRX`, `resource = BANDWIDTH`, `receiverAddress = victim`.
3. `FreezeBalanceActuator.delegateResource` (lines 320-345) appends `victim`'s `fromAccountsList` with each distinct Ai, growing it to size N with no cap.
4. Any future legitimate delegate/undelegate call touching `victim`, or any API query for `victim`'s delegated-resource index, now incurs O(N) list scan/copy cost, and the on-chain storage for `victim`'s index record grows proportionally, all driven purely by unprivileged transactions from the attacker.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L243-269)
```java
    byte[] receiverAddress = freezeBalanceContract.getReceiverAddress().toByteArray();
    //If the receiver is included in the contract, the receiver will receive the resource.
    if (!ArrayUtils.isEmpty(receiverAddress) && dynamicStore.supportDR()) {
      if (Arrays.equals(receiverAddress, ownerAddress)) {
        throw new ContractValidateException("receiverAddress must not be the same as ownerAddress");
      }

      if (!DecodeUtil.addressValid(receiverAddress)) {
        throw new ContractValidateException("Invalid receiverAddress");
      }

      AccountCapsule receiverCapsule = accountStore.get(receiverAddress);
      if (receiverCapsule == null) {
        String readableOwnerAddress = StringUtil.createReadableString(receiverAddress);
        throw new ContractValidateException(
            ActuatorConstant.ACCOUNT_EXCEPTION_STR
                + readableOwnerAddress + NOT_EXIST_STR);
      }

      if (dynamicStore.getAllowTvmConstantinople() == 1
          && receiverCapsule.getType() == AccountType.Contract) {
        throw new ContractValidateException(
            "Do not allow delegate resources to contract addresses");

      }

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

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L162-182)
```java
        //modify DelegatedResourceAccountIndexStore
        if (!dynamicStore.supportAllowDelegateOptimization()) {
          DelegatedResourceAccountIndexCapsule ownerIndexCapsule =
              delegatedResourceAccountIndexStore.get(ownerAddress);
          if (ownerIndexCapsule != null) {
            List<ByteString> toAccountsList = new ArrayList<>(ownerIndexCapsule
                .getToAccountsList());
            toAccountsList.remove(ByteString.copyFrom(receiverAddress));
            ownerIndexCapsule.setAllToAccounts(toAccountsList);
            delegatedResourceAccountIndexStore.put(ownerAddress, ownerIndexCapsule);
          }

          DelegatedResourceAccountIndexCapsule receiverIndexCapsule =
              delegatedResourceAccountIndexStore.get(receiverAddress);
          if (receiverIndexCapsule != null) {
            List<ByteString> fromAccountsList = new ArrayList<>(receiverIndexCapsule
                .getFromAccountsList());
            fromAccountsList.remove(ByteString.copyFrom(ownerAddress));
            receiverIndexCapsule.setAllFromAccounts(fromAccountsList);
            delegatedResourceAccountIndexStore.put(receiverAddress, receiverIndexCapsule);
          }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L57-94)
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

  public void removeToAccount(ByteString toAccount) {
    if (getToAccountsList().contains(toAccount)) {
      List<ByteString> toList = new ArrayList<>(getToAccountsList());
      toList.remove(toAccount);
      setAllToAccounts(toList);
    }
  }
```
