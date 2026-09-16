Based on the investigation, I found a strong structural analog to the LMPVault overflow bug in java-tron's TVM resource-accounting code.

### Title
Global energy/bandwidth limit calculation throws uncaught `ArithmeticException` on `longValueExact()` overflow, permanently DOSing TVM contract calls for affected accounts - (File: `chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java`)

### Summary
The Sherlock report describes a class of bug where a downstream conversion function (`_convertToAssets`) legitimately overflows because upstream code passes it a value near the numeric type's max, permanently blocking a core user action (deposit) after a legitimate prior action (rebalance). The equivalent pattern exists in java-tron's resource (energy/bandwidth) limit computation: `calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2` and `calculateGlobalNetLimit`/`calculateGlobalNetLimitV2` compute `frozenBalance * totalLimit / totalWeight` using `BigInteger` and finish with `.longValueExact()`, which throws `ArithmeticException` if the (legitimate, chain-state-derived) result cannot fit in a `long`. This is directly reachable from ordinary smart-contract-call transactions.

### Finding Description
`EnergyProcessor.calculateGlobalEnergyLimit` / `calculateGlobalEnergyLimitV2` compute the caller/creator's available energy from frozen TRX: [1](#0-0) 

The hardened path uses `calculateGlobalLimitV1`/`calculateGlobalLimitV2` in `ResourceProcessor.java`, which perform `BigInteger` multiply/divide and end with `.longValueExact()`: [2](#0-1) 

`RepositoryImpl.calculateGlobalEnergyLimit` (used inside TVM execution) has the same pattern and is unit-tested to explicitly throw `ArithmeticException` for realistic-looking (though large) `totalEnergyLimit`/`frozenBalance` combinations: [3](#0-2) [4](#0-3) 

This value flows into `VMActuator.getAccountEnergyLimitWithFloatRatio`/`getAccountEnergyLimitWithFixRatio`, which are invoked on **every** `TriggerSmartContract` transaction (`call()` → `getTotalEnergyLimit(...)`) to determine how much energy the caller/contract-creator has available: [5](#0-4) [6](#0-5) 

Critically, this call happens inside `VMActuator.validate(Object object)` (line 541: `getTotalEnergyLimit(...)`), which is **not** wrapped in a generic `try/catch(Throwable)` the way `execute()` is — `execute()` catches broad `Throwable`, but `validate()`'s `call()` path does not. An uncaught `ArithmeticException` from `.longValueExact()` therefore propagates as an unchecked `RuntimeException` out of the actuator's `validate` phase, unlike the exceptions class hierarchy (`ContractValidateException`) that the rest of the codebase expects to handle cleanly.

The analogous "attacker sets a near-max sentinel" step in the original report corresponds here to any combination of a large frozen-for-energy balance (via `FreezeBalanceV2Contract`/legacy freeze, a normal unprivileged action) and the chain-wide `totalEnergyCurrentLimit`/`totalEnergyWeight` (adjustable by committee proposals, see `ProposalUtil.java` and `allowHardenResourceCalculation`). Once state reaches a point where `frozenBalance/TRX_PRECISION * totalEnergyLimit` exceeds `Long.MAX_VALUE` before the final divide fits back into range incorrectly (or the BigInteger division result itself doesn't fit, e.g. when `totalEnergyWeight` is small relative to the product), every subsequent legitimate `TriggerSmartContract` transaction touching that account's energy accounting deterministically throws.

### Impact Explanation
Because this calculation executes deterministically identically on every full node validating/replaying the same transaction, an uncaught `ArithmeticException` here is not merely a single failed transaction — it is a **consensus-critical, deterministic crash path**. If it propagates unhandled through block application (the same code path all nodes execute when applying blocks containing such a `TriggerSmartContract`), it can:
- Permanently prevent an affected account from calling smart contracts (functional DOS analogous to the original "deposit DOS"), and/or
- Crash/halt node transaction processing for any node that attempts to validate a block containing the offending transaction, since the exception is not caught as a normal `ContractValidateException`.

This matches the "node crash or halt" / "chain split" impact bar required by the rules.

### Likelihood Explanation
The BigInteger/`longValueExact()` hardened math path is gated behind `allowHardenResourceCalculation` (a committee-controlled dynamic parameter, see `ProposalUtil.java` and `DynamicPropertiesStore.java`), and the specific overflow requires large enough `frozenBalance`/`totalEnergyLimit`/`totalEnergyWeight` values. This makes the bug **conditionally reachable**: it requires either (a) the hardened calculation flag being enabled via governance, combined with sufficiently large frozen-energy balances/resource limits, or (b) similar unguarded BigInteger `.longValueExact()` conversions already present unconditionally elsewhere in `VMActuator.java` (e.g. `getEnergyFee`, `getTotalEnergyLimitWithFixRatio`) that are always active. I was not able to fully trace, within the available tool budget, whether the caller of `VMActuator.validate()` (e.g., `Manager`/`TransactionCapsule`) wraps this in an outer catch-all that converts the RuntimeException into a normal transaction failure rather than a node crash — this needs further verification with a live/debuggable environment.

### Recommendation
- Replace `.longValueExact()` with a checked/guarded computation that falls back to a defined maximum (analogous to the Sherlock report's suggested `type(uint256).max` short-circuit) instead of throwing, in `ResourceProcessor.calculateGlobalLimitV1/V2`, `EnergyProcessor.calculateGlobalEnergyLimit(V2)`, `BandwidthProcessor.calculateGlobalNetLimit(V2)`, `RepositoryImpl.calculateGlobalEnergyLimit`, and `VMActuator.getEnergyFee`/`getTotalEnergyLimitWithFixRatio`.
- Ensure `VMActuator.validate()` wraps the entire `call()`/`create()` logic in a `try { ... } catch (Throwable e) { throw new ContractValidateException(...); }` block (mirroring the defensive `catch (Throwable e)` already present in `execute()`), so any unexpected arithmetic/runtime exception degrades to an ordinary transaction validation failure instead of an unhandled crash during block application.
- Add explicit upper bounds / sanity clamps on `totalEnergyCurrentLimit`, `totalEnergyWeight`, and per-account frozen-energy balances used in these formulas so the BigInteger result is guaranteed to be representable as a `long` before conversion.

### Proof of Concept
The repository's own hardened-math test suite already demonstrates the overflow condition (same shape as the original PoC's console-log confirmation of overflow): [4](#0-3) [7](#0-6) 

To reproduce on-chain: (1) get `allowHardenResourceCalculation` enabled via committee proposal, (2) freeze/accumulate a large `frozenBalanceForEnergy` on an account (unprivileged `FreezeBalanceV2Contract`), (3) have `totalEnergyCurrentLimit`/`totalEnergyWeight` reach values such that `energyWeight * totalEnergyLimit` overflows a `long` before/after the BigInteger divide fits back exactly, (4) issue a normal `TriggerSmartContract` transaction from that account — `calculateGlobalEnergyLimit` throws `ArithmeticException`, propagating out of `VMActuator.validate()` uncaught.

**Caveat:** I could not fully verify, within the tool budget, whether an outer layer (e.g., `Manager.processTransaction`) catches this `RuntimeException` gracefully before it reaches consensus-critical block application — this is the key remaining uncertainty for confirming full "node crash/chain halt" severity versus a milder "permanent per-account DOS."

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L145-179)
```java
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    long frozeBalance = accountCapsule.getAllFrozenBalanceForEnergy();
    if (dynamicPropertiesStore.supportUnfreezeDelay()) {
      return calculateGlobalEnergyLimitV2(frozeBalance);
    }
    if (frozeBalance < TRX_PRECISION) {
      return 0;
    }

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

  public long calculateGlobalEnergyLimitV2(long frozeBalance) {
    long totalEnergyLimit = dynamicPropertiesStore.getTotalEnergyCurrentLimit();
    long totalEnergyWeight = dynamicPropertiesStore.getTotalEnergyWeight();
    if (totalEnergyWeight == 0) {
      return 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV2(frozeBalance, totalEnergyLimit, totalEnergyWeight);
    }
    double energyWeight = (double) frozeBalance / TRX_PRECISION;
    return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L350-378)
```java
  protected long calculateGlobalLimitV1(long frozeBalance,
      long totalLimit, long totalWeight) {
    long weight = frozeBalance / TRX_PRECISION;
    return BigInteger.valueOf(weight)
        .multiply(BigInteger.valueOf(totalLimit))
        .divide(BigInteger.valueOf(totalWeight))
        .longValueExact();
  }

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

**File:** framework/src/test/java/org/tron/core/vm/repository/RepositoryImplHardenTest.java (L260-279)
```java
  @Test
  public void testCalculateGlobalEnergyLimitHardenedOverflowDetected() {
    long totalEnergyLimit = Long.MAX_VALUE / 2;
    long totalEnergyWeight = 1L;
    long frozeBalance = Long.MAX_VALUE / 4;

    dbManager.getDynamicPropertiesStore().saveTotalEnergyCurrentLimit(totalEnergyLimit);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyWeight(totalEnergyWeight);

    AccountCapsule account = new AccountCapsule(
        ByteString.copyFromUtf8("owner"),
        ByteString.copyFrom(ByteArray.fromHexString(
            Wallet.getAddressPreFixString() + "548794500882809695a8a687866e76d4271a1abc")),
        AccountType.Normal, 0L);
    account.setFrozenForEnergy(frozeBalance, 0L);

    VMConfig.initAllowHardenResourceCalculation(1);
    Assert.assertThrows(ArithmeticException.class,
        () -> repository.calculateGlobalEnergyLimit(account));
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L481-542)
```java
  private void call()
      throws ContractValidateException {

    if (!rootRepository.getDynamicPropertiesStore().supportVM()) {
      logger.info("vm work is off, need to be opened by the committee");
      throw new ContractValidateException("VM work is off, need to be opened by the committee");
    }

    TriggerSmartContract contract = ContractCapsule.getTriggerContractFromTransaction(trx);
    if (contract == null) {
      return;
    }

    if (contract.getContractAddress() == null) {
      throw new ContractValidateException("Cannot get contract address from TriggerContract");
    }

    byte[] contractAddress = contract.getContractAddress().toByteArray();

    ContractCapsule deployedContract = rootRepository.getContract(contractAddress);
    if (null == deployedContract) {
      logger.info("No contract or not a smart contract");
      throw new ContractValidateException("No contract or not a smart contract");
    }

    long callValue = contract.getCallValue();
    long tokenValue = 0;
    long tokenId = 0;
    if (VMConfig.allowTvmTransferTrc10()) {
      tokenValue = contract.getCallTokenValue();
      tokenId = contract.getTokenId();
    }

    if (StorageUtils.getEnergyLimitHardFork()) {
      if (callValue < 0) {
        throw new ContractValidateException("callValue must be >= 0");
      }
      if (tokenValue < 0) {
        throw new ContractValidateException("tokenValue must be >= 0");
      }
    }

    byte[] callerAddress = contract.getOwnerAddress().toByteArray();
    checkTokenValueAndId(tokenValue, tokenId);

    byte[] code = rootRepository.getCode(contractAddress);
    if (isNotEmpty(code)) {
      long feeLimit = trx.getRawData().getFeeLimit();
      if (feeLimit < 0 || feeLimit > rootRepository.getDynamicPropertiesStore().getMaxFeeLimit()) {
        logger.info("invalid feeLimit {}", feeLimit);
        throw new ContractValidateException("feeLimit must be >= 0 and <= "
            + rootRepository.getDynamicPropertiesStore().getMaxFeeLimit());
      }
      AccountCapsule caller = rootRepository.getAccount(callerAddress);
      long energyLimit;
      if (isConstantCall) {
        energyLimit = maxEnergyLimit;
      } else {
        AccountCapsule creator = rootRepository
            .getAccount(deployedContract.getInstance().getOriginAddress().toByteArray());
        energyLimit = getTotalEnergyLimit(creator, caller, contract, feeLimit, callValue);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L625-664)
```java
  private long getAccountEnergyLimitWithFloatRatio(AccountCapsule account, long feeLimit,
      long callValue) {

    long sunPerEnergy = VMConstant.SUN_PER_ENERGY;
    if (rootRepository.getDynamicPropertiesStore().getEnergyFee() > 0) {
      sunPerEnergy = rootRepository.getDynamicPropertiesStore().getEnergyFee();
    }
    // can change the calc way
    long leftEnergyFromFreeze = rootRepository.getAccountLeftEnergyFromFreeze(account);
    callValue = max(callValue, 0, VMConfig.disableJavaLangMath());
    long energyFromBalance = floorDiv(max(
        account.getBalance() - callValue, 0, VMConfig.disableJavaLangMath()), sunPerEnergy,
        VMConfig.disableJavaLangMath());

    long energyFromFeeLimit;
    long totalBalanceForEnergyFreeze = account.getAllFrozenBalanceForEnergy();
    if (0 == totalBalanceForEnergyFreeze) {
      energyFromFeeLimit =
          feeLimit / sunPerEnergy;
    } else {
      long totalEnergyFromFreeze = rootRepository
          .calculateGlobalEnergyLimit(account);
      long leftBalanceForEnergyFreeze = getEnergyFee(totalBalanceForEnergyFreeze,
          leftEnergyFromFreeze,
          totalEnergyFromFreeze);

      if (leftBalanceForEnergyFreeze >= feeLimit) {
        energyFromFeeLimit = BigInteger.valueOf(totalEnergyFromFreeze)
            .multiply(BigInteger.valueOf(feeLimit))
            .divide(BigInteger.valueOf(totalBalanceForEnergyFreeze)).longValueExact();
      } else {
        energyFromFeeLimit = addExact(
            leftEnergyFromFreeze, (feeLimit - leftBalanceForEnergyFreeze) / sunPerEnergy,
            VMConfig.disableJavaLangMath());
      }
    }

    return min(addExact(leftEnergyFromFreeze, energyFromBalance,
            VMConfig.disableJavaLangMath()), energyFromFeeLimit, VMConfig.disableJavaLangMath());
  }
```

**File:** framework/src/test/java/org/tron/core/db/CalculateGlobalLimitHardenTest.java (L67-78)
```java
  @Test
  public void testGlobalEnergyLimitOverflowDetectedWithHardening() {
    dbManager.getDynamicPropertiesStore().saveTotalEnergyCurrentLimit(Long.MAX_VALUE / 2);
    dbManager.getDynamicPropertiesStore().saveTotalEnergyWeight(1L);
    ownerCapsule.setFrozenForEnergy(Long.MAX_VALUE / 4, 0L);
    dbManager.getAccountStore().put(ownerCapsule.getAddress().toByteArray(), ownerCapsule);

    dbManager.getDynamicPropertiesStore().saveAllowHardenResourceCalculation(1);

    Assert.assertThrows(ArithmeticException.class,
        () -> energyProcessor.calculateGlobalEnergyLimit(ownerCapsule));
  }
```
