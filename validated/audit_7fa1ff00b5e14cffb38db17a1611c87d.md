### Title
Unbounded Legacy Delegation Index Migration in `UnfreezeBalanceActuator`/`UnDelegateResourceActuator` Can Exceed Per-Transaction Processing Limits - (File: `actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java`)

### Summary
`UnfreezeBalanceActuator.execute` and `UnDelegateResourceActuator.execute` call `DelegatedResourceAccountIndexStore.convert(address)` to migrate a legacy `DelegatedResourceAccountIndexCapsule`'s `toAccountsList`/`fromAccountsList` into the new per-pair key-value format. This mirrors the reported bug class: a single, unprivileged transaction triggers processing of a list whose size is fully attacker-controlled and unbounded, with no cap on iteration count within that transaction, similarly to `FeeConversionKeeper::performUpkeep` iterating over all vaults connected to a market.

### Finding Description
`UnfreezeBalanceActuator.execute` (and analogously `UnDelegateResourceActuator`) invokes: [1](#0-0) 

which calls into `DelegatedResourceAccountIndexStore.convert`: [2](#0-1) 

`convert()` reads the legacy `DelegatedResourceAccountIndexCapsule` for an address and, for every entry in its `toAccountsList` and `fromAccountsList`, performs a `delegate()` call, which writes **two** new key-value entries per legacy entry: [3](#0-2) 

Before the `AllowDelegateOptimization` upgrade was activated, any unprivileged account could freeze at least 1 TRX and delegate it (`DelegateResource`/`FreezeBalance` with a `receiverAddress`) to an arbitrary number of distinct receiver addresses. Each such delegation appended an entry to `toAccountsList` on the sender's index capsule and `fromAccountsList` on each receiver's index capsule via the old code path referenced in `UnfreezeBalanceActuator`: [4](#0-3) 

There is no bound on the number of distinct addresses one account can delegate to or receive delegations from in this legacy structure (unlike the bounded `FrozenV2List` size of 3, or `MAX_VOTE_NUMBER` for votes). An account (or an account targeted by an attacker sending it many tiny delegations from many source addresses) could therefore accumulate a very large `fromAccountsList`/`toAccountsList`. The very first `UnfreezeBalance`/`UnDelegateResource` transaction executed for that address after the optimization proposal activates will trigger `convert()`, which loops over the entire legacy list in a single transaction execution and performs `2 * N` DB writes, where `N` is fully attacker-inflated. This is the direct analog of `Vault.recalculateVaultsCreditCapacity` iterating over an unbounded, attacker-grow-able list of connected vaults inside a single externally triggered call.

### Impact Explanation
Because Tron transaction execution does not meter this DB-write-heavy loop against energy/bandwidth (it is native actuator logic, not TVM opcodes), an attacker-inflated legacy delegation index can make a single `UnfreezeBalance`/`UnDelegateResource` transaction perform an unbounded number of store writes. This can push transaction processing time for that single transaction beyond the acceptable block-production time budget, risking either the transaction/block being unable to be produced in time by witnesses (denial of service against block production for whichever witness happens to process it) or making it impossible for the victim account to ever successfully unfreeze/undelegate its balance (their funds effectively become inaccessible/frozen), which constitutes a permanent freezing-of-funds condition for the targeted account.

### Likelihood Explanation
This requires the network to still be pre-`AllowDelegateOptimization`-migration for a given account (i.e., the account has never had `convert()` triggered), and requires the attacker to have amassed a large number of distinct delegation relationships before or shortly after the optimization proposal was approved. Since the migration is lazy (only triggered on the next unfreeze/undelegate action, per the `convert()` code path), any account that accumulated many legacy delegation entries pre-upgrade remains exposed indefinitely until that one triggering transaction is sent, at which point the entire backlog is processed atomically and un-splittably in a single transaction.

### Recommendation
Bound the size of `toAccountsList`/`fromAccountsList` that can be created under the legacy delegation index scheme (mirroring the caps already applied to `FrozenV2List`/vote lists), or change `convert()` to migrate entries incrementally across multiple calls/transactions instead of atomically converting the full list in one execution. Additionally, consider capping or paginating the number of `delegate()` calls performed within a single `convert()` invocation, deferring any remainder to subsequent unfreeze/undelegate calls for the same address.

### Proof of Concept
1. Prior to `AllowDelegateOptimization` activation, have attacker-controlled addresses `A_1 … A_n` each freeze the 1 TRX minimum and delegate it to victim address `V` via `FreezeBalanceContract`/legacy delegate path, so `V`'s `DelegatedResourceAccountIndexCapsule.fromAccountsList` accumulates `n` entries (see `UnfreezeBalanceActuator.java:174-182` for how `fromAccountsList` is populated/consumed).
2. After `AllowDelegateOptimization` is enabled, have `V` (or anyone triggering the relevant unfreeze/undelegate action on `V`) submit an `UnfreezeBalance`/`UnDelegateResource` transaction that reaches the `convert()` code path.
3. Observe that `DelegatedResourceAccountIndexStore.convert` (`chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java:42-61`) iterates over all `n` entries in a single transaction execution, performing `2n` `delegate()` writes, with processing cost scaling linearly and unboundedly with attacker-chosen `n`, unmetered by any per-transaction energy/bandwidth cap for this native logic.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L162-188)
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
        } else {
          //modify DelegatedResourceAccountIndexStore new
          delegatedResourceAccountIndexStore.convert(ownerAddress);
          delegatedResourceAccountIndexStore.convert(receiverAddress);
          delegatedResourceAccountIndexStore.unDelegate(ownerAddress, receiverAddress);
        }
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L42-61)
```java
  public void convert(byte[] address) {
    DelegatedResourceAccountIndexCapsule indexCapsule = this.get(address);
    if (indexCapsule == null) {
      // convert complete or have no delegate
      return;
    }
    // convert old data
    List<ByteString> toList = indexCapsule.getToAccountsList();
    for (int i = 0; i < toList.size(); i++) {
      // use index as the timestamp, just to keep index in order
      this.delegate(address, toList.get(i).toByteArray(), i + 1L);
    }

    List<ByteString> fromList = indexCapsule.getFromAccountsList();
    for (int i = 0; i < fromList.size(); i++) {
      // use index as the timestamp, just to keep index in order
      this.delegate(fromList.get(i).toByteArray(), address, i + 1L);
    }
    this.delete(address);
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegatedResourceAccountIndexStore.java (L63-75)
```java
  public void delegate(byte[] from, byte[] to, long time) {
    byte[] fromKey = Bytes.concat(FROM_PREFIX, from, to);
    DelegatedResourceAccountIndexCapsule toIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(to));
    toIndexCapsule.setTimestamp(time);
    this.put(fromKey, toIndexCapsule);

    byte[] toKey = Bytes.concat(TO_PREFIX, to, from);
    DelegatedResourceAccountIndexCapsule fromIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(ByteString.copyFrom(from));
    fromIndexCapsule.setTimestamp(time);
    this.put(toKey, fromIndexCapsule);
  }
```
