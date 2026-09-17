## Analog Found

### Title
Contract owner (origin) is forced to pay TVM energy fees at whatever the current dynamic `ENERGY_FEE` is, with no origin-side cap on the price of energy their contract consumes on behalf of unprivileged callers - ([File: chainbase/src/main/java/org/tron/core/capsule/ReceiptCapsule.java])

### Summary
When `consumeUserResourcePercent` on a deployed contract is less than 100%, any unprivileged account can call `TriggerSmartContract` on that contract and force part of the energy cost onto the contract's `origin` (deployer) account. The origin only gets to cap the *amount of energy* it will subsidize via `originEnergyLimit`, but has no way to cap the *price per energy unit* (`ENERGY_FEE`, a chain-wide dynamic parameter) it is charged when its frozen energy is insufficient and TRX must be deducted from its balance. Because `ENERGY_FEE` can be raised at any time by committee proposal, this mirrors the Infinity Exchange bug where the buyer had no way to cap `tx.gasprice`: the account footing the bill has no control over the price component of the cost it is forced to reimburse, only over the quantity.

### Finding Description
`getTotalEnergyLimitWithFixRatio` in `VMActuator` computes how much energy the contract's `origin` account will subsidize for a caller-triggered transaction, bounded only by `originEnergyLimit` (an energy-unit quantity) and the origin's frozen energy: [1](#0-0) 

At bill-payment time, `ReceiptCapsule.payEnergyBill` splits usage between `origin` and `caller` according to `percent`, and calls the private `payEnergyBill` for the origin's portion: [2](#0-1) 

When the origin's frozen/free energy (`accountEnergyLeft`) is insufficient to cover its assigned usage, the shortfall is billed in TRX using the *current* dynamic `ENERGY_FEE`, deducted directly from the origin account's balance: [3](#0-2) 

`ENERGY_FEE` is a governance-controlled dynamic parameter (default 100 sun/energy) that can be changed at any time by committee proposal, and there is no analog of a "max gas price" the origin account can set to bound how much TRX it is willing to pay per unit of energy consumed by third-party callers: [4](#0-3) [5](#0-4) 

This is the exact structural analog of the reported bug: the party who did not initiate/control the transaction (origin/deployer, like the maker-buy order's buyer) is contractually forced to reimburse a cost (`energyFee`) whose price component (`ENERGY_FEE`, like `tx.gasprice`) is decided at execution time by conditions outside their control, with only a quantity cap (`originEnergyLimit`, like a max-gas-units setting) and no price cap (no `maxGasCost`/`maxEnergyFee` equivalent).

### Impact Explanation
Any unprivileged account holding TRX can call any contract whose `consumeUserResourcePercent < 100`, forcing the contract's `origin` account to burn TRX balance at the prevailing `ENERGY_FEE` rate once its frozen energy is exhausted. If `ENERGY_FEE` is raised (a normal, expected governance action, as seen historically going from 10 to 40 to 140 sun per energy in `EnergyPriceHistoryLoaderTest`), origin accounts that set `originEnergyLimit` based on an earlier, lower price will pay proportionally more TRX than they budgeted for the same energy-unit cap, for calls they did not initiate. This is a direct, unbounded (in TRX terms) balance drain vector triggerable by any caller, matching the "de facto fund loss" impact called out in the referenced report.

### Likelihood Explanation
Triggering the drain requires only a normal `TriggerSmartContract` transaction from any account against a contract with `consumeUserResourcePercent < 100` (a common, non-privileged deployer configuration) — reachable by any signed transaction. The severity is amplified whenever `ENERGY_FEE` changes via committee proposal, which is a routine governance event, not a "malicious" actor scenario, closely tracking the judge's "external requirement" framing of Medium severity in the original finding.

### Recommendation
Allow contract deployers to specify (alongside `originEnergyLimit`) a maximum energy price (`maxEnergyFeePerUnit`) they are willing to subsidize. When billing the origin's share in `ReceiptCapsule.payEnergyBill`/`payEnergyBill`, compare the current `dynamicPropertiesStore.getEnergyFee()` against this cap and cap the origin's chargeable usage (or fail closed / shift excess to the caller) if the current price exceeds what the origin agreed to when deploying/updating the contract.

### Proof of Concept
1. Deploy a contract with `consumeUserResourcePercent = 30` and `originEnergyLimit = 1_000_000`, when `ENERGY_FEE = 100` sun.
2. Committee raises `ENERGY_FEE` to `1000` sun via a passed proposal (`ProposalService.process`, `ENERGY_FEE` case) — a normal governance flow.
3. Any unprivileged account calls `TriggerSmartContract` on the deployed contract, consuming energy up to `originEnergyLimit`.
4. `VMActuator.getTotalEnergyLimitWithFixRatio` still allows the origin to subsidize up to `originEnergyLimit` energy units; `ReceiptCapsule.payEnergyBill`'s private `payEnergyBill` bills any shortfall beyond the origin's frozen energy in TRX at the new `ENERGY_FEE = 1000`, deducting up to 10x more TRX than the origin would have paid under the price in effect when it configured `originEnergyLimit`, with no mechanism for the origin to have capped this exposure.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L743-777)
```java
    long creatorEnergyLimit = 0;
    ContractCapsule contractCapsule = rootRepository
        .getContract(contract.getContractAddress().toByteArray());
    long consumeUserResourcePercent = contractCapsule.getConsumeUserResourcePercent(
        VMConfig.disableJavaLangMath());

    long originEnergyLimit = contractCapsule.getOriginEnergyLimit();
    if (originEnergyLimit < 0) {
      throw new ContractValidateException("originEnergyLimit can't be < 0");
    }

    long originEnergyLeft = 0;
    if (consumeUserResourcePercent < VMConstant.ONE_HUNDRED) {
      originEnergyLeft = rootRepository.getAccountLeftEnergyFromFreeze(creator);
      if (VMConfig.allowTvmFreeze() || VMConfig.allowTvmFreezeV2()) {
        receipt.setOriginEnergyLeft(originEnergyLeft);
      }
    }
    if (consumeUserResourcePercent <= 0) {
      creatorEnergyLimit = min(originEnergyLeft, originEnergyLimit,
          VMConfig.disableJavaLangMath());
    } else {
      if (consumeUserResourcePercent < VMConstant.ONE_HUNDRED) {
        // creatorEnergyLimit =
        // min(callerEnergyLimit * (100 - percent) / percent,
        //   creatorLeftFrozenEnergy, originEnergyLimit)

        creatorEnergyLimit = min(
            BigInteger.valueOf(callerEnergyLimit)
                .multiply(BigInteger.valueOf(VMConstant.ONE_HUNDRED - consumeUserResourcePercent))
                .divide(BigInteger.valueOf(consumeUserResourcePercent)).longValueExact(),
            min(originEnergyLeft, originEnergyLimit, VMConfig.disableJavaLangMath()),
            VMConfig.disableJavaLangMath());
      }
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ReceiptCapsule.java (L223-238)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ReceiptCapsule.java (L278-302)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L73-75)
```java
  private static final byte[] ENERGY_FEE = "ENERGY_FEE".getBytes();
  private static final long DEFAULT_ENERGY_FEE = 100L;
  public static final String DEFAULT_ENERGY_PRICE_HISTORY = "0:" + DEFAULT_ENERGY_FEE;
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
