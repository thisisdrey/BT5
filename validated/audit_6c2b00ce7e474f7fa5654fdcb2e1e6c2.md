## Analysis Result

I identified a plausible analog but could **not fully confirm root cause** within the available tool budget, so I present it with explicit caveats rather than a fully validated finding.

### Title
Client-facing bandwidth/energy delegation estimate (`Wallet.calcCanDelegatedBandWidthMaxSize`) does not mirror the enforcement path used by `DelegateResourceActuator::validate()`, risking DoS of delegate-resource transactions - (File: `framework/src/main/java/org/tron/core/Wallet.java`)

### Summary
The reported PoolTogether bug is a class of "estimator/limit view function does not match the stricter check performed by the state-changing execution path," causing transactions built from the estimate to revert (DoS). In java-tron, the `getCanDelegatedMaxSize` gRPC/HTTP API (`Wallet.calcCanDelegatedBandWidthMaxSize` / `calcCanDelegatedEnergyMaxSize`) is exposed to any unprivileged client to determine how much bandwidth/energy balance they can safely pass into a `DelegateResourceContract`. [1](#0-0) 

### Finding Description
`calcCanDelegatedBandWidthMaxSize` calls `BandwidthProcessor.updateUsage(ownerCapsule)` and always adds `TransactionUtil.estimateConsumeBandWidthSize(...)` to the account's net usage before computing `maxSize = frozenV2BalanceForBandwidth - v2NetUsage`. [2](#0-1) 

`DelegateResourceActuator.validate()`, which is the actual enforcement executed when a `DelegateResourceContract` transaction is broadcast, instead calls `BandwidthProcessor.updateUsageForDelegated(ownerCapsule)` and only conditionally adds the same `estimateConsumeBandWidthSize` term when `this.getTx().isTransactionCreate()` is true, then rejects the transaction with `"delegateBalance must be less than or equal to available FreezeBandwidthV2 balance"` if `frozenV2BalanceForBandwidth - v2NetUsage < delegateBalance`. [3](#0-2) 

This mirrors the structure of the PoolTogether bug: a query used by external actors to size a subsequent state-changing call (`liquidatableBalanceOf()` → `swapExactAmountOut()`, here `getCanDelegatedMaxSize` → `DelegateResourceContract`) computes its bound using a different accounting path/method than the one actually enforced during execution.

### Impact Explanation
If `updateUsage` and `updateUsageForDelegated` diverge in how they account for existing usage/expiration (which I was unable to verify due to tool-call limits — I did not get to read `BandwidthProcessor.updateUsage` vs `updateUsageForDelegated` internals), the API could report a `maxSize` larger than what `DelegateResourceActuator.validate()` will actually accept. Any wallet/bot that constructs a `DelegateResourceContract` using the reported `maxSize` would have its transaction reverted, wasting the caller's bandwidth/energy fee and blocking the intended resource delegation — a DoS on an unprivileged, broadcastable transaction type.

### Likelihood Explanation
**Uncertain.** I could not confirm within the available investigation that `updateUsage` and `updateUsageForDelegated` actually produce different results for the same account state, nor did I confirm the direction of any discrepancy (over- vs under-estimate). Without that confirmation, I cannot assert this is definitely exploitable — it may be that both methods converge for delegate-capable accounts, in which case there is no real-world divergence and this would not be a valid finding.

### Recommendation
A background engineering session should:
1. Read `org/tron/core/db/BandwidthProcessor.java` (`updateUsage` and `updateUsageForDelegated`) and `EnergyProcessor.updateUsage` to determine whether they compute different usage values for the same account.
2. Compare exact code paths of `Wallet.calcCanDelegatedBandWidthMaxSize`/`calcCanDelegatedEnergyMaxSize` against `DelegateResourceActuator.validate()` to confirm whether the estimate can exceed the actual enforced limit.
3. If confirmed, align the estimator to use the same usage-update method and the same conditional `estimateConsumeBandWidthSize` inclusion as the actuator's validate path.

### Proof of Concept
Not constructed — this requires confirming the divergence in `BandwidthProcessor`/`EnergyProcessor` behavior first, which was not completed due to tool-call limits. **I cannot state with confidence that this is an exploitable vulnerability without that additional verification step.**

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L994-1017)
```java
  public long calcCanDelegatedBandWidthMaxSize(
          ByteString ownerAddress) {
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    AccountCapsule ownerCapsule = accountStore.get(ownerAddress.toByteArray());
    if (ownerCapsule == null) {
      return 0L;
    }

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
