### Title
Premature integer truncation in legacy resource-limit weight calculation causes precision loss and incorrect bandwidth/energy allocation - (File: `chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java`)

### Summary
The DODO report describes a bug where a fixed-point calculation performs an early, mismatched-precision division that discards a fractional remainder before it is used in further arithmetic, corrupting the final result. The analogous pattern exists in java-tron's legacy (`V1`) global resource-limit formula, which truncates `frozeBalance / TRX_PRECISION` into an integer **before** multiplying by `totalLimit` and dividing by `totalWeight`, instead of deferring the division to the end as the "hardened" `V2` replacement does.

### Finding Description
`ResourceProcessor.calculateGlobalLimitV1` computes: [1](#0-0) 

```java
protected long calculateGlobalLimitV1(long frozeBalance,
    long totalLimit, long totalWeight) {
  long weight = frozeBalance / TRX_PRECISION;
  return BigInteger.valueOf(weight)
      .multiply(BigInteger.valueOf(totalLimit))
      .divide(BigInteger.valueOf(totalWeight))
      .longValueExact();
}
```

`weight = frozeBalance / TRX_PRECISION` truncates any remainder of `frozeBalance` that is not an exact multiple of `TRX_PRECISION` (1,000,000 sun) immediately, before it can be scaled by `totalLimit`. This is the same class of bug as the reported issue: two different scales/steps are combined in an order that causes an early truncation to compound into the final result, rather than deferring division until after all multiplications (as correctly done in the fix, `calculateGlobalLimitV2`): [2](#0-1) 

The project's own documentation of the hardened `V2` method explicitly states the semantic difference: fractional weight must be "preserved through the multiplication and only truncated at the final divide" to match proportional results — confirming that the `V1` path (still present and used unless a feature-flag is enabled) discards this fractional weight early. This is invoked from `BandwidthProcessor` and `EnergyProcessor` `calculateGlobalNetLimit`/`calculateGlobalEnergyLimit`, which compute the resource limit derived from an account's frozen (staked) TRX balance — reachable indirectly by any account that stakes/freezes balance and any subsequent transaction whose bandwidth/energy accounting depends on that limit.

### Impact Explanation
Because the truncation happens on `frozeBalance` alone (divided by a fixed 1,000,000 precision) prior to scaling by `totalLimit`/`totalWeight`, accounts whose frozen balance is not an exact multiple of `TRX_PRECISION` lose part of their proportional weight in the network-wide resource pool. Given `totalLimit` values on the order of tens of billions and `totalWeight` typically much smaller, the discarded fractional remainder (up to just under `TRX_PRECISION`, i.e., 999,999 sun) can, after multiplication by `totalLimit` and division by `totalWeight`, represent a non-trivial swing in the computed bandwidth/energy limit — a resource/economic-accounting deviation for stakers, matching the "stake/delegation/reward math" and "bandwidth accounting" categories in scope. This is a deterministic accounting discrepancy rather than a fund-theft primitive, so severity is bounded by the magnitude of the miscalculated resource allocation.

### Likelihood Explanation
The precision-losing legacy path is exercised for every account computing its global bandwidth/energy limit unless `allowHardenResourceCalculation` is enabled network-wide (the tests show this is a togglable dynamic property, implying it may not be active on all networks/at all times). Any account holder who freezes/stakes a balance that is not an exact multiple of 1,000,000 sun will trigger the truncation deterministically on every limit recalculation — this requires no special privilege, just a normal freeze/stake transaction.

### Recommendation
Ensure the hardened `calculateGlobalLimitV2`-style computation (deferring division until after all multiplications, using `BigInteger`) is the default and only code path, removing/disabling the legacy `calculateGlobalLimitV1` truncate-then-multiply pattern, or gate its use so it can never silently activate in production without the hardening flag.

### Proof of Concept
Given `frozeBalance = 1_500_000` (1.5 × `TRX_PRECISION`), `totalLimit = 50_000_000_000`, `totalWeight = 1_000_000`:
- `calculateGlobalLimitV1`: `weight = 1_500_000 / 1_000_000 = 1` (truncated), result `= 1 * 50_000_000_000 / 1_000_000 = 50_000`.
- Correct (deferred) computation: `1_500_000 * 50_000_000_000 / (1_000_000 * 1_000_000) = 75_000`.

The legacy path underreports the account's proportional resource limit by 33% in this example, purely due to the early truncating division — directly analogous to the reported DODO mismatched-precision bug.

Note: I was unable to fully confirm from the indexed code whether `allowHardenResourceCalculation` is enabled by default on mainnet or is still opt-in, which affects whether the vulnerable `V1` path is presently reachable in production; this should be verified against `DynamicPropertiesStore.allowHardenResourceCalculation()` default initialization and the current chain parameter state, which the available index did not fully expose. [3](#0-2)

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L346-348)
```java
  protected boolean hardenCalculation() {
    return dynamicPropertiesStore.allowHardenResourceCalculation();
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L350-357)
```java
  protected long calculateGlobalLimitV1(long frozeBalance,
      long totalLimit, long totalWeight) {
    long weight = frozeBalance / TRX_PRECISION;
    return BigInteger.valueOf(weight)
        .multiply(BigInteger.valueOf(totalLimit))
        .divide(BigInteger.valueOf(totalWeight))
        .longValueExact();
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
