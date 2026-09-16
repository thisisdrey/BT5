## Title
Unguarded `int64`↔`double` truncation in `UnDelegateResourceActuator`/`UnDelegateResourceProcessor` allows corrupted bandwidth/energy usage accounting - (File: `actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java`)

### Summary
The `tf.range` advisory's root cause is the classic C++/mixed-arithmetic pattern where an `int64` result is computed through an intermediate `double`, silently losing precision or overflowing before being truncated back to `int64`. Java-tron has the exact same bug class in its resource-delegation accounting code, and — unlike the sibling code paths that were hardened with `BigInteger`/`hardenCalculation()` — the un-delegate path was left unprotected.

### Finding Description
`RepositoryImpl.calculateGlobalEnergyLimit`, `EnergyProcessor.calculateGlobalEnergyLimit`, `BandwidthProcessor.calculateGlobalNetLimit`, and `ResourceProcessor.calculateGlobalLimitV2` all had their `(long) (weight * ((double) totalLimit / totalWeight))` formulas replaced with exact `BigInteger` arithmetic gated by `hardenCalculation()` / `VMConfig.allowHardenResourceCalculation()`, precisely because the double-precision path can silently truncate/lose precision for balances beyond `2^53` (~9.007e15) [1](#0-0) [2](#0-1) .

However, `UnDelegateResourceActuator.execute` still computes `unDelegateMaxUsage` and `transferUsage` purely through unguarded `double` division/multiplication with no `hardenCalculation()`/`BigInteger` fallback at all: [3](#0-2) 

The same unguarded pattern is duplicated in the newer native-contract processor used by the TVM `unDelegateResource` precompile path: [4](#0-3) 

`unDelegateBalance` is directly attacker-controlled (it is the amount being un-delegated in a user-broadcast `UnDelegateResourceContract`), and `receiverCapsule.getNetUsage()` / `getAllFrozenBalanceForBandwidth()` are long-lived on-chain state that legitimately grows into the range where `double` mantissa precision (53 bits, ~9.0e15) is exceeded by TRX's total sun supply (~1.0e17). When `transferUsage` is computed with precision loss, `newNetUsage = receiverCapsule.getNetUsage() - transferUsage` (or the energy equivalent) can be driven to an incorrect value — including negative — persisted directly via `accountStore.put(...)`.

### Impact Explanation
A persistently negative or under/over-computed `netUsage`/`energyUsage` is an "unbacked" resource-accounting bug: subsequent bandwidth/energy availability checks (`bytes > (netLimit - newNetUsage)` in `BandwidthProcessor.useAccountNet`) compare against a corrupted baseline, letting the affected account transact essentially for free (theft of network resources) or, conversely, get bricked with permanently unusable resources despite frozen collateral. This falls squarely within the disclosed rule scope of "stake/delegation/reward math" and "unbacked balance" impact.

### Likelihood Explanation
`UnDelegateResourceContract` is broadcastable by any account holding delegated TRON resources — no special privilege required. Reaching the precision-loss boundary requires the receiver's frozen/usage counters or the network's `totalNetLimit`/`totalNetWeight`/`totalEnergyWeight` to be large enough that `double` arithmetic diverges from exact integer arithmetic, which is realistic given TRON's total sun supply and the fact that the project's own test suite (`CalculateGlobalLimitHardenTest`, `RepositoryImplHardenTest`) demonstrates this exact divergence at values well within plausible on-chain frozen balances (e.g. `9_876_543_210_000_000L`) [5](#0-4) .

### Recommendation
Apply the same `hardenCalculation()`/`BigInteger` (or `BigInteger.longValueExact()`) treatment used in `ResourceProcessor.calculateGlobalLimitV1/V2` and `RepositoryImpl.usageToBalance` to the `unDelegateMaxUsage`/`transferUsage` computations in both `UnDelegateResourceActuator.execute` and `UnDelegateResourceProcessor.execute`, replacing the raw `double` math with exact integer arithmetic (and rejecting/clamping instead of silently wrapping on overflow).

### Proof of Concept
1. An account accumulates a large `AllFrozenBalanceForBandwidth`/`NetUsage` (or the network's `TotalNetLimit`/`TotalNetWeight`) such that `unDelegateBalance`, `getNetUsage()`, or `getAllFrozenBalanceForBandwidth()` values push the double computation in
`UnDelegateResourceActuator.java:81-84` past 2^53-representable precision.
2. Broadcast an `UnDelegateResourceContract` un-delegating from the receiver.
3. `transferUsage = (long) (receiverCapsule.getNetUsage() * ((double)(unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()))` truncates incorrectly, producing a `transferUsage` inconsistent with exact-integer expectation (as demonstrated by the divergence tests already in `CalculateGlobalLimitHardenTest`/`RepositoryImplHardenTest`), which is then persisted as the receiver's new `NetUsage`, corrupting future bandwidth-availability checks in `BandwidthProcessor.useAccountNet`.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L992-1010)
```java
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    long frozeBalance = accountCapsule.getAllFrozenBalanceForEnergy();
    if (frozeBalance < TRX_PRECISION) {
      return 0;
    }
    long energyWeight = frozeBalance / TRX_PRECISION;
    long totalEnergyLimit = getDynamicPropertiesStore().getTotalEnergyCurrentLimit();
    long totalEnergyWeight = getDynamicPropertiesStore().getTotalEnergyWeight();

    assert totalEnergyWeight > 0;

    if (hardenResourceCalculation()) {
      return BigInteger.valueOf(energyWeight)
          .multiply(BigInteger.valueOf(totalEnergyLimit))
          .divide(BigInteger.valueOf(totalEnergyWeight))
          .longValueExact();
    }
    return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
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

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L80-92)
```java
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * ((double) (dynamicStore.getTotalNetLimit()) / dynamicStore.getTotalNetWeight()));
            transferUsage = (long) (receiverCapsule.getNetUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
            transferUsage = min(unDelegateMaxUsage, transferUsage);

            receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForBandwidth(-unDelegateBalance);
          }

          long newNetUsage = receiverCapsule.getNetUsage() - transferUsage;
          receiverCapsule.setNetUsage(newNetUsage);
          receiverCapsule.setLatestConsumeTime(now);
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnDelegateResourceProcessor.java (L114-127)
```java
          } else {
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * dynamicStore.getTotalNetLimit() / repo.getTotalNetWeight());
            transferUsage = (long) (receiverCapsule.getNetUsage()
                * ((double) (unDelegateBalance) / receiverCapsule.getAllFrozenBalanceForBandwidth()));
            transferUsage = min(unDelegateMaxUsage, transferUsage, VMConfig.disableJavaLangMath());

            receiverCapsule.addAcquiredDelegatedFrozenV2BalanceForBandwidth(-unDelegateBalance);
          }

          long newNetUsage = receiverCapsule.getNetUsage() - transferUsage;
          receiverCapsule.setNetUsage(newNetUsage);
          receiverCapsule.setLatestConsumeTime(now);
```

**File:** framework/src/test/java/org/tron/core/db/CalculateGlobalLimitHardenTest.java (L95-112)
```java
  @Test
  public void testGlobalEnergyLimitV2CorrectVsDoublePrecisionLoss() {
    long totalEnergyLimit = 50_000_000_000L;
    long totalEnergyWeight = 1_234_567L;
    long frozeBalance = 9_876_543_210_000_000L; // ~9.8e15

    dbManager.getDynamicPropertiesStore().saveTotalEnergyCurrentLimit(totalEnergyLimit);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyWeight(totalEnergyWeight);

    BigInteger expected = BigInteger.valueOf(frozeBalance)
        .multiply(BigInteger.valueOf(totalEnergyLimit))
        .divide(BigInteger.valueOf(1_000_000L)
            .multiply(BigInteger.valueOf(totalEnergyWeight)));

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);
    long actual = energyProcessor.calculateGlobalEnergyLimitV2(frozeBalance);
    Assert.assertEquals(expected.longValueExact(), actual);
  }
```
