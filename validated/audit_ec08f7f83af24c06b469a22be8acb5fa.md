### Title
Governance proposal parameters `ENERGY_FEE` and `EXCHANGE_CREATE_FEE` bypass all range validation, enabling protocol-wide fee bricking or unbacked-balance minting - ([File: actuator/src/main/java/org/tron/core/utils/ProposalUtil.java])

### Summary
`ProposalUtil.validator()` is the single validation gate that `ProposalCreateActuator` calls on every chain-parameter proposal before a witness's transaction is accepted into the `ProposalStore` and, after committee approval, applied into `DynamicPropertiesStore`. While nearly every numeric parameter in the switch statement enforces an explicit `[0, LONG_VALUE]` or type-specific bound, the `ENERGY_FEE` and `EXCHANGE_CREATE_FEE` cases fall straight through to `break;` with **no bound check at all**, unlike their sibling fee parameters.

### Finding Description
In `ProposalUtil.validator()`, most fee-denominated parameters are validated identically: [1](#0-0) 

But two parameters skip this check entirely: [2](#0-1) 

Any signed transaction of type `ProposalCreateContract` from an existing witness reaches this code via: [3](#0-2) 

Because `validateValue()` unconditionally calls `ProposalUtil.validator()` for every parameter in the map, and `ENERGY_FEE` (code 11) / `EXCHANGE_CREATE_FEE` (code 12) hit the unguarded case, a proposal can set these to any `long` value — including negative values or `Long.MAX_VALUE` — and it will pass `validate()`. Once approved by the committee, the value is written verbatim into `DynamicPropertiesStore` by `ProposalService` during block maintenance with no additional bound enforcement.

`ENERGY_FEE` is the global multiplier used everywhere energy consumption is converted into TRX cost (fee deduction/burn in `VMActuator`, `ReceiptCapsule`, `Wallet`, `TronJsonRpcImpl`). `EXCHANGE_CREATE_FEE` is used directly as `ExchangeCreateActuator.calcFee()` and subtracted from an account's balance on every exchange creation.

### Impact Explanation
- If `ENERGY_FEE` is set negative, every subsequent smart-contract transaction that burns "negative" energy cost effectively **credits** TRX back to the black hole/fee-payer path instead of debiting it, producing unbacked balance creation network-wide on every contract call — a chain-wide fund-integrity break.
- If `ENERGY_FEE` (or `EXCHANGE_CREATE_FEE`) is set to an extreme value such as `Long.MAX_VALUE`, downstream fee multiplication (`energyUsed * energyFee`) can overflow or make every contract call/exchange creation immediately fail for insufficient balance, permanently bricking TVM contract execution or exchange creation chain-wide until a further governance proposal — itself requiring the same currently-unbounded validator — corrects it.
- Both are exactly the "settings bricking / fund integrity" pattern described in the reference finding (unbounded governance-settable parameter with no sane min/max), but here the blast radius is broader because `ENERGY_FEE` gates all TVM contract execution, not just a single feature.

This satisfies the Medium/High bar: concrete unbacked-balance risk and node-wide functionality freeze reachable purely from a signed witness transaction plus committee approval (no privileged/malicious-SR assumption needed beyond the existing witness-proposal governance flow).

### Likelihood Explanation
Low-to-moderate: requires a witness to submit the proposal and a quorum of witnesses to approve it (same likelihood profile judged Medium in the original nouns-builder report — "not in the community's interest, but possible via miscalculation, misunderstanding, or compromised/collusive witnesses"). No validation exists to stop it even if attempted deliberately or accidentally.

### Recommendation
Add the same bound check used for the other fee-type parameters (`value < 0 || value > LONG_VALUE`, or a tighter economically sane bound) to the `ENERGY_FEE` and `EXCHANGE_CREATE_FEE` cases in `ProposalUtil.validator()`:
```java
case ENERGY_FEE:
case EXCHANGE_CREATE_FEE: {
  if (value < 0 || value > LONG_VALUE) {
    throw new ContractValidateException(LONG_VALUE_ERROR);
  }
  break;
}
```

### Proof of Concept
1. A registered witness submits a `ProposalCreateContract` transaction with `parameters = {11: -1}` (code 11 = `ENERGY_FEE`).
2. `ProposalCreateActuator.validate()` calls `ProposalUtil.validator(..., 11, -1)`, which matches `case ENERGY_FEE: break;` — no exception thrown, proposal accepted.
3. Sufficient witnesses call `ProposalApproveActuator` to approve; at the next maintenance cycle `ProposalService` applies the parameter, calling `dynamicPropertiesStore.saveEnergyFee(-1)` with no re-validation.
4. From this point, every TVM transaction's energy-to-TRX fee computation uses `energyFee = -1`, causing incorrect (negative) fee debits/credits in `VMActuator`/`ReceiptCapsule`, i.e., unbacked TRX minting on every contract execution — or, with `Long.MAX_VALUE`, all future contract calls fail, freezing all TVM functionality network-wide.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ProposalCreateActuator.java (L110-125)
```java
    if (contract.getParametersMap().size() == 0) {
      throw new ContractValidateException("This proposal has no parameter.");
    }

    for (Map.Entry<Long, Long> entry : contract.getParametersMap().entrySet()) {
      validateValue(entry);
    }

    return true;
  }

  private void validateValue(Map.Entry<Long, Long> entry) throws ContractValidateException {
    ProposalUtil
        .validator(chainBaseManager.getDynamicPropertiesStore(), forkController, entry.getKey(),
            entry.getValue());
  }
```
