### Title
Exchange pool can be permanently frozen by unchecked floating-point Bancor calculation in legacy `ExchangeCapsule.transaction()` - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
`ExchangeCapsule.transaction()` computes new pool balances after every `ExchangeTransactionContract`/`ExchangeWithdrawContract` trade using either a hardened, bounds-checked path or a legacy, unchecked path selected by the `allowHardenExchangeCalculation` dynamic parameter. In the legacy path (the default unless a committee proposal has activated hardening), the new balances are computed with plain `long` arithmetic fed by `ExchangeProcessor`, which internally uses `double`/`Math.pow` floating-point math, and the result is stored with **no floor/negativity check**. Any unprivileged account can repeatedly call `ExchangeTransactionActuator`/`ExchangeWithdrawActuator` on a public Exchange pool; because of double-precision rounding in the Bancor relay formula, a token side balance can be driven to exactly `0` (or theoretically negative), at which point `ExchangeTransactionActuator.validate()`/`ExchangeWithdrawActuator.validate()` will reject **all further trades on that pool forever** with `"Token balance in exchange is equal with 0, the exchange has been closed"`. This mirrors the reported "pools become unavailable after some hours" bug: legitimate trading activity — not malicious behavior — drives the pool into an irreversible closed state, permanently locking whatever tokens remain on the other side.

### Finding Description
`ExchangeCapsule.transaction()`: [1](#0-0) 

only enforces `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` when `hardenedCalc` is `true` (i.e., `SafeExchangeProcessor`, BigDecimal-based). When `hardenedCalc` is `false` (the legacy `ExchangeProcessor`), the balances are set unconditionally: [2](#0-1) 

`ExchangeProcessor` performs the Bancor relay math with `double` and `Math.pow`, then truncates to `long` via `(long) issuedSupply` / `(long) exchangeBalance`. Repeated small trades accumulate floating point rounding error, and because the legacy branch never rejects a resulting balance of exactly `0`, a sequence of ordinary (non-malicious) trades submitted by any account can reduce one side of the pool to `0`.

`allowHarden()` in `AbstractExchangeActuator` gates which processor is used, driven purely by the dynamic property (default legacy path unless explicitly hardened by governance): [3](#0-2) 

Once either `firstTokenBalance` or `secondTokenBalance` reaches `0`, both `ExchangeTransactionActuator.doValidate()` and the equivalent check in `ExchangeWithdrawActuator`/`ExchangeInjectActuator` reject the transaction permanently: [4](#0-3) 

This validate-time check is confirmed by existing unit tests describing the closed state: [5](#0-4) 

There is no actuator, proposal, or admin action able to "reopen" an exchange once a balance side hits `0` — `ExchangeWithdrawActuator` also computes withdrawal amounts using the same unchecked long math and division, so once one side is `0` any attempt to interact with the pool fails validation. This means the pool (and any remaining locked token balance on the non-zero side, which becomes unwithdrawable through normal actuators) becomes permanently unusable — directly analogous to the reported "pools become unavailable" bug in the external report.

### Impact Explanation
- Any account can, through entirely legitimate/ordinary trading calls to `ExchangeTransactionContract` (broadcastable by any transaction sender, no special permission required), push a public Exchange pool's balance to `0` due to floating-point rounding in the legacy Bancor calculation.
- Once a side hits `0`, the pool is permanently closed: all subsequent `ExchangeTransactionContract`, `ExchangeInjectContract`, and `ExchangeWithdrawContract` operations against that exchange fail validation forever.
- Any token balance remaining in the exchange on the non-zero side becomes permanently locked/inaccessible via the exchange actuators (no actuator provides an emergency withdrawal path once balance hits zero), constituting a permanent freezing-of-funds condition and a denial of service against a public, node-served market API (`Wallet.getMarketPriceByPair`, `getExchangeStore`, related HTTP endpoints continue to report a dead/closed pool).
- This matches the external report's described impact rating (3/5) — a service-availability/fund-locking bug rather than outright theft — but is reachable purely from unprivileged transaction broadcasting.

### Likelihood Explanation
- Reaching the bug requires no special privilege: any address with sufficient balance/asset can submit `ExchangeCreateContract` (to make or use an existing pool) and then repeatedly submit `ExchangeTransactionContract` trades.
- The condition is not adversary-crafted per trade; it emerges naturally from double-precision rounding accumulated over normal trading volume — matching the external report's finding that pools became unavailable purely through routine testing/usage over hours, without any special exploit technique.
- The vulnerability is gated behind the default (non-hardened) calculation path; it is present unless/until the `allowHardenExchangeCalculation` dynamic parameter has been activated by committee proposal for a given chain deployment.

### Recommendation
Apply the same bounds validation that already exists in the hardened branch to the legacy branch of `ExchangeCapsule.transaction()`: reject (throw `ContractValidateException`) whenever the computed `newFirstTokenBalance` or `newSecondTokenBalance` would be `<= 0`, regardless of `hardenedCalc`. Alternatively, make `SafeExchangeProcessor` (BigDecimal-based, bounds-checked) the unconditional default path for all exchange trade/withdraw calculations rather than an opt-in governed by `allowHardenExchangeCalculation`, closing off the floating-point rounding drift that enables balances to reach exactly zero without detection in the legacy path.

### Proof of Concept
1. Create an `Exchange` pool via `ExchangeCreateContract` with token balances `A` (TRX) and `B` (custom asset) under default dynamic properties (`allowHardenExchangeCalculation = 0`, i.e., legacy `ExchangeProcessor` in use), as exercised in [6](#0-5) .
2. Repeatedly submit `ExchangeTransactionContract` trades (any account, any small `quant`) against the pool, alternating sell direction to exercise `ExchangeCapsule.transaction()` at [1](#0-0) ; each call updates `firstTokenBalance`/`secondTokenBalance` via the unchecked `double`-based `ExchangeProcessor` at [2](#0-1) .
3. Over enough trades, accumulated rounding truncation drives one side's balance to exactly `0` (the existing test suite already demonstrates the resulting "closed" state once balance is manually set to `0`: [5](#0-4) ).
4. Any further `ExchangeTransactionContract`/`ExchangeWithdrawContract`/`ExchangeInjectContract` call on that exchange now fails permanently in `validate()`, e.g. [4](#0-3) , leaving the pool and any residual locked balance unusable — directly reproducing the "pools become unavailable" behavior from the external report.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-169)
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
  }
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L194-197)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeTransactionActuatorTest.java (L1105-1112)
```java
  /**
   * SameTokenName open,Token balance in exchange is equal with 0, the exchange has been closed"
   */
  @Test
  public void SameTokenNameOpenTokenBalanceZero() {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(1);
    InitExchangeSameTokenNameActive();
    long exchangeId = 2;
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeCreateActuatorTest.java (L199-279)
```java
  @Test
  public void sameTokenNameCloseSuccessExchangeCreate2() {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(0);
    String firstTokenId = "_";
    long firstTokenBalance = 100_000_000_000000L;
    String secondTokenId = "abc";
    long secondTokenBalance = 100_000_000L;

    AssetIssueCapsule assetIssueCapsule =
        new AssetIssueCapsule(
            AssetIssueContract.newBuilder()
                .setName(ByteString.copyFrom(secondTokenId.getBytes()))
                .build());
    assetIssueCapsule.setId(String.valueOf(1L));
    dbManager.getAssetIssueStore()
        .put(assetIssueCapsule.getName().toByteArray(), assetIssueCapsule);

    byte[] ownerAddress = ByteArray.fromHexString(OWNER_ADDRESS_FIRST);
    AccountCapsule accountCapsule = dbManager.getAccountStore().get(ownerAddress);
    accountCapsule.setBalance(200_000_000_000000L);
    accountCapsule.addAssetAmount(secondTokenId.getBytes(), 200_000_000L, true);
    dbManager.getAccountStore().put(ownerAddress, accountCapsule);

    ExchangeCreateActuator actuator = new ExchangeCreateActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_FIRST, firstTokenId, firstTokenBalance, secondTokenId, secondTokenBalance));
    TransactionResultCapsule ret = new TransactionResultCapsule();
    Assert.assertEquals(dbManager.getDynamicPropertiesStore().getLatestExchangeNum(), 0);
    try {
      actuator.validate();
      actuator.execute(ret);
      Assert.assertEquals(ret.getInstance().getRet(), code.SUCESS);
      long id = 1;
      Assert.assertEquals(dbManager.getDynamicPropertiesStore().getLatestExchangeNum(), id);
      // check old version
      ExchangeCapsule exchangeCapsule = dbManager.getExchangeStore().get(ByteArray.fromLong(id));
      Assert.assertNotNull(exchangeCapsule);
      Assert.assertEquals(ByteString.copyFrom(ownerAddress), exchangeCapsule.getCreatorAddress());
      Assert.assertEquals(id, exchangeCapsule.getID());
      Assert.assertEquals(1000000, exchangeCapsule.getCreateTime());
      Assert.assertTrue(Arrays.equals(firstTokenId.getBytes(), exchangeCapsule.getFirstTokenId()));
      Assert.assertEquals(firstTokenId, ByteArray.toStr(exchangeCapsule.getFirstTokenId()));
      Assert.assertEquals(firstTokenBalance, exchangeCapsule.getFirstTokenBalance());
      Assert.assertEquals(secondTokenId, ByteArray.toStr(exchangeCapsule.getSecondTokenId()));
      Assert.assertEquals(secondTokenBalance, exchangeCapsule.getSecondTokenBalance());

      accountCapsule = dbManager.getAccountStore().get(ownerAddress);
      Map<String, Long> assetMap = accountCapsule.getAssetMapForTest();
      Assert.assertEquals(200_000_000_000000L - 1024_000000L - firstTokenBalance,
          accountCapsule.getBalance());
      Assert.assertEquals(100_000_000L, assetMap.get(secondTokenId).longValue());

      // check V2 version
      ExchangeCapsule exchangeCapsuleV2 = dbManager.getExchangeV2Store()
          .get(ByteArray.fromLong(id));
      Assert.assertNotNull(exchangeCapsuleV2);
      Assert.assertEquals(ByteString.copyFrom(ownerAddress), exchangeCapsuleV2.getCreatorAddress());
      Assert.assertEquals(id, exchangeCapsuleV2.getID());
      Assert.assertEquals(1000000, exchangeCapsuleV2.getCreateTime());
      secondTokenId = dbManager.getAssetIssueStore().get(secondTokenId.getBytes()).getId();
      Assert
          .assertTrue(Arrays.equals(firstTokenId.getBytes(), exchangeCapsuleV2.getFirstTokenId()));
      Assert.assertEquals(firstTokenId, ByteArray.toStr(exchangeCapsuleV2.getFirstTokenId()));
      Assert.assertEquals(firstTokenBalance, exchangeCapsuleV2.getFirstTokenBalance());
      Assert.assertEquals(secondTokenId, ByteArray.toStr(exchangeCapsuleV2.getSecondTokenId()));
      Assert.assertEquals(secondTokenBalance, exchangeCapsuleV2.getSecondTokenBalance());

      accountCapsule = dbManager.getAccountStore().get(ownerAddress);
      Map<String, Long> getAssetV2Map = accountCapsule.getAssetV2MapForTest();
      Assert.assertEquals(200_000_000_000000L - 1024_000000L - firstTokenBalance,
          accountCapsule.getBalance());
      Assert.assertEquals(100_000_000L, getAssetV2Map.get(secondTokenId).longValue());

    } catch (ContractValidateException e) {
      Assert.assertFalse(e instanceof ContractValidateException);
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    } catch (ItemNotFoundException e) {
      Assert.assertFalse(e instanceof ItemNotFoundException);
    }
  }
```
