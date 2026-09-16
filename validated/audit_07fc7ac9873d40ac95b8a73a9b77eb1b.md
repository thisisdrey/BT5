## Confirmed: DelegateResourceContract supports `lock` (persisted as a separate `DelegatedResourceCapsule` at `createDbKeyV2(owner, receiver, true)`), created via `DelegateResourceActuator.delegateResource()`. [1](#0-0) 

### Title
Missing lock-resource check when TVM native `unDelegateResource` clears the delegation index, causing stale/orphaned `DelegatedResourceAccountIndexStore` entries - (File: actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java)

### Summary
`UnDelegateResourceProcessor` (invoked from TVM opcode via `Program.unDelegateResource()`) clears the `DelegatedResourceAccountIndexStore` index entries whenever the *unlocked* `DelegatedResourceCapsule` balance reaches zero for both bandwidth and energy, without checking whether a *locked* `DelegatedResourceCapsule` (created with `lock=true`) still exists for the same owner/receiver pair — unlike the ordinary `UnDelegateResourceActuator` transaction path, which explicitly checks both the unlock and lock resource capsules before removing the index.

### Finding Description
When a normal signed transaction delegates resources with `lock=true` via `DelegateResourceContract`, `DelegateResourceActuator.delegateResource()` stores a `DelegatedResourceCapsule` under the key `createDbKeyV2(owner, receiver, true)` — separate from the unlocked delegation stored under `createDbKeyV2(owner, receiver, false)`. [1](#0-0) 

When undelegating via a regular transaction, `UnDelegateResourceActuator.execute()` correctly fetches both the unlock resource and the lock resource before deciding to purge the `DelegatedResourceAccountIndexStore` entry:
```java
if (unlockResource.getFrozenBalanceForBandwidth() == 0
    && unlockResource.getFrozenBalanceForEnergy() == 0) {
  delegatedResourceStore.delete(unlockKey);
  unlockResource = null;
} ...
DelegatedResourceCapsule lockResource = delegatedResourceStore.get(lockKey);
if (lockResource == null && unlockResource == null) {
  delegatedResourceAccountIndexStore.unDelegateV2(ownerAddress, receiverAddress);
}
``` [2](#0-1) 

However, the sibling TVM native-contract path, `UnDelegateResourceProcessor.execute()` (reachable from any deployed contract that calls the `unDelegateResource` TVM opcode via `Program.unDelegateResource()` / `OperationActions.unDelegateResourceAction()`), only checks the *unlocked* `DelegatedResourceCapsule` (fetched via `createDbKeyV2(ownerAddress, receiverAddress, false)`), and purges both `DelegatedResourceAccountIndexStore` "from" and "to" index entries as soon as that unlocked capsule's balances hit zero — with no query or check of the locked capsule at `createDbKeyV2(owner, receiver, true)`:
```java
byte[] key = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, false);
DelegatedResourceCapsule delegatedResourceCapsule = repo.getDelegatedResource(key);
...
if (delegatedResourceCapsule.getFrozenBalanceForBandwidth() == 0
    && delegatedResourceCapsule.getFrozenBalanceForEnergy() == 0) {
  //modify DelegatedResourceAccountIndex
  byte[] fromKey = Bytes.concat(
      DelegatedResourceAccountIndexStore.getV2_FROM_PREFIX(), ownerAddress, receiverAddress);
  repo.updateDelegatedResourceAccountIndex(fromKey, new DelegatedResourceAccountIndexCapsule(new byte[0]));
  byte[] toKey = Bytes.concat(
      DelegatedResourceAccountIndexStore.getV2_TO_PREFIX(), receiverAddress, ownerAddress);
  repo.updateDelegatedResourceAccountIndex(toKey, new DelegatedResourceAccountIndexCapsule(new byte[0]));
}
``` [3](#0-2) 

This is structurally the same class of bug as the referenced Ajna finding: one code path that mutates shared accounting state (`removeMaxCollateral` / `UnDelegateResourceActuator`) performs the complete invariant check (deposit==0 AND collateral==0, or unlock==0 AND lock==0) before finalizing/"bankrupting" the state, while a sibling code path that performs the same kind of mutation (`removeCollateral` / TVM's `UnDelegateResourceProcessor`) omits part of the check, leaving the underlying store in an inconsistent state: the locked `DelegatedResourceCapsule` (with a real, non-zero frozen balance still delegated to the receiver) continues to exist, but the `DelegatedResourceAccountIndexStore` entry that is meant to reference it has been deleted.

### Impact Explanation
`DelegatedResourceAccountIndexStore` is the index that Wallet/HTTP/gRPC query paths (e.g. `getDelegatedResourceAccountIndex`, `listDelegatedResource`) rely on to enumerate a user's delegations. If the index entry is wiped out while a locked, balance-backed `DelegatedResourceCapsule` still exists, that delegation becomes effectively invisible to index-based queries even though it is still consuming the receiver's bandwidth/energy allowance and the owner's TRX remains locked/delegated. This can strand delegated (and locked) TRX from the owner's perspective — the owner cannot easily discover or later reclaim the locked delegation through normal index-based tooling, and reconciliation/accounting tools relying on the index will under-report outstanding delegations. This falls into the "permanent freezing of funds" / "unbacked balance" category since the on-chain source of truth (`DelegatedResourceStore`) and the index used to reach it diverge.

### Likelihood Explanation
Reachable by any account: (1) issue a `DelegateResourceContract` with `lock=true` to fund a locked delegation to a receiver, (2) issue an unlocked delegation to the same receiver as well (or have some small unlocked delegation), (3) from a smart contract owned by the same address, invoke the `unDelegateResource` TVM opcode to undelegate the unlocked portion down to zero. This directly triggers `UnDelegateResourceProcessor.execute()`, clearing the index while the locked capsule remains. All operations are ordinary signed transactions / contract calls; no privileged role or malicious actor collusion is required.

### Recommendation
Update `UnDelegateResourceProcessor.execute()` to mirror `UnDelegateResourceActuator`'s full-invariant check: before clearing `DelegatedResourceAccountIndexStore` entries, also fetch the locked `DelegatedResourceCapsule` via `createDbKeyV2(ownerAddress, receiverAddress, true)` and only purge the index if both the unlocked and locked resource capsules are null/empty.

### Proof of Concept
1. Owner freezes TRX v2 for bandwidth/energy.
2. Owner sends `DelegateResourceContract{receiver, resource=ENERGY, balance=X, lock=true}` — creates `DelegatedResourceCapsule` at `createDbKeyV2(owner, receiver, true)` with `frozenBalanceForEnergy = X`, and registers the index via `delegatedResourceAccountIndexStore.delegateV2(...)`. [4](#0-3) 
3. Owner also sends a small unlocked delegation (`lock=false`) of the same resource type to the same receiver (creating the `false`-keyed capsule with a small balance).
4. Owner's contract calls the TVM `unDelegateResource` opcode to undelegate exactly the small unlocked balance.
5. In `UnDelegateResourceProcessor.execute()`, the unlocked capsule's `frozenBalanceForEnergy`/`frozenBalanceForBandwidth` become 0, satisfying the zero-check, so both `fromKey` and `toKey` entries in `DelegatedResourceAccountIndexStore` are cleared — even though the locked capsule (`X` TRX worth of energy delegation) is untouched and still exists in `DelegatedResourceStore`. [5](#0-4) 
6. Subsequent index-based lookups (`getDelegatedResourceAccountIndexV2`/wallet queries) for owner→receiver no longer show the outstanding locked delegation, even though it still holds `X` TRX worth of delegated/locked resource.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L290-315)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L170-185)
```java
    if (unlockResource.getFrozenBalanceForBandwidth() == 0
        && unlockResource.getFrozenBalanceForEnergy() == 0) {
      delegatedResourceStore.delete(unlockKey);
      unlockResource = null;
    } else {
      delegatedResourceStore.put(unlockKey, unlockResource);
    }

    byte[] lockKey = DelegatedResourceCapsule
        .createDbKeyV2(ownerAddress, receiverAddress, true);
    DelegatedResourceCapsule lockResource = delegatedResourceStore
        .get(lockKey);
    if (lockResource == null && unlockResource == null) {
      //modify DelegatedResourceAccountIndexStore
      delegatedResourceAccountIndexStore.unDelegateV2(ownerAddress, receiverAddress);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L160-206)
```java
    // modify owner Account
    byte[] key = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, false);
    DelegatedResourceCapsule delegatedResourceCapsule = repo.getDelegatedResource(key);
    switch (param.getResourceType()) {
      case BANDWIDTH: {
        delegatedResourceCapsule.addFrozenBalanceForBandwidth(-unDelegateBalance, 0);

        ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth(-unDelegateBalance);
        ownerCapsule.addFrozenBalanceForBandwidthV2(unDelegateBalance);

        BandwidthProcessor processor = new BandwidthProcessor(ChainBaseManager.getInstance());
        if (Objects.nonNull(receiverCapsule) && transferUsage > 0) {
          processor.unDelegateIncrease(ownerCapsule, receiverCapsule,
              transferUsage, BANDWIDTH, now);
        }
      }
      break;
      case ENERGY: {
        delegatedResourceCapsule.addFrozenBalanceForEnergy(-unDelegateBalance, 0);

        ownerCapsule.addDelegatedFrozenV2BalanceForEnergy(-unDelegateBalance);
        ownerCapsule.addFrozenBalanceForEnergyV2(unDelegateBalance);

        EnergyProcessor processor =
            new EnergyProcessor(dynamicStore, ChainBaseManager.getInstance().getAccountStore());
        if (Objects.nonNull(receiverCapsule) && transferUsage > 0) {
          processor.unDelegateIncrease(ownerCapsule, receiverCapsule, transferUsage, ENERGY, now);
        }
      }
      break;
      default:
        //this should never happen
        break;
    }

    if (delegatedResourceCapsule.getFrozenBalanceForBandwidth() == 0
        && delegatedResourceCapsule.getFrozenBalanceForEnergy() == 0) {
      //modify DelegatedResourceAccountIndex
      byte[] fromKey = Bytes.concat(
          DelegatedResourceAccountIndexStore.getV2_FROM_PREFIX(), ownerAddress, receiverAddress);
      repo.updateDelegatedResourceAccountIndex(
          fromKey, new DelegatedResourceAccountIndexCapsule(new byte[0]));
      byte[] toKey = Bytes.concat(
          DelegatedResourceAccountIndexStore.getV2_TO_PREFIX(), receiverAddress, ownerAddress);
      repo.updateDelegatedResourceAccountIndex(
          toKey, new DelegatedResourceAccountIndexCapsule(new byte[0]));
    }
```
