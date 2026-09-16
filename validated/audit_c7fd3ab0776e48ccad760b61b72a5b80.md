Based on the codebase evidence gathered, I found a strong analog to the flashloan griefing pattern.

### Title
Unauthorized draining of a smart contract owner's TRX balance via repeated `TriggerSmartContract` calls that exhaust sponsored energy and fall back to charging the origin's real balance - ([File: chainbase/src/main/java/org/tron/core/capsule/ReceiptCapsule.java])

### Summary
When a contract owner sets `consumeUserResourcePercent < 100` on their smart contract (a supported, common configuration to sponsor callers' energy costs), any unprivileged caller can invoke the contract to force the owner ("origin") account to pay part of the energy bill. Once the origin's staked/frozen energy is exhausted, the code silently falls back to deducting the deficit directly from the origin's live TRX balance, with no cumulative cap beyond a per-transaction `originEnergyLimit`. This lets a griefer repeatedly trigger the contract to drain the owner's real balance, exactly like the referenced flashloan griefing bug where anyone could call `flashLoan` on behalf of an approved victim contract to burn its funds.

### Finding Description
`TransactionTrace.pay()` resolves the `origin` account (contract deployer) and `caller` account (transaction sender) and computes the percentage split of energy usage the origin must cover based on `contractCapsule.getConsumeUserResourcePercent(...)`: [1](#0-0) 

This is passed into `ReceiptCapsule.payEnergyBill`, which computes `originUsage` and calls `energyProcessor.useEnergy(origin, originUsage, now)` and then `payEnergyBill` for the origin separately from the caller: [2](#0-1) 

Inside the private `payEnergyBill(... AccountCapsule account, long usage ...)` helper, if the account's left energy (from freezing) is less than the usage, the shortfall is billed as a TRX fee directly deducted from `account.getBalance()`: [3](#0-2) 

Because `account` here is the `origin` (contract owner) whenever `caller != origin`, and any external, unprivileged party can send a `TriggerSmartContract` transaction naming that contract, the owner's real TRX balance is at risk any time their frozen energy allotment runs out - even though the owner only intended to sponsor a bounded amount of energy per call (governed by `originEnergyLimit`, checked in `getOriginUsage`): [4](#0-3) 

There is no protection preventing an attacker from calling the contract over and over, each time draining a bit more of the owner's balance once frozen energy is depleted, similar to how the Join contract's `flashLoan` could be invoked repeatedly against an approved third-party contract to erode its balance via unwanted fee charges.

### Impact Explanation
Any account with `consumeUserResourcePercent < 100` (the standard, documented mechanism to subsidize dApp users) is exposed to unauthorized, repeated draining of its live TRX balance by anonymous callers, once its staked/frozen energy for that window is exhausted. This is a direct loss of funds for the contract owner triggered entirely by third-party, unprivileged transactions, matching "unauthorized account operation, theft ... of funds."

### Likelihood Explanation
Likelihood is high for any deployed contract using partial energy sponsorship (a common and encouraged pattern in TRON dApps for onboarding users). An attacker only needs to know the contract address and repeatedly submit low-cost `TriggerSmartContract` transactions; no special privileges, whitelisting, or victim interaction beyond the initial deployment settings are required.

### Recommendation
Introduce a cumulative, owner-controlled ceiling (beyond the per-transaction `originEnergyLimit`) on how much real TRX balance can be consumed to cover sponsored energy over time, and/or require the fallback-to-balance charge to be opt-in separately from the energy-sponsorship percentage, so that exhausting frozen energy does not silently expose the owner's spendable balance to arbitrary callers.

### Proof of Concept
1. Deploy a contract with `consumeUserResourcePercent` set below 100 and a nonzero `originEnergyLimit`, funding the owner account with both frozen energy and a liquid TRX balance.
2. As an unrelated third party, repeatedly send `TriggerSmartContract` transactions invoking a non-trivial function on the contract.
3. Observe via `TransactionTrace.pay()` → `ReceiptCapsule.payEnergyBill()` that once `getAccountLeftEnergyFromFreeze(origin)` is exhausted, the code computes `energyFee = (usage - accountEnergyLeft) * sunPerEnergy` and deducts it from `account.setBalance(balance - energyFee)` for the origin account, on every subsequent call, with no limitation preventing continued draining across many transactions. [5](#0-4)

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/TransactionTrace.java (L239-256)
```java
      case TRX_CONTRACT_CALL_TYPE:
        TriggerSmartContract callContract = ContractCapsule
            .getTriggerContractFromTransaction(trx.getInstance());
        ContractCapsule contractCapsule =
            contractStore.get(callContract.getContractAddress().toByteArray());

        callerAccount = callContract.getOwnerAddress().toByteArray();
        originAccount = contractCapsule.getOriginAddress();
        boolean disableJavaLangMath = dynamicPropertiesStore.disableJavaLangMath();
        percent = max(Constant.ONE_HUNDRED - contractCapsule.getConsumeUserResourcePercent(
            disableJavaLangMath), 0, disableJavaLangMath);
        percent = min(percent, Constant.ONE_HUNDRED,
            disableJavaLangMath);
        originEnergyLimit = contractCapsule.getOriginEnergyLimit();
        break;
      default:
        return;
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ReceiptCapsule.java (L223-239)
```java
    if ((!Objects.isNull(origin)) && caller.getAddress().equals(origin.getAddress())) {
      payEnergyBill(dynamicPropertiesStore, accountStore, forkController, caller,
          receipt.getEnergyUsageTotal(), receipt.getResult(), energyProcessor, now);
    } else {
      long originUsage = multiplyExact(receipt.getEnergyUsageTotal(), percent, disableJavaLangMath)
          / 100;
      originUsage = getOriginUsage(dynamicPropertiesStore, origin, originEnergyLimit,
          energyProcessor,
          originUsage);

      long callerUsage = receipt.getEnergyUsageTotal() - originUsage;
      energyProcessor.useEnergy(origin, originUsage, now);
      this.setOriginEnergyUsage(originUsage);
      payEnergyBill(dynamicPropertiesStore, accountStore, forkController,
          caller, callerUsage, receipt.getResult(), energyProcessor, now);
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ReceiptCapsule.java (L241-258)
```java
  private long getOriginUsage(DynamicPropertiesStore dynamicPropertiesStore, AccountCapsule origin,
      long originEnergyLimit,
      EnergyProcessor energyProcessor, long originUsage) {
    boolean disableJavaLangMath = dynamicPropertiesStore.disableJavaLangMath();
    if (dynamicPropertiesStore.getAllowTvmFreeze() == 1
        || dynamicPropertiesStore.supportUnfreezeDelay()) {
      return min(originUsage, min(originEnergyLeft, originEnergyLimit, disableJavaLangMath),
          disableJavaLangMath);
    }

    if (checkForEnergyLimit(dynamicPropertiesStore)) {
      return min(originUsage,
          min(energyProcessor.getAccountLeftEnergyFromFreeze(origin), originEnergyLimit,
              disableJavaLangMath), disableJavaLangMath);
    }
    return min(originUsage, energyProcessor.getAccountLeftEnergyFromFreeze(origin),
        disableJavaLangMath);
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ReceiptCapsule.java (L260-317)
```java
  private void payEnergyBill(
      DynamicPropertiesStore dynamicPropertiesStore, AccountStore accountStore,
      ForkController forkController,
      AccountCapsule account,
      long usage,
      contractResult contractResult,
      EnergyProcessor energyProcessor,
      long now) throws BalanceInsufficientException {
    long accountEnergyLeft;
    if (dynamicPropertiesStore.getAllowTvmFreeze() == 1
        || dynamicPropertiesStore.supportUnfreezeDelay()) {
      accountEnergyLeft = callerEnergyLeft;
    } else {
      accountEnergyLeft = energyProcessor.getAccountLeftEnergyFromFreeze(account);
    }
    if (accountEnergyLeft >= usage) {
      energyProcessor.useEnergy(account, usage, now);
      this.setEnergyUsage(usage);
    } else {
      energyProcessor.useEnergy(account, accountEnergyLeft, now);

      if (forkController.pass(ForkBlockVersionEnum.VERSION_3_6_5) &&
          dynamicPropertiesStore.getAllowAdaptiveEnergy() == 1) {
        long blockEnergyUsage =
            dynamicPropertiesStore.getBlockEnergyUsage() + (usage - accountEnergyLeft);
        dynamicPropertiesStore.saveBlockEnergyUsage(blockEnergyUsage);
      }

      long sunPerEnergy = Constant.SUN_PER_ENERGY;
      long dynamicEnergyFee = dynamicPropertiesStore.getEnergyFee();
      if (dynamicEnergyFee > 0) {
        sunPerEnergy = dynamicEnergyFee;
      }
      long energyFee =
          (usage - accountEnergyLeft) * sunPerEnergy;
      this.setEnergyUsage(accountEnergyLeft);
      this.setEnergyFee(energyFee);
      long balance = account.getBalance();
      if (balance < energyFee) {
        throw new BalanceInsufficientException(
            StringUtil.createReadableString(account.createDbKey()) + " insufficient balance");
      }
      account.setBalance(balance - energyFee);

      if (dynamicPropertiesStore.supportTransactionFeePool() &&
          !contractResult.equals(contractResult.OUT_OF_TIME)) {
        dynamicPropertiesStore.addTransactionFeePool(energyFee);
      } else if (dynamicPropertiesStore.supportBlackHoleOptimization()) {
        dynamicPropertiesStore.burnTrx(energyFee);
      } else {
        //send to blackHole
        Commons.adjustBalance(accountStore, accountStore.getBlackhole(),
            energyFee, dynamicPropertiesStore.disableJavaLangMath());
      }

    }

    accountStore.put(account.getAddress().toByteArray(), account);
```
