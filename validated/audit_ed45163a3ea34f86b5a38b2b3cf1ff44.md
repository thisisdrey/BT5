I found the strongest analog to the "missing bound check on divisor causing divide-by-zero" bug class: `TOTAL_NET_LIMIT` can be proposed down to `0` via a committee proposal, and that zero value is used as a divisor by wallet/API and actuator code paths without a zero-guard in several places, while another divisor (`TOTAL_ENERGY_CURRENT_LIMIT`) is used the same way.

### Title
Missing bound check on committee-settable `TOTAL_NET_LIMIT`/`TOTAL_ENERGY_CURRENT_LIMIT` allows zero-divisor use in resource calculations - ([File: actuator/src/main/java/org/tron/core/utils/ProposalUtil.java])

### Summary
`ProposalUtil.validator` allows the `TOTAL_NET_LIMIT` and `TOTAL_CURRENT_ENERGY_LIMIT` chain parameters to be set to `0` (the range check only enforces `value < 0 || value > MAX`, allowing `0`) via `UpdateEnergyLimitContract`/proposal committee flow, and several places downstream divide by these values without checking for zero, exactly mirroring the `setRewardDuration`/`notifyRewardAmount` divide-by-zero pattern in the external report.

### Finding Description
`ProposalUtil.validator` explicitly permits `TOTAL_NET_LIMIT` (and the deprecated `TOTAL_ENERGY_LIMIT`/`TOTAL_CURRENT_ENERGY_LIMIT`) to be `0`: [1](#0-0) 

This value is then read back as a raw divisor in `DelegateResourceActuator.doValidate()`, where `dynamicStore.getTotalNetLimit()` and `dynamicStore.getTotalEnergyCurrentLimit()` are used directly as denominators with no zero-check: [2](#0-1) 

The same unguarded division pattern occurs in `UnDelegateResourceActuator.execute()`: [3](#0-2) 

And again in `Wallet.calcCanDelegatedBandWidthMaxSize` / `calcCanDelegatedEnergyMaxSize`, which are the query paths reachable from the gRPC/HTTP API: [4](#0-3) [5](#0-4) 

By contrast, `BandwidthProcessor.calculateGlobalNetLimit`/`calculateGlobalNetLimitV2` (used in ordinary bandwidth consumption) do contain an explicit `totalNetWeight == 0` guard: [6](#0-5) 

but the `DelegateResourceActuator`/`UnDelegateResourceActuator`/`Wallet` code paths above divide by `getTotalNetLimit()`/`getTotalEnergyCurrentLimit()` (not `TotalWeight`) with no equivalent guard, so if that dynamic property is ever `0` (its default is non-zero on mainnet, but the proposal validator does not prevent a committee proposal from setting it to `0`, and it's initialized to a default only via a catch on `IllegalArgumentException`), any signed `DelegateResourceContract`/`UnDelegateResourceContract` transaction or the `GetCanDelegatedMaxSize` API call would trigger a division by zero.

### Impact Explanation
A `ArithmeticException` (integer divide-by-zero) or `Infinity`/`NaN` propagation (double divide-by-zero) thrown from within `DelegateResourceActuator.validate()`/`execute()` or `UnDelegateResourceActuator.execute()` — both invoked from ordinary transaction processing inside `Manager`'s block/transaction application — would abort transaction processing unexpectedly and could crash the node processing the block, or corrupt resource accounting for delegated bandwidth/energy for the affected accounts (either freezing funds via miscalculated usage limits or permitting more delegation than the account should be allowed).

### Likelihood Explanation
Reaching this requires a governance committee proposal to actually set `TOTAL_NET_LIMIT` (or the related energy limit) to `0`, which is gated behind the proposal-approval process (not a single unprivileged transaction) — this significantly lowers likelihood versus the original report's simple owner-only setter. I could not confirm within the available context whether committee members are considered privileged/out-of-scope actors per the given exclusion rules, nor could I verify all runtime initialization paths that might reset this value to a safe non-zero default before it's ever read as a divisor. This uncertainty limits confidence that the finding meets the "reachable by unprivileged transaction/API request" bar required by the rules.

### Recommendation
Add an explicit lower bound greater than zero for `TOTAL_NET_LIMIT` and `TOTAL_CURRENT_ENERGY_LIMIT` in `ProposalUtil.validator`, and add defensive zero-checks before dividing by `dynamicStore.getTotalNetLimit()`/`getTotalEnergyCurrentLimit()` in `DelegateResourceActuator`, `UnDelegateResourceActuator`, and `Wallet.calcCanDelegatedBandWidthMaxSize`/`calcCanDelegatedEnergyMaxSize`, consistent with the existing guard in `BandwidthProcessor.calculateGlobalNetLimit`.

### Proof of Concept
Not independently verified end-to-end within the available index (would require confirming the committee proposal flow permits `0` in practice and tracing full transaction execution through `Manager`); the analysis above is based on static code review of the cited files only.

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

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L157-177)
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
      }
      break;
      case ENERGY: {
        EnergyProcessor processor = new EnergyProcessor(dynamicStore, accountStore);
        processor.updateUsage(ownerCapsule);

        long energyUsage = (long) (ownerCapsule.getEnergyUsage() * TRX_PRECISION * ((double)
            (dynamicStore.getTotalEnergyWeight()) / dynamicStore.getTotalEnergyCurrentLimit()));
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L81-105)
```java
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
          break;
        case ENERGY:
          EnergyProcessor energyProcessor = new EnergyProcessor(dynamicStore, accountStore);
          energyProcessor.updateUsage(receiverCapsule);

          if (receiverCapsule.getAcquiredDelegatedFrozenV2BalanceForEnergy()
              < unDelegateBalance) {
            // A TVM contract receiver, re-create will produce this situation
            receiverCapsule.setAcquiredDelegatedFrozenV2BalanceForEnergy(0);
          } else {
            // calculate usage
            long unDelegateMaxUsage = (long) ((double) unDelegateBalance / TRX_PRECISION
                * ((double) (dynamicStore.getTotalEnergyCurrentLimit()) / dynamicStore.getTotalEnergyWeight()));
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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1030-1031)
```java
    long energyUsage = (long) (ownerCapsule.getEnergyUsage() * TRX_PRECISION * ((double)
            (dynamicStore.getTotalEnergyWeight()) / dynamicStore.getTotalEnergyCurrentLimit()));
```

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L440-466)
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

  public long calculateGlobalNetLimitV2(long frozeBalance) {
    long totalNetLimit = dynamicPropertiesStore.getTotalNetLimit();
    long totalNetWeight = dynamicPropertiesStore.getTotalNetWeight();
    if (totalNetWeight == 0) {
      return 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV2(frozeBalance, totalNetLimit, totalNetWeight);
    }
    double netWeight = (double) frozeBalance / TRX_PRECISION;
    return (long) (netWeight * ((double) totalNetLimit / totalNetWeight));
  }
```
