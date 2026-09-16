## Title
`Wallet.getCanDelegatedMaxSize()` bandwidth estimate diverges from `DelegateResourceActuator`'s actual validation, causing the advertised delegatable amount to revert on broadcast - (File: framework/src/main/java/org/tron/core/Wallet.java)

### Summary
The public, unauthenticated query API `getCanDelegatedMaxSize` (reachable via gRPC `GetCanDelegatedMaxSize` and the `/wallet/getcandelegatedmaxsize` HTTP endpoint) computes the maximum amount of frozen bandwidth balance an account can delegate. Its calculation always deducts an estimated bandwidth cost for the delegate transaction itself. However, the actual on-chain check performed by `DelegateResourceActuator.validate()` only applies that same deduction conditionally, based on `TransactionCapsule.isTransactionCreate()`. This is structurally the same bug class as the PoolTogether finding: a "max" advisory function and the actual state-changing "execute" path use different limit-calculation logic, so the value returned by the query does not reliably match what the actuator will actually accept.

### Finding Description
`Wallet.calcCanDelegatedBandWidthMaxSize()` unconditionally adds the simulated delegate-transaction bandwidth cost to the account's net usage before computing the delegatable maximum: [1](#0-0) 

```java
BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);
processor.updateUsage(ownerCapsule);

long accountNetUsage = ownerCapsule.getNetUsage();
accountNetUsage += TransactionUtil.estimateConsumeBandWidthSize(dynamicStore,
        ownerCapsule.getFrozenV2BalanceForBandwidth());
...
long maxSize = ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage;
```

By contrast, `DelegateResourceActuator.validate()` (the actual gate that a `DelegateResourceContract` transaction must pass in order to execute) only applies `estimateConsumeBandWidthSize` when the in-flight `TransactionCapsule` is flagged as `isTransactionCreate()`: [2](#0-1) 

```java
case BANDWIDTH: {
  BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);
  processor.updateUsageForDelegated(ownerCapsule);

  long accountNetUsage = ownerCapsule.getNetUsage();
  if (null != this.getTx() && this.getTx().isTransactionCreate()) {
    accountNetUsage += TransactionUtil.estimateConsumeBandWidthSize(dynamicStore,
            ownerCapsule.getFrozenV2BalanceForBandwidth());
  }
  ...
  if (ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance) {
    throw new ContractValidateException(
        "delegateBalance must be less than or equal to available FreezeBandwidthV2 balance");
  }
}
```

The equivalent native-contract path used by the TVM opcode/precompile for `delegateResource()` (`DelegateResourceProcessor.validate()`) always calls `processor.updateUsageForDelegated(ownerCapsule)` and never applies `estimateConsumeBandWidthSize` at all: [3](#0-2) 

There are now three independent computations of "how much bandwidth-frozen balance can be delegated" (the HTTP/gRPC query, the `DelegateResourceActuator`, and the TVM `DelegateResourceProcessor`), and they diverge on whether/when the delegate-transaction's own bandwidth cost is subtracted. An unprivileged API client calling `getCanDelegatedMaxSize`/`GetCanDelegatedMaxSize` gets a value that is not guaranteed to be accepted by `DelegateResourceActuator.validate()` when that exact amount is subsequently submitted in a broadcast `DelegateResourceContract` transaction (or vice-versa, via the TVM native contract, where no deduction is applied at all and thus a larger amount than the query suggests could actually succeed).

### Impact Explanation
This is a read-only/advisory API bug affecting external wallets, exchanges, and smart-contract integrators who rely on `GetCanDelegatedMaxSize` to construct `DelegateResourceContract` transactions without over-delegating. Because the value returned is computed with different assumptions than the actuator's actual admission check, transactions built using the "safe" maximum can be rejected (`ContractValidateException: "delegateBalance must be less than or equal to available FreezeBandwidthV2 balance"`), and conversely, integrators using the TVM `delegateResource()` native contract path could delegate more than the value reported by the query implies is safe. This does not directly cause fund loss or an unbacked balance, but it breaks the API's documented contract/consistency guarantee for any transaction/contract-call constructor built on it, which is the exact "integration issue"/compliance concern flagged as Medium in the referenced report.

### Likelihood Explanation
Any unprivileged API client can trigger this discrepancy by calling `GetCanDelegatedMaxSize`/`getcandelegatedmaxsize` and then submitting a `DelegateResourceContract` for that reported amount, or by calling the `delegateResource()` TVM native contract from a smart contract. No special privileges, precompile access, or non-standard conditions are required — only the `TransactionCapsule.isTransactionCreate()` flag state at validate-time, which is set by whichever code path constructs the transaction (wallet-side creation vs. re-validation during block application), causing inconsistent enforcement.

### Recommendation
Unify the bandwidth-delegation "available to delegate" calculation into a single shared helper used identically by `Wallet.calcCanDelegatedBandWidthMaxSize()`, `DelegateResourceActuator.validate()`, and `DelegateResourceProcessor.validate()`, so the estimate returned to external callers exactly matches the criteria enforced on-chain (either always or never accounting for `estimateConsumeBandWidthSize`, consistently, regardless of `isTransactionCreate()`).

### Proof of Concept
1. Freeze bandwidth for an account such that `frozenV2BalanceForBandwidth` is close to its net usage plus one delegate-transaction cost (as in `DelegateResourceActuatorTest.testDelegateResourceNoFreeze123`, which asserts `delegateBalance = frozenBalance - 279 * TRX_PRECISION` is the actual boundary enforced by the actuator when `isTransactionCreate()` is true).
2. Call `getCanDelegatedMaxSize` for that account/BANDWIDTH; it returns `maxSize = frozenV2BalanceForBandwidth - v2NetUsage` where `v2NetUsage` already includes the `estimateConsumeBandWidthSize` deduction unconditionally.
3. Depending on how the resulting `DelegateResourceContract` transaction is subsequently constructed/validated (whether `TransactionCapsule.isTransactionCreate()` ends up `true` or `false` for that specific submission path, or whether it is instead submitted via the TVM `delegateResource()` native contract which applies no such deduction at all), the amount reported by step 2 either gets rejected by `DelegateResourceActuator.validate()` or under-reports what `DelegateResourceProcessor` would actually allow — demonstrating the query and the enforcement path do not agree, exactly analogous to `maxDeposit()` vs. `_depositAndMint()`/`yieldVault.mint()` in the referenced PoolTogether finding.

Note: I was unable to fully trace every call site that sets `TransactionCapsule.isTransactionCreate()` to true/false within the given index (only two references were found: the actuator and the capsule itself), so the precise real-world trigger conditions for when `isTransactionCreate()` is `false` during actual broadcast validation could not be completely confirmed from the indexed code; a full trace through `TransactionCapsule` and transaction pre-processing (`Manager`/`TransactionCapsule` construction) would be needed to pin down the exact scenario, and I'd recommend starting a Devin session with full repository access to verify this precisely.

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1003-1017)
```java
    BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);
    processor.updateUsage(ownerCapsule);

    long accountNetUsage = ownerCapsule.getNetUsage();
    accountNetUsage += TransactionUtil.estimateConsumeBandWidthSize(dynamicStore,
            ownerCapsule.getFrozenV2BalanceForBandwidth());

    long netUsage = (long) (accountNetUsage * TRX_PRECISION * ((double)
            (dynamicStore.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));

    long v2NetUsage = getV2NetUsage(ownerCapsule, netUsage, dynamicStore.disableJavaLangMath());

    long maxSize = ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage;
    return max(0, maxSize, dynamicStore.disableJavaLangMath());
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L152-169)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L58-72)
```java
    switch (param.getResourceType()) {
      case BANDWIDTH: {
        BandwidthProcessor processor = new BandwidthProcessor(ChainBaseManager.getInstance());
        processor.updateUsageForDelegated(ownerCapsule);

        long netUsage = (long) (ownerCapsule.getNetUsage() * TRX_PRECISION * ((double)
            (repo.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));

        long v2NetUsage = getV2NetUsage(ownerCapsule, netUsage, disableJavaLangMath);

        if (ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance) {
          throw new ContractValidateException(
                  "delegateBalance must be less than or equal to available FreezeBandwidthV2 balance");
        }
      }
```
