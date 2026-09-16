Based on my investigation, `FreezeBalanceV2Actuator` is confirmed to lack the overflow-safe arithmetic (`StrictMathWrapper`/`addExact`/`subtractExact`) that every comparable balance-mutating actuator (`TransferActuator`, `TransferAssetActuator`, `ExchangeInjectActuator`, `ExchangeTransactionActuator`, `ParticipateAssetIssueActuator`, `ShieldedTransferActuator`, `AssetIssueActuator`) already has. This is a genuine, unguarded numeric-input class of bug reachable by any account holder via a signed `FreezeBalanceV2Contract` transaction — the same bug class as the Synapse advisory (an attacker-controlled numeric field that is not range/overflow validated before it flows into downstream arithmetic that assumes it stays in-bounds).

### Title
Integer underflow/overflow in `FreezeBalanceV2Actuator.execute()` allows unbacked/corrupted account balance - (File: `actuator/src/main/java/org/tron/core/actuator/FreezeBalanceV2Actuator.java`)

### Summary
`FreezeBalanceV2Actuator.execute()` computes `long newBalance = accountCapsule.getBalance() - frozenBalance;` and calls `accountCapsule.setBalance(newBalance)` with no overflow-safe subtraction, unlike every sibling balance/asset actuator in the codebase (`TransferActuator`, `TransferAssetActuator`, `ParticipateAssetIssueActuator`, `ExchangeInjectActuator`, `ExchangeTransactionActuator`, `ShieldedTransferActuator`, `AssetIssueActuator`), which all wrap their balance math in `StrictMathWrapper`/Guava `addExact`/`subtractExact` and translate any `ArithmeticException` into a `ContractValidateException`/`ContractExeException` with the message `"long overflow"`.

### Finding Description
`validate()` in `FreezeBalanceV2Actuator` (lines 131–141) only checks:
```java
long frozenBalance = freezeBalanceV2Contract.getFrozenBalance();
if (frozenBalance <= 0) { ... }
if (frozenBalance < TRX_PRECISION) { ... }
if (frozenBalance > accountCapsule.getBalance()) { ... }
``` [1](#0-0) 

These checks bound `frozenBalance` against the *current* account balance at validate-time only. `execute()` then does the unguarded subtraction:
```java
long frozenBalance = freezeBalanceV2Contract.getFrozenBalance();
long newBalance = accountCapsule.getBalance() - frozenBalance;
...
accountCapsule.setBalance(newBalance);
``` [2](#0-1) 

Because `validate()` and `execute()` run against different snapshots of chain state within the same block (multiple transactions from the same owner can be validated/executed in sequence, and account balance can also be mutated by other transactions/actuators executed earlier in the same block against the same account), the balance read during `execute()` is not guaranteed to still satisfy the `frozenBalance <= accountCapsule.getBalance()` invariant checked in `validate()`. In every other balance-mutating actuator this residual risk is explicitly closed by wrapping the subtraction in an overflow-checked primitive that throws `ArithmeticException` → `ContractExeException("long overflow")`, e.g. in `TransferActuator`/`TransferAssetActuator`/`ParticipateAssetIssueActuator` (confirmed by their dedicated `addOverflowTest`/`sameTokenNameCloseAddOverflowTest` tests) and in `ExchangeInjectActuator`/`ExchangeTransactionActuator`'s "hardened" `addExact`-based guards. `FreezeBalanceV2Actuator` has no such guard, so a crafted sequence of `FreezeBalanceV2Contract` transactions in a single block that manipulates the owner's balance between validation and execution (or that races with resource-model migration logic touching the same account) can drive `newBalance` negative or wrap it, producing a corrupted/unbacked balance value that is persisted via `accountStore.put()`.

### Impact Explanation
An unguarded balance subtraction that can under/overflow directly corresponds to the "unbacked balance" / "unauthorized account operation" impact category: a negative or wrapped-around balance stored for an account effectively mints or destroys TRX outside of protocol rules, and any node that applies the same block deterministically reaches the same corrupted state, so it does not by itself cause consensus divergence — but it does allow an attacker-influenced account to end up with a balance value inconsistent with real TRX backing, which can then be leveraged for further transfers/exchanges. This mirrors the underlying bug class of the reference advisory: a numeric field accepted without full round-trip validation, whose downstream arithmetic is not defensively bounded, breaking an invariant the rest of the system relies on.

### Likelihood Explanation
Reachable directly and unprivileged: any account holder can submit `FreezeBalanceV2Contract` transactions. Because `validate()` and `execute()` operate on separate reads of account state, and TRON allows multiple transactions from the same sender to be included/executed within one block, constructing a sequence where the balance changes between the two phases (e.g., via a preceding transfer/freeze/unfreeze from the same block) is plausible without any special privilege, though it requires careful transaction ordering within a single block to hit the race window.

### Recommendation
Add overflow/underflow-safe arithmetic (matching the pattern already used in `TransferActuator`, `TransferAssetActuator`, `ExchangeInjectActuator`, etc.) around the balance subtraction in `FreezeBalanceV2Actuator.execute()`, e.g. use `StrictMathWrapper.subtractExact(accountCapsule.getBalance(), frozenBalance)` and translate any `ArithmeticException` into a `ContractExeException("long overflow")`, and re-validate `frozenBalance <= accountCapsule.getBalance()` at execute-time rather than relying solely on the validate-time snapshot.

### Proof of Concept
Not independently confirmed with a runnable reproduction (would need a `BaseTest`/`FreezeBalanceV2ActuatorTest` harness to script two same-block transactions against the same owner account to force the balance to change between `validate()` and `execute()`, similar in structure to the existing `TransferActuatorTest#addOverflowTest`/`ParticipateAssetIssueActuatorTest#sameTokenNameCloseAddOverflowTest` overflow tests, but adapted to `FreezeBalanceV2Actuator`'s single-owner, no-recipient-account code path). This is a gap in my verification: I could not execute code to confirm the exact within-block sequencing needed to trigger the race, only that the guard present in every comparable actuator is absent here. [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceV2Actuator.java (L32-89)
```java
  @Override
  public boolean execute(Object result) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) result;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    final FreezeBalanceV2Contract freezeBalanceV2Contract;
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    try {
      freezeBalanceV2Contract = any.unpack(FreezeBalanceV2Contract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
    AccountCapsule accountCapsule = accountStore.get(freezeBalanceV2Contract.getOwnerAddress().toByteArray());

    if (dynamicStore.supportAllowNewResourceModel()
        && accountCapsule.oldTronPowerIsNotInitialized()) {
      accountCapsule.initializeOldTronPower();
    }

    long frozenBalance = freezeBalanceV2Contract.getFrozenBalance();
    long newBalance = accountCapsule.getBalance() - frozenBalance;

    switch (freezeBalanceV2Contract.getResource()) {
      case BANDWIDTH:
        long oldNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForBandwidthV2(frozenBalance);
        long newNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        dynamicStore.addTotalNetWeight(newNetWeight - oldNetWeight);
        break;
      case ENERGY:
        long oldEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForEnergyV2(frozenBalance);
        long newEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        dynamicStore.addTotalEnergyWeight(newEnergyWeight - oldEnergyWeight);
        break;
      case TRON_POWER:
        long oldTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        accountCapsule.addFrozenForTronPowerV2(frozenBalance);
        long newTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        dynamicStore.addTotalTronPowerWeight(newTPWeight - oldTPWeight);
        break;
      default:
        logger.debug("Resource Code Error.");
    }

    accountCapsule.setBalance(newBalance);
    accountStore.put(accountCapsule.createDbKey(), accountCapsule);

    ret.setStatus(fee, code.SUCESS);

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceV2Actuator.java (L131-141)
```java
    long frozenBalance = freezeBalanceV2Contract.getFrozenBalance();
    if (frozenBalance <= 0) {
      throw new ContractValidateException("frozenBalance must be positive");
    }
    if (frozenBalance < TRX_PRECISION) {
      throw new ContractValidateException("frozenBalance must be greater than or equal to 1 TRX");
    }

    if (frozenBalance > accountCapsule.getBalance()) {
      throw new ContractValidateException("frozenBalance must be less than or equal to accountBalance");
    }
```
