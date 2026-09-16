### Title
Missing upper-bound validation on `ENERGY_FEE` / `EXCHANGE_CREATE_FEE` proposal parameters allows committee to set unbounded fees - ([File: actuator/src/main/java/org/tron/core/utils/ProposalUtil.java])

### Summary
`ProposalUtil.validator()` enforces a strict numeric range `[0, 100_000_000_000_000_000L]` for sibling fee-type chain parameters (`ACCOUNT_UPGRADE_COST`, `CREATE_ACCOUNT_FEE`, `TRANSACTION_FEE`, `ASSET_ISSUE_FEE`, `WITNESS_PAY_PER_BLOCK`, `WITNESS_STANDBY_ALLOWANCE`, `CREATE_NEW_ACCOUNT_FEE_IN_SYSTEM_CONTRACT`, `CREATE_NEW_ACCOUNT_BANDWIDTH_RATE`), but the `ENERGY_FEE` and `EXCHANGE_CREATE_FEE` cases fall through with no bound check at all, allowing these values to be set to any `long`, including negative values or `Long.MAX_VALUE`.

### Finding Description
In `ProposalUtil.validator()`, the switch statement groups several fee parameters under one validation branch that restricts their value to `[0, LONG_VALUE]`: [1](#0-0) 

Immediately below it, `ENERGY_FEE` and `EXCHANGE_CREATE_FEE` are handled by an empty case that performs no validation whatsoever: [2](#0-1) 

This is the exact same validator invoked both when a witness creates a proposal (`ProposalCreateActuator.validateValue`) and, more critically, this check has no effect on what actually gets persisted once a proposal is approved: `ProposalService.process()` writes the raw, unvalidated `entry.getValue()` directly into the dynamic properties store via `saveEnergyFee`: [3](#0-2) 

`ENERGY_FEE` is the sun-per-energy-unit price charged to every account executing energy-consuming TVM operations (smart contract calls), consumed throughout `VMActuator`, `ReceiptCapsule`, and `Wallet`. Because there is no upper bound enforced when the value is proposed/approved, a committee majority (analogous to the report's "restricted" `onlyConfigMaster` role) can set `ENERGY_FEE` to an arbitrarily large value (or to a negative value, since even the `value < 0` check present for other parameters is absent here). This mirrors the reported bug class: a fee parameter that governs how much of users' balances are burned on execution has no protective ceiling consistent with the documented/expected numeric guardrails applied to sibling parameters in the very same function.

### Impact Explanation
If this parameter is set to an extreme value, every account that triggers a smart contract call afterward will be charged an extortionate amount of TRX for energy consumption, resulting in unauthorized draining of user balances network-wide — a direct "theft of funds" impact reachable purely through the committee proposal mechanism, with no additional guardrail comparable to the ones protecting other, functionally identical fee parameters.

### Likelihood Explanation
Requires committee approval (majority of active witnesses), the same trust model as the "restricted admin" described in the original report. The likelihood is analogous to the original finding: it requires privileged-but-restricted actors, and the code path is directly reachable through the standard `ProposalCreateContract`/`ProposalApproveContract` transaction flow with no additional protection.

### Recommendation
Add the same `[0, LONG_VALUE]` bound check (or an appropriately tighter, documented cap) to the `ENERGY_FEE` and `EXCHANGE_CREATE_FEE` cases in `ProposalUtil.validator()`, consistent with the other fee-type parameters validated in the preceding case block.

### Proof of Concept
1. A witness submits a `ProposalCreateContract` with parameter id `11` (`ENERGY_FEE`) and value `Long.MAX_VALUE` (or any value far exceeding `100_000_000_000_000_000L`).
2. `ProposalCreateActuator.validate()` calls `ProposalUtil.validator()`, which hits the `case ENERGY_FEE: case EXCHANGE_CREATE_FEE: break;` branch and returns without throwing, unlike what would happen for `TRANSACTION_FEE` or `ASSET_ISSUE_FEE` with the same out-of-range value.
3. Once approved by the required witness quorum, `ProposalController.processProposal()` → `ProposalService.process()` calls `dynamicPropertiesStore.saveEnergyFee(Long.MAX_VALUE)` unconditionally.
4. Every subsequent transaction that consumes energy is now charged based on this unbounded fee, draining user account balances far beyond any reasonable amount.

### Citations

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L42-54)
```java
      case ACCOUNT_UPGRADE_COST:
      case CREATE_ACCOUNT_FEE:
      case TRANSACTION_FEE:
      case ASSET_ISSUE_FEE:
      case WITNESS_PAY_PER_BLOCK:
      case WITNESS_STANDBY_ALLOWANCE:
      case CREATE_NEW_ACCOUNT_FEE_IN_SYSTEM_CONTRACT:
      case CREATE_NEW_ACCOUNT_BANDWIDTH_RATE: {
        if (value < 0 || value > LONG_VALUE) {
          throw new ContractValidateException(LONG_VALUE_ERROR);
        }
        break;
      }
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L74-76)
```java
      case ENERGY_FEE:
      case EXCHANGE_CREATE_FEE:
        break;
```

**File:** framework/src/main/java/org/tron/core/consensus/ProposalService.java (L83-90)
```java
        case ENERGY_FEE: {
          manager.getDynamicPropertiesStore().saveEnergyFee(entry.getValue());
          // update energy price history
          manager.getDynamicPropertiesStore().saveEnergyPriceHistory(
              manager.getDynamicPropertiesStore().getEnergyPriceHistory()
                  + "," + proposalCapsule.getExpirationTime() + ":" + entry.getValue());
          break;
        }
```
