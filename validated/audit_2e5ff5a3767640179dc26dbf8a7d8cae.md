### Title
ExchangeTransactionContract deprecation bypass via `AllowHardenExchangeCalculation` flag re-enables legacy floating-point Exchange (Bancor-style AMM) transfers - (File: framework/src/main/java/org/tron/core/db/Manager.java)

### Summary
`Manager.pushTransaction()` rejects `ExchangeTransactionContract` broadcasts via `isExchangeTransaction(trx.getInstance())` — a deprecation gate that normally makes the legacy exchange/AMM feature unreachable from unprivileged transaction broadcasters. Test evidence shows that when the `ALLOW_HARDEN_EXCHANGE_CALCULATION` dynamic property is set to `1`, `isExchangeTransaction()` returns `false` for a genuine `ExchangeTransactionContract`, which causes the rejection branch to be skipped and the deprecated Exchange actuator path to execute normally.

### Finding Description [1](#0-0) 
shows the entry point used by every unprivileged, signed transaction broadcast through `pushTransaction`:
```
if (isExchangeTransaction(trx.getInstance())) {
  throw new ContractValidateException("ExchangeTransactionContract is rejected");
}
```
This mirrors an intentional feature-freeze of the old Bancor-formula `Exchange`/`ExchangeTransaction` system (superseded by the Market order book actuators). The unit test [2](#0-1)  directly demonstrates that this gate is conditioned on `allowHardenExchangeCalculation()`:
- with the flag at `0` (default), `isExchangeTransaction` returns `true` and the contract is rejected as intended;
- with the flag at `1`, the same call on an identical `ExchangeTransactionContract` returns `false`, meaning the rejection in `pushTransaction` is bypassed and the transaction is allowed to proceed to `processTransaction` → `ExchangeTransactionActuator`.

The `ExchangeTransactionActuator`/`ExchangeCapsule.transaction()` path [3](#0-2)  then routes through `SafeExchangeProcessor` when hardened math is enabled, using `BigDecimal`/`pow()` based bonding-curve pricing [4](#0-3) . Whatever the original rationale for freezing this feature (e.g. known precision/rounding exploitation of the Bancor `pow()` formula for asset pool balances, since this AMM math directly manipulates real TRX/token balances between arbitrary users), toggling `ALLOW_HARDEN_EXCHANGE_CALCULATION` — a chain parameter intended only to control whether strict/overflow-safe math is used in exchange computations — has the side effect of silently re-enabling the entire deprecated contract type for any unprivileged transaction broadcaster. This is a control-flow inversion bug: a numeric-safety toggle also functions as a feature-enablement toggle for a contract type meant to be permanently rejected.

### Impact Explanation
If `ExchangeTransactionContract` was frozen because trading against the legacy `Exchange` pools is unsafe (rounding/precision manipulation of AMM balances, or because the feature was deprecated in favor of Market orders for correctness reasons), then flipping on hardened math inadvertently un-freezes this attack surface for every already-existing `Exchange`/`ExchangeV2` pool still stored on chain, letting any signer trade against those pools and shift real TRX/token balances. This is a "concrete unauthorized account operation" class impact: unauthorized reactivation of a code path meant to be permanently rejected, directly affecting account balances through the exchange/actuator execute path (`accountCapsule.setBalance`, `addAssetAmountV2`/`reduceAssetAmountV2` in `ExchangeTransactionActuator.execute`, lines 77-93 shown above).

### Likelihood Explanation
The precondition (`ALLOW_HARDEN_EXCHANGE_CALCULATION == 1`) is governed by chain governance/proposal (`ProposalType.ALLOW_HARDEN_EXCHANGE_CALCULATION`, gated behind hard fork `VERSION_4_8_2`) [5](#0-4) , so it is not attacker-controlled directly; it requires the network to activate this parameter (which the project appears to intend to do, since `SafeExchangeProcessor` was built specifically to make exchange math safe again). Once active, any signer can reach `ExchangeTransactionActuator` with a normal signed transaction with no special privilege — full reachability from an unprivileged transaction broadcaster once the flag is on.

### Recommendation
Decouple the deprecation/rejection check in `pushTransaction` from `allowHardenExchangeCalculation()`. `isExchangeTransaction` (or equivalently the reject-gate) should be driven by an explicit, independent flag (e.g., `ALLOW_EXCHANGE_TRANSACTION` / the existing `ForkBlockVersionEnum` freeze that originally deprecated Exchange in v4.0.1), not by the hardened-math toggle. If the intent is that hardened math makes it safe to re-enable Exchange trading, that decision should be a deliberate, explicitly named proposal/parameter, and should be reviewed for whether re-opening trading against legacy `Exchange`/`ExchangeV2` pools (created before the freeze) is actually safe, rather than an implicit side effect of `AllowHardenExchangeCalculation`.

### Proof of Concept
Based on `ManagerTest.isExchangeTransactionBypassedWhenHardenedEnabled` [2](#0-1) :
1. Build an `ExchangeTransactionContract` transaction referencing an existing `Exchange`/`ExchangeV2` pool.
2. With `AllowHardenExchangeCalculation == 0`, call `Manager.pushTransaction(trx)` → throws `ContractValidateException("ExchangeTransactionContract is rejected")` (expected, deprecated).
3. Set `dynamicPropertiesStore.saveAllowHardenExchangeCalculation(1)`.
4. Call `Manager.pushTransaction(trx)` again with the same contract type → `isExchangeTransaction` now returns `false`, the rejection is skipped, and the transaction is processed normally through `ExchangeTransactionActuator`, mutating account/exchange pool balances as shown in `ExchangeTransactionActuatorTest.hardenedSuccessExchangeTransaction` [6](#0-5) .

Note: I was unable to locate/read the exact body of the private `isExchangeTransaction(Transaction)` method itself (only its call sites and test-driven behavior), due to it not being returned by search; a full review of that method's implementation is recommended to confirm the precise condition causing the bypass.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L892-899)
```java
    if (isShieldedTransaction(trx.getInstance()) && !chainBaseManager.getDynamicPropertiesStore()
        .supportShieldedTransaction()) {
      throw new ContractValidateException("ShieldedTransferContract is not supported.");
    }

    if (isExchangeTransaction(trx.getInstance())) {
      throw new ContractValidateException("ExchangeTransactionContract is rejected");
    }
```

**File:** framework/src/test/java/org/tron/core/db/ManagerTest.java (L1341-1367)
```java
  @Test
  public void isExchangeTransactionBypassedWhenHardenedEnabled() throws Exception {
    Transaction exchange = Transaction.newBuilder().setRawData(
        Transaction.raw.newBuilder().addContract(
            Transaction.Contract.newBuilder()
                .setType(ContractType.ExchangeTransactionContract)
                .setParameter(Any.pack(ExchangeTransactionContract.newBuilder()
                    .setExchangeId(1L).setQuant(1L).setExpected(1L).build()))
                .build())).build();

    java.lang.reflect.Method m = Manager.class.getDeclaredMethod(
        "isExchangeTransaction", Transaction.class);
    m.setAccessible(true);

    // Default: hardened disabled (==0) -> contract is treated as exchange
    chainManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
    Assert.assertTrue("Exchange tx must be detected when hardened disabled",
        (boolean) m.invoke(dbManager, exchange));

    // Hardened enabled -> bypass returns false
    chainManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(1);
    Assert.assertFalse("Exchange tx must be bypassed when hardened enabled",
        (boolean) m.invoke(dbManager, exchange));

    // Reset
    chainManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-168)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

    long buyTokenQuant = 0;
    long firstTokenBalance = this.exchange.getFirstTokenBalance();
    long secondTokenBalance = this.exchange.getSecondTokenBalance();
    long newFirstTokenBalance;
    long newSecondTokenBalance;

    if (this.exchange.getFirstTokenId().equals(ByteString.copyFrom(sellTokenID))) {
      buyTokenQuant = processor.exchange(firstTokenBalance,
          secondTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(firstTokenBalance, sellTokenQuant)
          : firstTokenBalance + sellTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(secondTokenBalance, buyTokenQuant)
          : secondTokenBalance - buyTokenQuant;

    } else {
      buyTokenQuant = processor.exchange(secondTokenBalance,
          firstTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(firstTokenBalance, buyTokenQuant)
          : firstTokenBalance - buyTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(secondTokenBalance, sellTokenQuant)
          : secondTokenBalance + sellTokenQuant;

    }

    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

    return buyTokenQuant;
```

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L19-44)
```java
  private BigDecimal exchangeToSupply(long balance, long quant) {
    long newBalance = StrictMathWrapper.addExact(balance, quant);
    BigDecimal bdQuant = BigDecimal.valueOf(quant);
    BigDecimal bdNewBalance = BigDecimal.valueOf(newBalance);
    BigDecimal base = BigDecimal.ONE.add(
        bdQuant.divide(bdNewBalance, 18, RoundingMode.HALF_UP));
    double powResult = StrictMathWrapper.pow(base.doubleValue(), 0.0005);
    return SUPPLY.negate().multiply(
        BigDecimal.ONE.subtract(BigDecimal.valueOf(powResult))).setScale(0, RoundingMode.DOWN);
  }

  private long exchangeFromSupply(long balance, BigDecimal supplyQuant) {
    BigDecimal bdBalance = BigDecimal.valueOf(balance);
    BigDecimal base = BigDecimal.ONE.add(
        supplyQuant.divide(SUPPLY, 18, RoundingMode.HALF_UP));
    double powResult = StrictMathWrapper.pow(base.doubleValue(), 2000.0);
    BigDecimal exchangeBalance = bdBalance.multiply(
        BigDecimal.valueOf(powResult).subtract(BigDecimal.ONE));
    return exchangeBalance.setScale(0, RoundingMode.DOWN).longValueExact();
  }

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    BigDecimal relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/utils/ProposalUtilTest.java (L685-737)
```java
  private void testAllowHardenExchangeCalculationProposal() {
    long code = ProposalType.ALLOW_HARDEN_EXCHANGE_CALCULATION.getCode();
    ThrowingRunnable proposeZero = () -> ProposalUtil.validator(dynamicPropertiesStore, forkUtils,
        code, 0);
    ThrowingRunnable proposeOne = () -> ProposalUtil.validator(dynamicPropertiesStore, forkUtils,
        code, 1);
    ThrowingRunnable proposeTwo = () -> ProposalUtil.validator(dynamicPropertiesStore, forkUtils,
        code, 2);

    byte[] stats = new byte[27];
    forkUtils.getManager().getDynamicPropertiesStore()
        .statsByVersion(ForkBlockVersionEnum.VERSION_4_8_1.getValue(), stats);
    long maintenanceTimeInterval = forkUtils.getManager().getDynamicPropertiesStore()
        .getMaintenanceTimeInterval();
    long hardForkTime =
        ((ForkBlockVersionEnum.VERSION_4_8_2.getHardForkTime() - 1) / maintenanceTimeInterval + 1)
            * maintenanceTimeInterval;
    forkUtils.getManager().getDynamicPropertiesStore()
        .saveLatestBlockHeaderTimestamp(hardForkTime - 1);

    // 1) before fork 4.8.2 -> rejected
    ContractValidateException thrown = assertThrows(ContractValidateException.class, proposeOne);
    assertEquals("Bad chain parameter id [ALLOW_HARDEN_EXCHANGE_CALCULATION]",
        thrown.getMessage());

    forkUtils.getManager().getDynamicPropertiesStore()
        .saveLatestBlockHeaderTimestamp(hardForkTime + 1);
    Arrays.fill(stats, (byte) 1);
    forkUtils.getManager().getDynamicPropertiesStore()
        .statsByVersion(ForkBlockVersionEnum.VERSION_4_8_2.getValue(), stats);

    // 2) value not in {0, 1} -> rejected
    thrown = assertThrows(ContractValidateException.class, proposeTwo);
    assertEquals("This value[ALLOW_HARDEN_EXCHANGE_CALCULATION] is only allowed to be 0 or 1",
        thrown.getMessage());

    // 3) current value is 0 (default), proposing 0 again -> rejected
    thrown = assertThrows(ContractValidateException.class, proposeZero);
    assertEquals("[ALLOW_HARDEN_EXCHANGE_CALCULATION] has been set to 0, no need to propose again",
        thrown.getMessage());

    // 4) value=1 to enable -> ok
    try {
      proposeOne.run();
    } catch (Throwable e) {
      Assert.fail("Should pass when toggling 0 -> 1: " + e.getMessage());
    }

    // 5) after activation, proposing 1 again -> rejected
    dynamicPropertiesStore.saveAllowHardenExchangeCalculation(1);
    thrown = assertThrows(ContractValidateException.class, proposeOne);
    assertEquals("[ALLOW_HARDEN_EXCHANGE_CALCULATION] has been set to 1, no need to propose again",
        thrown.getMessage());
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeTransactionActuatorTest.java (L1835-1863)
```java
  @Test
  public void hardenedSuccessExchangeTransaction() {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(1);
    dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(1);
    InitExchangeSameTokenNameActive();
    long exchangeId = 1;
    String tokenId = "_";
    long quant = 100_000_000L;

    byte[] ownerAddress = ByteArray.fromHexString(OWNER_ADDRESS_SECOND);
    AccountCapsule before = dbManager.getAccountStore().get(ownerAddress);
    long initialBalance = before.getBalance();

    ExchangeTransactionActuator actuator = new ExchangeTransactionActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_SECOND, exchangeId, tokenId, quant, 1));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);
      Assert.assertEquals(code.SUCESS, ret.getInstance().getRet());
      AccountCapsule after = dbManager.getAccountStore().get(ownerAddress);
      Assert.assertEquals(initialBalance - quant, after.getBalance());
      Assert.assertTrue("Hardened tx must produce positive received amount",
          ret.getExchangeReceivedAmount() > 0);
    } catch (Exception e) {
      Assert.fail("Hardened transaction must succeed: " + e.getMessage());
    } finally {
```
