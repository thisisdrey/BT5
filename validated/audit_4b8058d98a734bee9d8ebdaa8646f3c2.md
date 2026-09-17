This is a real TVM opcode with a confirmed reachable path: `FREEZEEXPIRETIME` is a registered opcode in `Op.java`/`OperationRegistry.java` that dispatches to `Program.freezeExpireTime`, callable by any deployed contract from a signed transaction (contract call).

### Title
Wrong `DelegatedResourceCapsule` key version used in `Program.freezeExpireTime` causes the `FREEZEEXPIRETIME` TVM opcode to always report expired/zero for V2-delegated resources - (File: actuator/src/main/java/org/tron/core/vm/program/Program.java)

### Summary
`Program.freezeExpireTime` (invoked by the `FREEZEEXPIRETIME` TVM opcode) looks up a delegated resource record using the legacy `DelegatedResourceCapsule.createDbKey(owner, target)` key format instead of the `createDbKeyV2(owner, target, lock)` format that the actual resource-delegation actuators (`DelegateResourceActuator`, `DelegateResourceProcessor`, `UnDelegateResourceActuator`) write to.

### Finding Description
Since TRON's "new resource model" (post `supportDR()`/unfreeze-delay activation), all delegate/undelegate operations reachable from user transactions store `DelegatedResourceCapsule` records under the V2 key layout: [1](#0-0) [2](#0-1) 

Both use `DelegatedResourceCapsule.createDbKeyV2(from, to, lock)`: [3](#0-2) 

However, `Program.freezeExpireTime`, which backs the `FREEZEEXPIRETIME` TVM opcode reachable by any contract via a normal signed transaction, constructs the lookup key with the legacy V1 format `createDbKey(owner, target)`: [4](#0-3) 

Because `createDbKey` produces a different byte layout than `createDbKeyV2` (no version prefix byte, and it doesn't distinguish locked vs. unlocked resources), this lookup (`getContractState().getDelegatedResource(key)`) will not find any record created through the current (V2/lock-aware) delegation path. The function then falls through to `return 0;`, silently reporting that no delegated freeze exists / expire time is 0 for delegated resources that actually exist and have real balances and expire times.

This is directly analogous to the reported `_handleLiquidationSuccess` bug: a lookup helper is invoked with a parameter/key construction that doesn't match how the corresponding record was actually stored, causing the lookup to always miss real records.

### Impact Explanation
Any deployed smart contract can call the `FREEZEEXPIRETIME` opcode to query the resource-freeze expiration for a delegated relationship. For any delegation created via the current V2 flow (`DelegateResourceContract`/native `DelegateResourceProcessor`), this opcode will incorrectly return `0` instead of the real expire time, and will also incorrectly report `0`/non-existence for the frozen balance check path that depends on this key. Smart contracts that gate logic on this opcode (e.g., staking/DeFi contracts built on top of TRON's native resource delegation, verifying lock periods before allowing an action) can be misled into treating an active, locked delegation as already expired, potentially enabling premature actions that should be blocked until the real lock/expire time has passed. This can lead to unauthorized account operations or incorrect resource/fund handling in dependent contracts — a concrete on-chain integrity defect stemming directly from java-tron's own precompiled opcode logic, not from misuse by a caller.

### Likelihood Explanation
Likelihood is high given normal usage: delegation of resources via `DelegateResourceContract` is a routine, unprivileged operation available to any account, and it always uses the V2 key format on any network with the new resource model activated (which is the case on current mainnet/most networks given `supportDR()`/`supportUnfreezeDelay()` gating elsewhere in the codebase implies these features are expected to be enabled). Any contract calling `FREEZEEXPIRETIME` against a delegation created this way will deterministically hit the bug — no adversarial conditions or races are required.

### Recommendation
Update `Program.freezeExpireTime` to use `DelegatedResourceCapsule.createDbKeyV2(owner, target, lock)` consistent with how `DelegateResourceProcessor`/`UnDelegateResourceProcessor` store and read records, checking both the locked and unlocked key variants (as `UnDelegateResourceActuator.validate` does at lines 256–259) rather than the legacy V1 `createDbKey`.

### Proof of Concept
1. Account A delegates BANDWIDTH to account B via `DelegateResourceContract` with `lock=true` and some `lockPeriod`. This is processed by `DelegateResourceProcessor.execute`, which stores the record under `createDbKeyV2(A, B, false)` (unlocked bucket per current code) or under the lock-prefixed key depending on `lock`. [5](#0-4) 
2. A contract owned/controlled by A or B calls the `FREEZEEXPIRETIME` opcode (via Solidity inline assembly or a native contract call) with `targetAddress = B` (or A) and `resourceType = 0` (bandwidth).
3. `Program.freezeExpireTime` computes `key = DelegatedResourceCapsule.createDbKey(owner, target)` (V1 format) and calls `getContractState().getDelegatedResource(key)`. [6](#0-5) 
4. Because the stored record's actual key is `createDbKeyV2(...)`, the V1 key does not match, `delegatedResourceCapsule` is `null`, and the function returns `0`, even though a real, non-zero-balance, non-expired delegation exists on-chain.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L146-159)
```java
  private void delegateResource(
      byte[] ownerAddress,
      byte[] receiverAddress,
      boolean isBandwidth,
      long delegateBalance,
      Repository repo) {
    //modify DelegatedResourceStore
    byte[] key = DelegatedResourceCapsule.createDbKeyV2(ownerAddress, receiverAddress, false);
    DelegatedResourceCapsule delegatedResourceCapsule = repo.getDelegatedResource(key);
    if (delegatedResourceCapsule == null) {
      delegatedResourceCapsule = new DelegatedResourceCapsule(
          ByteString.copyFrom(ownerAddress),
          ByteString.copyFrom(receiverAddress));
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L128-131)
```java
    byte[] unlockKey = DelegatedResourceCapsule
        .createDbKeyV2(ownerAddress, receiverAddress, false);
    DelegatedResourceCapsule unlockResource = delegatedResourceStore
        .get(unlockKey);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceCapsule.java (L37-49)
```java
  public static byte[] createDbKey(byte[] from, byte[] to) {
    byte[] key = new byte[from.length + to.length];
    System.arraycopy(from, 0, key, 0, from.length);
    System.arraycopy(to, 0, key, from.length, to.length);
    return key;
  }

  public static byte[] createDbKeyV2(byte[] from, byte[] to, boolean lock) {
    if (lock) {
      return Bytes.concat(V2_LOCK_PREFIX, from, to);
    }
    return Bytes.concat(V2_PREFIX, from, to);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2010-2023)
```java
    } else {
      byte[] key = DelegatedResourceCapsule.createDbKey(owner, target);
      DelegatedResourceCapsule delegatedResourceCapsule = getContractState().getDelegatedResource(key);
      if (delegatedResourceCapsule != null) {
        if (resourceCode == 0) {
          if (delegatedResourceCapsule.getFrozenBalanceForBandwidth() != 0) {
            return delegatedResourceCapsule.getExpireTimeForBandwidth();
          }
        } else if (resourceCode == 1) {
          if (delegatedResourceCapsule.getFrozenBalanceForEnergy() != 0) {
            return delegatedResourceCapsule.getExpireTimeForEnergy();
          }
        }
      }
```
