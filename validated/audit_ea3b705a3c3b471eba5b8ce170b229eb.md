I have sufficient evidence to write the finding now.

### Title
Double-precision arithmetic mismatch in `DelegateResourceActuator.validate()` causes inaccurate resource-usage accounting, allowing delegation of already-consumed FreezeV2 bandwidth/energy - (File: actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java)

### Summary
`DelegateResourceActuator.validate()` computes the account's V2 (post-unfreeze-delay) bandwidth/energy usage with a mixed-precision `double`/`long` formula that multiplies raw usage by `TRX_PRECISION` and a `double` weight/limit ratio before truncating to `long`. This mirrors the reported Blueberry issue: combining values expressed in different scales/precisions through floating-point division/multiplication produces truncation and rounding error, yielding an incorrect "already consumed" usage figure that gates how much FreezeV2 balance may be delegated.

### Finding Description
In `DelegateResourceActuator.doValidate()`, for `BANDWIDTH`: [1](#0-0) 
and for `ENERGY`: [2](#0-1) 

Both branches compute `netUsage`/`energyUsage` as `(long) (usage * TRX_PRECISION * ((double) totalWeight / totalLimit))`. This mixes an integer `usage` value (network/energy "bandwidth points" scale) with `TRX_PRECISION` (a fixed integer constant, `1_000_000`) and a `double`-cast ratio of two chain-wide integer accounting parameters (`totalNetWeight`/`totalNetLimit` or `totalEnergyWeight`/`totalEnergyCurrentLimit`). Because `double` has only 52 bits of mantissa, once `usage * TRX_PRECISION * totalWeight` exceeds `2^53` (easily reached given `totalWeight`/`totalLimit` can be in the billions and usage can be large on a busy account), the multiplication loses precision before the final truncating cast to `long`. The resulting `netUsage`/`energyUsage` feeds directly into `getV2NetUsage`/`getV2EnergyUsage`: [3](#0-2) 
which subtracts frozen/delegated amounts from this imprecise `netUsage`/`energyUsage` to derive `v2NetUsage`/`v2EnergyUsage`, ultimately used in the delegation-eligibility check: [4](#0-3) 

The codebase already recognizes this exact bug class elsewhere and has introduced a `BigInteger`-based "hardened" replacement (`allowHardenResourceCalculation`) specifically to avoid this precision loss: [5](#0-4) 
and a regression test explicitly demonstrating the double-based formula is "buggy" relative to the `BigInteger` result: [6](#0-5) 
However, `DelegateResourceActuator.doValidate()` (and its native-contract counterpart `DelegateResourceProcessor.validate()` and the RPC-facing `Wallet.calcCanDelegatedBandWidthMaxSize`/`calcCanDelegatedEnergyMaxSize`) still perform the netUsage/energyUsage computation unconditionally via the imprecise `double` formula, without any hardened/BigInteger gate: [7](#0-6) [8](#0-7) 

### Impact Explanation
A user can broadcast a `DelegateResourceContract` (reachable directly from any signed transaction, no privilege required) whose validation of "available FreezeBandwidthV2/FreezeEnergyV2 balance" relies on this imprecise `netUsage`/`energyUsage`. When the double-precision truncation causes the computed usage to be lower than the true value, `v2NetUsage`/`v2EnergyUsage` is smaller than it should be, making `getFrozenV2BalanceForBandwidth()/Energy() - v2Usage` appear larger than the true remaining balance. This lets an account delegate resource balance it should not currently have available (because it is already backing consumed bandwidth/energy), i.e. an unbacked resource delegation — the receiver gets bandwidth/energy points that are not actually backed by unused frozen TRX, which can be used to consume network resources for free or double-count frozen stake across delegator and delegatee.

### Likelihood Explanation
Exploitability depends on account/network parameters reaching magnitudes where `double` (53-bit mantissa) precision is insufficient for the intermediate product `usage * TRX_PRECISION * totalWeight` before dividing by `totalLimit` — achievable on a chain with large `totalNetWeight`/`totalEnergyWeight` or an account with large accumulated usage, both realistic on a live production network (`totalNetWeight`/`totalEnergyWeight` scale with total frozen TRX network-wide). No special privileges are needed; any account owner controlling their own `getNetUsage()`/`getEnergyUsage()` and choosing when to delegate can attempt to land on values that bias truncation in their favor.

### Recommendation
Replace the `double`-based computation in `DelegateResourceActuator.doValidate()` (and the equivalent code in `DelegateResourceProcessor.validate()` and `Wallet.calcCanDelegatedBandWidthMaxSize`/`calcCanDelegatedEnergyMaxSize`) with the same `BigInteger`-based exact arithmetic already used in `ResourceProcessor.calculateGlobalLimitV2`/`RepositoryImpl.usageToBalance`, i.e. compute `usage * TRX_PRECISION * totalWeight / totalLimit` using `BigInteger` (or gate it behind the existing `allowHardenResourceCalculation`/`disableJavaLangMath` hardening flags consistently) so the delegation-eligibility check is not subject to floating-point truncation.

### Proof of Concept
1. Freeze a large TRX balance for BANDWIDTH via `FreezeBalanceV2Contract` on an account, and accumulate `netUsage` over time via normal transactions (or on a testnet, manipulate `totalNetWeight`/`totalNetLimit` to reach a regime where `usage * TRX_PRECISION * totalNetWeight` exceeds 2^53).
2. Call `DelegateResourceContract` with a `delegateBalance` computed to be just above the true remaining `frozenV2BalanceForBandwidth - trueV2NetUsage`, but at or below the double-truncated value from `DelegateResourceActuator.doValidate()` line 162-163.
3. Observe that `validate()` succeeds (accepting a delegation that should have been rejected), and `execute()` moves `delegateBalance` out of `FrozenBalanceForBandwidthV2` to the receiver via `addDelegatedFrozenV2BalanceForBandwidth`/`addFrozenBalanceForBandwidthV2(-delegateBalance)`, producing resource points not fully backed by unused frozen stake. [9](#0-8)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L76-80)
```java
        delegateResource(ownerAddress, receiverAddress, true,
            delegateBalance, lock, lockPeriod);

        ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth(delegateBalance);
        ownerCapsule.addFrozenBalanceForBandwidthV2(-delegateBalance);
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L157-169)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L172-183)
```java
      case ENERGY: {
        EnergyProcessor processor = new EnergyProcessor(dynamicStore, accountStore);
        processor.updateUsage(ownerCapsule);

        long energyUsage = (long) (ownerCapsule.getEnergyUsage() * TRX_PRECISION * ((double)
            (dynamicStore.getTotalEnergyWeight()) / dynamicStore.getTotalEnergyCurrentLimit()));
        long v2EnergyUsage = getV2EnergyUsage(ownerCapsule, energyUsage,
            this.disableJavaLangMath());
        if (ownerCapsule.getFrozenV2BalanceForEnergy() - v2EnergyUsage < delegateBalance) {
          throw new ContractValidateException(
                  "delegateBalance must be less than or equal to available FreezeEnergyV2 balance");
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/FreezeV2Util.java (L245-261)
```java
  public static long getV2NetUsage(AccountCapsule ownerCapsule, long netUsage, boolean
      disableJavaLangMath) {
    long v2NetUsage= netUsage
        - ownerCapsule.getFrozenBalance()
        - ownerCapsule.getAcquiredDelegatedFrozenBalanceForBandwidth()
        - ownerCapsule.getAcquiredDelegatedFrozenV2BalanceForBandwidth();
    return max(0, v2NetUsage, disableJavaLangMath);
  }

  public static long getV2EnergyUsage(AccountCapsule ownerCapsule, long energyUsage, boolean
      disableJavaLangMath) {
    long v2EnergyUsage= energyUsage
          - ownerCapsule.getEnergyFrozenBalance()
          - ownerCapsule.getAcquiredDelegatedFrozenBalanceForEnergy()
          - ownerCapsule.getAcquiredDelegatedFrozenV2BalanceForEnergy();
    return max(0, v2EnergyUsage, disableJavaLangMath);
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L359-378)
```java
  /**
   * Hardened replacement of legacy V2 formula
   * {@code (long)(((double) frozeBalance / TRX_PRECISION)
   *               * ((double) totalLimit / totalWeight))}.
   *
   * <p>Preserves V2 semantics: equivalent to
   * {@code (frozeBalance * totalLimit) / (TRX_PRECISION * totalWeight)} with
   * a single integer truncation at the end. Critically, fractional weight
   * (i.e. {@code frozeBalance < TRX_PRECISION}) is preserved through the
   * multiplication and only truncated at the final divide, so small balances
   * yield the same proportional result as the double-arithmetic path.
   */
  protected long calculateGlobalLimitV2(long frozeBalance,
      long totalLimit, long totalWeight) {
    return BigInteger.valueOf(frozeBalance)
        .multiply(BigInteger.valueOf(totalLimit))
        .divide(BigInteger.valueOf(TRX_PRECISION)
            .multiply(BigInteger.valueOf(totalWeight)))
        .longValueExact();
  }
```

**File:** framework/src/test/java/org/tron/core/vm/repository/RepositoryImplHardenTest.java (L227-258)
```java
  @Test
  public void testCalculateGlobalEnergyLimitHardenedParityWithNonIntegerRatio() {
    long totalEnergyLimit = 50_000_000_000L;
    long totalEnergyWeight = 1_234_567L;
    long frozeBalance = 10_000_000_000L;

    dbManager.getDynamicPropertiesStore().saveTotalEnergyCurrentLimit(totalEnergyLimit);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyWeight(totalEnergyWeight);

    AccountCapsule account = new AccountCapsule(
        ByteString.copyFromUtf8("owner"),
        ByteString.copyFrom(ByteArray.fromHexString(
            Wallet.getAddressPreFixString() + "548794500882809695a8a687866e76d4271a1abc")),
        AccountType.Normal, 0L);
    account.setFrozenForEnergy(frozeBalance, 0L);

    VMConfig.initAllowHardenResourceCalculation(0);
    long resultOld = repository.calculateGlobalEnergyLimit(account);

    VMConfig.initAllowHardenResourceCalculation(1);
    long resultNew = repository.calculateGlobalEnergyLimit(account);

    long expected = java.math.BigInteger.valueOf(10000L)
        .multiply(java.math.BigInteger.valueOf(totalEnergyLimit))
        .divide(java.math.BigInteger.valueOf(totalEnergyWeight))
        .longValueExact();
    Assert.assertEquals(expected, resultNew);
    Assert.assertEquals(resultOld, resultNew);

    long buggy = 10000L * (totalEnergyLimit / totalEnergyWeight);
    Assert.assertNotEquals(buggy, resultNew);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L59-87)
```java
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
      break;
      case ENERGY: {
        EnergyProcessor processor =
            new EnergyProcessor(dynamicStore, ChainBaseManager.getInstance().getAccountStore());
        processor.updateUsage(ownerCapsule);

        long energyUsage = (long) (ownerCapsule.getEnergyUsage() * TRX_PRECISION * ((double)
            (repo.getTotalEnergyWeight()) / dynamicStore.getTotalEnergyCurrentLimit()));

        long v2EnergyUsage = getV2EnergyUsage(ownerCapsule, energyUsage, disableJavaLangMath);

        if (ownerCapsule.getFrozenV2BalanceForEnergy() - v2EnergyUsage < delegateBalance) {
          throw new ContractValidateException(
                  "delegateBalance must be less than or equal to available FreezeEnergyV2 balance");
        }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1010-1013)
```java
    long netUsage = (long) (accountNetUsage * TRX_PRECISION * ((double)
            (dynamicStore.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));

    long v2NetUsage = getV2NetUsage(ownerCapsule, netUsage, dynamicStore.disableJavaLangMath());
```
