Confirmed: `TOTAL_NET_LIMIT` proposal validation at `ProposalUtil.java:506-515` allows `value < 0 || value > 1_000_000_000_000L`, meaning `value == 0` is a **valid** committee-approved value (the same is true for `TOTAL_CURRENT_ENERGY_LIMIT` at lines 134-141, which also accepts `value == 0`). This is the exact analog to the reported bug class: a privileged actor (committee, via `ProposalApproveActuator`/`ProposalCreateActuator`) can legitimately drive a critical divisor to `0`, and several downstream consumers divide by it without a zero-guard.

### Title
Committee-Approved `TOTAL_NET_LIMIT`/`TOTAL_CURRENT_ENERGY_LIMIT` of 0 Causes Unguarded Division-by-Zero in Delegation Validation and Query Paths - (File: `actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java`, `framework/src/main/java/org/tron/core/Wallet.java`)

### Summary
The chain parameters `TOTAL_NET_LIMIT` and `TOTAL_CURRENT_ENERGY_LIMIT` (a.k.a. `TotalEnergyCurrentLimit`) can be proposed and approved by the committee with value `0`, since `ProposalUtil.validator()` only checks `value < 0 || value > <max>` for these proposal types [1](#0-0) . Several code paths that read `getTotalNetLimit()`/`getTotalEnergyCurrentLimit()` divide by these values without checking for zero first, unlike the sibling `BandwidthProcessor`/`EnergyProcessor` methods that do include `if (totalNetWeight == 0) return 0;`-style guards.

### Finding Description
`DelegateResourceActuator.validate()` computes `netUsage`/`energyUsage` by dividing by `dynamicStore.getTotalNetLimit()` and `dynamicStore.getTotalEnergyCurrentLimit()` directly: [2](#0-1) 

The same unguarded division pattern also appears in `Wallet.calcCanDelegatedBandWidthMaxSize` / `calcCanDelegatedEnergyMaxSize`, which back the `GetCanDelegatedMaxSize` gRPC/HTTP query endpoint: [3](#0-2) [4](#0-3) 

By contrast, the "authoritative" resource-limit computation in `BandwidthProcessor`/`EnergyProcessor` explicitly guards the same divisor (`totalNetWeight`/`totalEnergyWeight` — the companion value) against zero before dividing: [5](#0-4) [6](#0-5) 

`RepositoryImpl.calculateGlobalEnergyLimit` even contains a bare `assert totalEnergyWeight > 0;` (line 1001) rather than a real validation, which is a no-op in production builds (assertions disabled by default), confirming that the "divisor can never be zero" assumption is not actually enforced end-to-end for the related total-limit/weight values.

This mirrors the reported bug class exactly: the actuator/validator responsible for *committing* the state (here, `ProposalUtil` approving a governance change) does not exclude zero for a value later used unconditionally as a divisor by other code paths that trust the "will never be zero" invariant.

### Impact Explanation
If `TOTAL_NET_LIMIT` or `TOTAL_CURRENT_ENERGY_LIMIT` is driven to `0` via a passed committee proposal (a normal, reachable governance action — not a "malicious SR" attack, since the value is explicitly within the documented valid range `[0, 1_000_000_000_000L]`), any subsequent call to `DelegateResourceActuator.validate()` for `BANDWIDTH`/`ENERGY` delegation divides by zero. In Java, dividing a non-zero `double` by `0.0` yields `Infinity`/`NaN` rather than throwing, so `netUsage`/`energyUsage` becomes `Infinity` or overflow-derived garbage on cast to `long` (undefined/implementation-specific `long` value, often `Long.MAX_VALUE` or a wrapped negative number). This corrupts the comparison `ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance`, which can incorrectly reject *all* legitimate delegate-resource transactions network-wide (denial of resource-delegation functionality) or, depending on how the corrupted value flows into later usage/limit bookkeeping, permit inconsistent bandwidth/energy accounting. The `Wallet.calcCanDelegatedBandWidthMaxSize`/`calcCanDelegatedEnergyMaxSize` query paths would similarly return corrupted (`Infinity`/garbage-derived) results to any API caller via `GetCanDelegatedMaxSize`, breaking a public query endpoint node-wide.

### Likelihood Explanation
Reaching this requires the committee proposal for `TOTAL_NET_LIMIT`/`TOTAL_CURRENT_ENERGY_LIMIT` to pass with value `0` — a governance action, not a single unprivileged transaction, so likelihood is lower than a fully user-triggerable bug. However, unlike the report's "accidental" framing, `0` is an *explicitly permitted* value per the current validator code (no lower bound above 0), making this a real, standing gap rather than a theoretical one; any committee member proposing/approving `0` (intentionally or by mistake) triggers it network-wide with no additional preconditions.

### Recommendation
In `ProposalUtil.validator()`, reject `value == 0` for `TOTAL_NET_LIMIT` and `TOTAL_CURRENT_ENERGY_LIMIT` (require `value > 0`), consistent with their use as divisors. Additionally, add defensive zero-checks in `DelegateResourceActuator.validate()` and in `Wallet.calcCanDelegatedBandWidthMaxSize`/`calcCanDelegatedEnergyMaxSize` before dividing by `getTotalNetLimit()`/`getTotalEnergyCurrentLimit()`, mirroring the existing guards in `BandwidthProcessor.calculateGlobalNetLimit`/`EnergyProcessor.calculateGlobalEnergyLimit`.

### Proof of Concept
1. Committee proposes and approves a chain parameter change setting `TOTAL_NET_LIMIT` (proposal code 37, per `ProposalType`) to `0`. `ProposalUtil.validator()` accepts this since it only checks `value < 0 || value > 1_000_000_000_000L` [1](#0-0) .
2. After the maintenance cycle applies the proposal, `dynamicPropertiesStore.getTotalNetLimit()` returns `0`.
3. Any account submits a `DelegateResourceContract` for `BANDWIDTH`. `DelegateResourceActuator.validate()` executes:
   `long netUsage = (long) (accountNetUsage * TRX_PRECISION * ((double)(dynamicStore.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));`
   with `getTotalNetLimit() == 0`, producing `Infinity`/garbage `long` after cast [7](#0-6) .
4. The subsequent balance check `ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance` misbehaves for every delegation transaction network-wide, and equivalent corruption occurs in the `GetCanDelegatedMaxSize` query path via `Wallet.calcCanDelegatedBandWidthMaxSize`.

### Citations

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L506-515)
```java
      case TOTAL_NET_LIMIT: {
        if (!forkController.pass(ForkBlockVersionEnum.VERSION_4_3)) {
          throw new ContractValidateException("Bad chain parameter id [TOTAL_NET_LIMIT]");
        }
        if (value < 0 || value > 1_000_000_000_000L) {
          throw new ContractValidateException(
              "Bad chain parameter value, valid range is [0, 1_000_000_000_000L]");
        }
        break;
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L162-177)
```java
        long netUsage = (long) (accountNetUsage * TRX_PRECISION * ((double)
            (dynamicStore.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));
        long v2NetUsage = getV2NetUsage(ownerCapsule, netUsage,
            this.disableJavaLangMath());
        if (ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage < delegateBalance) {
          throw new ContractValidateException(
              "delegateBalance must be less than or equal to available FreezeBandwidthV2 balance");
        }
      }
      break;
      case ENERGY: {
        EnergyProcessor processor = new EnergyProcessor(dynamicStore, accountStore);
        processor.updateUsage(ownerCapsule);

        long energyUsage = (long) (ownerCapsule.getEnergyUsage() * TRX_PRECISION * ((double)
            (dynamicStore.getTotalEnergyWeight()) / dynamicStore.getTotalEnergyCurrentLimit()));
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1010-1017)
```java
    long netUsage = (long) (accountNetUsage * TRX_PRECISION * ((double)
            (dynamicStore.getTotalNetWeight()) / dynamicStore.getTotalNetLimit()));

    long v2NetUsage = getV2NetUsage(ownerCapsule, netUsage, dynamicStore.disableJavaLangMath());

    long maxSize = ownerCapsule.getFrozenV2BalanceForBandwidth() - v2NetUsage;
    return max(0, maxSize, dynamicStore.disableJavaLangMath());
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1030-1037)
```java
    long energyUsage = (long) (ownerCapsule.getEnergyUsage() * TRX_PRECISION * ((double)
            (dynamicStore.getTotalEnergyWeight()) / dynamicStore.getTotalEnergyCurrentLimit()));

    long v2EnergyUsage = getV2EnergyUsage(ownerCapsule, energyUsage,
        dynamicStore.disableJavaLangMath());

    long maxSize =  ownerCapsule.getFrozenV2BalanceForEnergy() - v2EnergyUsage;
    return max(0, maxSize, dynamicStore.disableJavaLangMath());
```

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L440-453)
```java
    long totalNetLimit = chainBaseManager.getDynamicPropertiesStore().getTotalNetLimit();
    long totalNetWeight = chainBaseManager.getDynamicPropertiesStore().getTotalNetWeight();
    if (dynamicPropertiesStore.allowNewReward() && totalNetWeight <= 0) {
      return 0;
    }
    if (totalNetWeight == 0) {
      return 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV1(frozeBalance, totalNetLimit, totalNetWeight);
    }
    long netWeight = frozeBalance / TRX_PRECISION;
    return (long) (netWeight * ((double) totalNetLimit / totalNetWeight));
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L153-166)
```java

    long totalEnergyLimit = dynamicPropertiesStore.getTotalEnergyCurrentLimit();
    long totalEnergyWeight = dynamicPropertiesStore.getTotalEnergyWeight();
    if (dynamicPropertiesStore.allowNewReward() && totalEnergyWeight <= 0) {
      return 0;
    } else {
      assert totalEnergyWeight > 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV1(frozeBalance, totalEnergyLimit, totalEnergyWeight);
    }
    long energyWeight = frozeBalance / TRX_PRECISION;
    return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
  }
```
