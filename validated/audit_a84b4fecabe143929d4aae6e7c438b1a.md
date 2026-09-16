## Analog Found

### Title
Null-pointer dereference on delegated-resource receiver account in `UnfreezeBalanceActuator.execute()` - (File: `actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java`)

### Summary
`UnfreezeBalanceActuator.execute()` fetches the delegation receiver account and then enters a branch guarded by an `||` condition where the first disjunct (`dynamicStore.getAllowTvmConstantinople() == 0`) can be true independently of whether `receiverCapsule` is null, exactly like the kernel bug's `i2c_transfer()` being called through a null `adapter` reached via a fallback path that wasn't validated for null. Inside that branch the code unconditionally calls methods on `receiverCapsule`, causing an NPE that crashes the node when processing an otherwise valid, unprivileged transaction.

### Finding Description
In `execute()`: [1](#0-0) 

`receiverCapsule` is looked up with `accountStore.get(receiverAddress)`, which can legitimately return `null` (the code even documents this scenario elsewhere: "TVM contract suicide can result in no receiving account", see `UnDelegateResourceActuator.java` comment at line 247-249). The subsequent guard:

```java
if (dynamicStore.getAllowTvmConstantinople() == 0 ||
    (receiverCapsule != null && receiverCapsule.getType() != AccountType.Contract)) {
```

short-circuits to `true` whenever `getAllowTvmConstantinople() == 0`, **regardless of whether `receiverCapsule` is null**. The branch body then calls `receiverCapsule.getAcquiredDelegatedFrozenBalanceForBandwidth()` / `...ForEnergy()` and `receiverCapsule.createDbKey()` without any null check, throwing `NullPointerException`.

This mirrors the CVE-2024-26728 root cause pattern precisely: a fallback/legacy code path (`aux_mode` absent → use i2c adapter; here, `AllowTvmConstantinople == 0` → use legacy behavior) is taken without validating that the object it depends on (`i2c adapter` / `receiverCapsule`) is actually present, leading to a null dereference deep in a commonly-reached code path.

Note that `validate()` for this same class does gate the null case correctly for `AllowTvmConstantinople == 0` (lines 350-355), but that check only fires when `receiverCapsule == null` **and** `AllowTvmConstantinople == 0` at validation time. Since the receiver account can be deleted (e.g., via TVM contract suicide) *after* validation but *before*/*during* execution — or via block-application timing — `execute()`'s independent (and differently structured) OR-condition is not equivalent to the guard in `validate()`, and can be reached with `receiverCapsule == null`. [2](#0-1) 

### Impact Explanation
`UnfreezeBalanceActuator` is invoked from ordinary `UnfreezeBalanceContract` transaction processing during `Manager` block application — a completely unprivileged, user-broadcastable transaction type. An uncaught `NullPointerException` thrown from an actuator's `execute()` during block application is not one of the actuator's declared checked exceptions (`ContractExeException`/`ContractValidateException`), so it propagates as a `RuntimeException`, which can crash block processing / halt the node (denial of service) for every full node and SR that processes the malicious block/transaction — a node-crash impact explicitly in scope per the validation rules.

### Likelihood Explanation
Reachable via a normal signed `UnfreezeBalanceContract` transaction with a `receiverAddress` set (delegated-resource unfreeze), where the receiver account has been removed from `AccountStore` (e.g., contract self-destruct/suicide as documented elsewhere in the codebase) and the network still has `AllowTvmConstantinople == 0` (a chain-parameter/fork-dependent condition). On networks where this parameter is 0 (or during a specific window), any user with a delegated resource pointing at a deletable/self-destructing contract receiver can trigger this without any special privilege.

### Recommendation
Add an explicit `receiverCapsule == null` check before entering the branch in `execute()` (mirroring, or better, matching exactly the logic already used in `validate()`), and short-circuit to the same behavior as the "else" (own-account unfreeze bookkeeping) path when the receiver no longer exists, consistent with the handling already used in `UnfreezeBalanceProcessor.execute()` (TVM native contract), which correctly wraps its analogous receiver-side update in `if (receiverCapsule != null) { ... }`: [3](#0-2) 

### Proof of Concept
1. Delegate bandwidth/energy from account A to a contract account C (`DelegateResourceContract`, or legacy delegated freeze).
2. Cause C to self-destruct (TVM `SELFDESTRUCT`/suicide), removing it from `AccountStore`.
3. Ensure `AllowTvmConstantinople` dynamic parameter is `0` on the chain (legacy/older configuration, or a chain that hasn't activated Constantinople-forward TVM features).
4. Broadcast an `UnfreezeBalanceContract` from A specifying `receiverAddress = C` after the delegated resource's expire time has passed.
5. `validate()` passes because it doesn't hard-fail on `receiverCapsule == null` when the other pre-conditions align (delegated resource still exists in `DelegatedResourceStore`, only the receiver account row was deleted).
6. `execute()` reaches the `if (dynamicStore.getAllowTvmConstantinople() == 0 || ...)` branch, evaluates true via short-circuit, and calls `receiverCapsule.getAcquiredDelegatedFrozenBalanceForBandwidth()` on a null reference, throwing `NullPointerException` during block application.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L113-120)
```java
      AccountCapsule receiverCapsule = accountStore.get(receiverAddress);

      if (dynamicStore.getAllowTvmConstantinople() == 0 ||
          (receiverCapsule != null && receiverCapsule.getType() != AccountType.Contract)) {
        switch (unfreezeBalanceContract.getResource()) {
          case BANDWIDTH:
            long oldNetWeight = receiverCapsule.getAcquiredDelegatedFrozenBalanceForBandwidth() / 
                    TRX_PRECISION;
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L349-355)
```java
      AccountCapsule receiverCapsule = accountStore.get(receiverAddress);
      if (dynamicStore.getAllowTvmConstantinople() == 0
          && receiverCapsule == null) {
        String readableReceiverAddress = StringUtil.createReadableString(receiverAddress);
        throw new ContractValidateException(
            "Receiver Account[" + readableReceiverAddress + "] does not exist");
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceProcessor.java (L134-151)
```java
      // take back resource from receiver account
      AccountCapsule receiverCapsule = repo.getAccount(receiverAddress);
      if (receiverCapsule != null) {
        switch (param.getResourceType()) {
          case BANDWIDTH:
            receiverCapsule.safeAddAcquiredDelegatedFrozenBalanceForBandwidth(-unfreezeBalance,
                VMConfig.disableJavaLangMath());
            break;
          case ENERGY:
            receiverCapsule.safeAddAcquiredDelegatedFrozenBalanceForEnergy(-unfreezeBalance,
                VMConfig.disableJavaLangMath());
            break;
          default:
            //this should never happen
            break;
        }
        repo.updateAccount(receiverCapsule.createDbKey(), receiverCapsule);
      }
```
