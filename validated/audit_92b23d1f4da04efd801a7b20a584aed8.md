### Title
Exchange (Bancor-relay) trade calculation lacks reserve-underflow/overflow protection unless `AllowHardenExchangeCalculation` is enabled - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
`ExchangeTransactionActuator` lets any account trade against TRON's built-in bonding-curve "Exchange" (TRX/TRC-10 relay pool), analogous to a PancakeSwap-style pool in the rekt report. The trade amount is computed by `ExchangeCapsule.transaction()`, which by default uses `ExchangeProcessor` — floating-point `double` math on a Bancor-style curve — and only performs the "resulting reserve must be ≥ 0" safety check when `hardenedCalc` (`allowHarden()`) is true. `allowHarden()` is gated by the dynamic property `allowHardenExchangeCalculation`, a committee-controlled flag that is off by default (tests explicitly `saveAllowHardenExchangeCalculation(1)` to exercise the safe path and reset it to `0` afterward). In the default/non-hardened path, an attacker-controlled trade can drive one side of the exchange's reserve balance negative or otherwise corrupt it, exactly mirroring NGP's exploit where an unguarded reserve-adjustment mechanism (`sync()` after a broken fee deduction) collapsed pool reserves and broke the AMM invariant.

### Finding Description
`ExchangeTransactionActuator.execute()` computes the counter-party amount via: [1](#0-0) 

which calls `ExchangeCapsule.transaction()`: [2](#0-1) 

Note that the guard `if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) throw ...` at line 160 is only enforced when `hardenedCalc` is `true`. When it's `false` (the default), `newFirstTokenBalance`/`newSecondTokenBalance` are computed with plain `long` arithmetic (`firstTokenBalance + sellTokenQuant`, `secondTokenBalance - buyTokenQuant`) with no floor or overflow check, and `buyTokenQuant` itself comes from `ExchangeProcessor`, which relies on IEEE-754 `double` and `Math.pow` for the Bancor curve: [3](#0-2) 

This is precisely the two ingredients present in the NGP exploit: (1) a bonding-curve/price calculation that is not hardened against manipulation/precision loss, and (2) a reserve-adjustment step with no invariant check, allowing the pool balance to be pushed to a broken/negative state. `AbstractExchangeActuator.allowHarden()` shows the gate: [4](#0-3) 

The test suite confirms the hardened path is opt-in and not the default: it is explicitly turned on and back off around individual test cases rather than being always active. [5](#0-4) 

`ExchangeTransactionActuator.doValidate()` also does not independently verify that the computed `anotherTokenQuant` does not exceed the actual counter-reserve balance before execution — it only checks `anotherTokenQuant < tokenExpected` (slippage-style check) and the aggregate `balanceLimit`: [6](#0-5) 

### Impact Explanation
If the non-hardened floating-point calculation (default state) produces a `buyTokenQuant`/`anotherTokenQuant` that exceeds the actual counter-token reserve, `ExchangeCapsule.transaction()` will silently store a negative `firstTokenBalance` or `secondTokenBalance` in the `ExchangeCapsule`/`ExchangeV2Store` (int64 protobuf fields accept negative values), while the trading account is credited via `addAssetAmountV2`/`setBalance` with tokens that are not actually backed by the pool. This is an unbacked-balance condition: subsequent traders interacting with the same exchange pool operate on corrupted (negative or nonsensical) reserve numbers, and the attacker who triggered the miscalculation has extracted more value than they deposited — the same "reserves collapse from 477,000 to 0.035 units, x*y=k invariant obliterated" outcome described in the NGP report, but on TRON's own built-in AMM rather than a third-party TVM contract.

### Likelihood Explanation
The trade path is reachable by any account via a single `ExchangeTransactionContract` transaction (`Wallet`/API entry point → `ExchangeTransactionActuator`), requiring no special privilege — only that the target Exchange pair has thin reserves relative to the trade size, which is common for freshly created or low-liquidity TRC-10/TRX exchange pairs created via `ExchangeCreateContract`. Because `allowHardenExchangeCalculation` must be turned on by committee/SR proposal, any deployment where this proposal has not been activated is exposed by default, and the actuator provides no independent invariant check regardless of the flag's state in `doValidate()`.

### Recommendation
- Make the `hardenedCalc` (BigDecimal, overflow/underflow-checked) path in `ExchangeCapsule.transaction()` mandatory rather than conditional on the `allowHardenExchangeCalculation` proposal, removing the `hardenedCalc &&` short-circuit at line 160 so the reserve non-negativity check always applies.
- Replace `ExchangeProcessor`'s `double`/`Math.pow` arithmetic with the `BigDecimal`-based `SafeExchangeProcessor` unconditionally.
- Add an explicit reserve-sufficiency check in `ExchangeTransactionActuator.doValidate()` verifying `anotherTokenQuant <= counterTokenBalance` before allowing execution, independent of the `allowHarden` flag.

### Proof of Concept
1. Create (or locate) a TRC-10/TRX `Exchange` pair with a very small counter-token reserve (e.g., via `ExchangeCreateContract` with minimal `secondTokenBalance`), while `allowHardenExchangeCalculation` remains at its default (disabled) value.
2. Submit an `ExchangeTransactionContract` with a `sellTokenQuant` sized so that `ExchangeProcessor.exchange()` (the CRR-0.0005 Bancor curve using `double`/`Math.pow`) computes a `buyTokenQuant` that is greater than or very close to the actual `secondTokenBalance`.
3. Observe in `ExchangeCapsule.transaction()` (non-hardened branch) that `newSecondTokenBalance = secondTokenBalance - buyTokenQuant` is computed with plain `long` subtraction and stored without any non-negative check, while the trading account is simultaneously credited with `buyTokenQuant` tokens via `addAssetAmountV2`.
4. Confirm the persisted `ExchangeCapsule`/`ExchangeV2Store` entry now reflects a negative or reserve-inconsistent balance, and that the credited tokens exceed what the pool actually held — demonstrating the unbacked-balance / broken-invariant condition analogous to the NGP pool-drain.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L67-69)
```java
      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-221)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }

    if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(tokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(tokenID, tokenQuant, dynamicStore)) {
        throw new ContractValidateException("token balance is not enough");
      }
    }

    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-45)
```java
  private long exchangeToSupply(long balance, long quant) {
    logger.debug("balance: " + balance);
    long newBalance = balance + quant;
    logger.debug("balance + quant: " + newBalance);

    double issuedSupply = -supply * (1.0
        - Maths.pow(1.0 + (double) quant / newBalance, 0.0005, this.useStrictMath));
    logger.debug("issuedSupply: " + issuedSupply);
    long out = (long) issuedSupply;
    supply += out;

    return out;
  }

  private long exchangeFromSupply(long balance, long supplyQuant) {
    supply -= supplyQuant;

    double exchangeBalance = balance
        * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, this.useStrictMath) - 1.0);
    logger.debug("exchangeBalance: " + exchangeBalance);

    return (long) exchangeBalance;
  }

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeWithdrawActuatorTest.java (L1838-1864)
```java
  public void hardenedPrecisionCheckFailsWhenImprecise() {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(1);
    dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(1);
    InitExchangeSameTokenNameActive();
    long exchangeId = 1;
    // Pool 100M/200M; withdrawing 9991 of "456" produces non-integer ratio
    String secondTokenId = "456";
    long quant = 9991L;

    ExchangeWithdrawActuator actuator = new ExchangeWithdrawActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_FIRST, exchangeId, secondTokenId, quant));
    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);
      Assert.fail("Should fail with Not precise enough");
    } catch (ContractValidateException e) {
      Assert.assertEquals("Not precise enough", e.getMessage());
    } catch (Exception e) {
      Assert.fail("Unexpected exception: " + e.getMessage());
    } finally {
      dbManager.getExchangeStore().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeStore().delete(ByteArray.fromLong(2L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(2L));
      dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
```
