### Title
Missing null-check guard allows NullPointerException in `UnfreezeBalanceActuator.execute()` when a delegated resource receiver account is destroyed - (File: `actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java`)

### Summary
`UnfreezeBalanceActuator.execute()` fetches the delegation receiver account (`receiverCapsule`) and then guards the subsequent field mutations with a condition that only partially validates the null-ness of that object, exactly mirroring the kernel bug class in the report (a check is performed, but a later code path uses the object without re-validating that it is still non-null/valid before dereferencing it).

### Finding Description
In `execute()`, after unfreezing a delegated resource, the code re-fetches the receiver account and decides how to update it: [1](#0-0) 

```java
AccountCapsule receiverCapsule = accountStore.get(receiverAddress);

if (dynamicStore.getAllowTvmConstantinople() == 0 ||
    (receiverCapsule != null && receiverCapsule.getType() != AccountType.Contract)) {
  switch (unfreezeBalanceContract.getResource()) {
    case BANDWIDTH:
      long oldNetWeight = receiverCapsule.getAcquiredDelegatedFrozenBalanceForBandwidth() /
              TRX_PRECISION;
```

The `if` condition is a boolean OR: `dynamicStore.getAllowTvmConstantinople() == 0` **or** `(receiverCapsule != null && ...)`. When the first operand is `true` (i.e., `getAllowTvmConstantinople() == 0`), the whole condition short-circuits to `true` regardless of whether `receiverCapsule` is `null`. The block then unconditionally calls `receiverCapsule.getAcquiredDelegatedFrozenBalanceForBandwidth()` / `getAcquiredDelegatedFrozenBalanceForEnergy()`, dereferencing a potentially-null reference.

`receiverCapsule` can legitimately become `null` when the receiver was a smart contract that self-destructed (as explicitly acknowledged in the sibling actuator `UnDelegateResourceActuator`, which comments "A TVM contract suicide, re-create will produce this situation" and guards this exact scenario with `if (receiverCapsule != null)`): [2](#0-1) 

`UnfreezeBalanceActuator` does not apply the same defensive check for the equivalent legacy `UnfreezeBalanceContract` path, so the ownership check ("account still exists as a normal account") is bypassed exactly when `allowTvmConstantinople` is disabled.

### Impact Explanation
An unhandled `NullPointerException` thrown inside `execute()` is not caught by the actuator's own `try/catch` (which only wraps the protobuf `unpack()` call), so it propagates out of transaction execution during block application in `Manager.processTransaction`/`processBlock`. This can cause the transaction-processing/block-application path to fail unexpectedly with an uncaught runtime exception instead of a handled `ContractExeException`, and — because it is reachable purely from a broadcast transaction — it is a node-crash-class defect matching the impact bar (node crash/service disruption from an unprivileged transaction).

### Likelihood Explanation
The bug requires `dynamicStore.getAllowTvmConstantinople() == 0`. On long-running public TRON networks this proposal has been permanently activated, so the flag is `1` and this branch is currently dead on those specific networks. However, on freshly bootstrapped/private java-tron deployments (sidechains, private networks, or networks before this specific chain parameter proposal is activated) the flag defaults to `0`, making the path directly reachable by any account: freeze balance with a receiver, have the receiver contract self-destruct (or otherwise be absent from `AccountStore`), then submit `UnfreezeBalanceContract`.

### Recommendation
Change the guard to require the null check unconditionally, e.g.:
```java
if (receiverCapsule != null &&
    (dynamicStore.getAllowTvmConstantinople() == 0 || receiverCapsule.getType() != AccountType.Contract)) {
```
and add an explicit `else` branch mirroring `UnDelegateResourceActuator`'s handling of a `null` receiver (skip receiver mutation, only recompute `decrease`).

### Proof of Concept
1. Deploy on a java-tron network/testnet where `getAllowTvmConstantinople() == 0` (default before that chain parameter proposal is activated).
2. Account A freezes TRX and delegates bandwidth/energy to contract account B (`FreezeBalanceContract` with `receiver_address = B`, requires `dynamicStore.supportDR()`).
3. Contract B self-destructs (`selfdestruct`), removing its entry from `AccountStore` (as demonstrated by the `StakeV2AfterSelfDestructTest` scenarios in the codebase for the analogous V2 flow).
4. Account A submits `UnfreezeBalanceContract` with the same `receiver_address = B`.
5. In `execute()`, `accountStore.get(receiverAddress)` returns `null` for `receiverCapsule`; because `getAllowTvmConstantinople() == 0`, the guard evaluates to `true` regardless, and `receiverCapsule.getAcquiredDelegatedFrozenBalanceForBandwidth()` throws `NullPointerException`, propagating out of transaction execution.

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

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L67-78)
```java
    // modify receiver Account
    if (receiverCapsule != null) {
      long now = chainBaseManager.getHeadSlot();
      switch (unDelegateResourceContract.getResource()) {
        case BANDWIDTH:
          BandwidthProcessor bandwidthProcessor = new BandwidthProcessor(chainBaseManager);
          bandwidthProcessor.updateUsageForDelegated(receiverCapsule);

          if (receiverCapsule.getAcquiredDelegatedFrozenV2BalanceForBandwidth()
              < unDelegateBalance) {
            // A TVM contract suicide, re-create will produce this situation
            receiverCapsule.setAcquiredDelegatedFrozenV2BalanceForBandwidth(0);
```
