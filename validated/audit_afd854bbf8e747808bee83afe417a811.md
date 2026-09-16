### Title
Exchange balance arithmetic silently overflows/wraps when `allowHardenExchangeCalculation` is disabled, allowing corruption of on‑chain exchange and account balances - (File: actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java)

### Summary
The reported MySQL InnoDB CVE is a bug class where a network‑reachable, authenticated operation triggers unchecked internal arithmetic/logic that corrupts persisted data (integrity + availability impact, no confidentiality impact — matching `C:N/I:H/A:H`). The closest reachable analog in java-tron is the Exchange (Bancor-style DEX) actuator family, where balance arithmetic falls back to raw, unchecked `long` addition/subtraction whenever the `allowHardenExchangeCalculation` chain parameter is not enabled, instead of always using overflow-checked math.

### Finding Description
`AbstractExchangeActuator` defines `addExact`/`subtractExact` helpers that are supposed to guard against `long` overflow, but the overflow check is conditional on a runtime flag: [1](#0-0) 

When `allowHarden()` (i.e. `DynamicPropertiesStore.allowHardenExchangeCalculation()`) is not active, these methods perform plain `x + y` / `x - y`, silently wrapping on overflow instead of throwing `ArithmeticException`.

`ExchangeInjectActuator.execute()` and `ExchangeWithdrawActuator.execute()` call these inherited, non-static `addExact`/`subtractExact` methods (which resolve to the instance methods above, not `java.lang.Math`'s exact methods) when updating both the `ExchangeCapsule` pool balances and the `AccountCapsule` TRX/asset balances: [2](#0-1) [3](#0-2) 

`ExchangeTransactionActuator` uses the same pattern for the account-side balance updates and also uses `addExact` in its balance-limit check inside `doValidate()`: [4](#0-3) [5](#0-4) 

Separately, `ExchangeCapsule.transaction()` — the core AMM math invoked by `ExchangeTransactionActuator` for every trade — takes an explicit `hardenedCalc` flag; when it is `false` (the pre-hardening/legacy code path retained for compatibility), the new pool balances are computed with plain `+`/`-` and committed to the persistent `ExchangeCapsule` unconditionally, without any overflow guard: [6](#0-5) 

Because both the "harden" gate in `AbstractExchangeActuator` and the `hardenedCalc` gate in `ExchangeCapsule` are runtime, committee-controlled chain parameters (not always-on), any deployment or migration window in which `allowHardenExchangeCalculation` is `0` runs the original unguarded arithmetic. Under that condition, a single account that (a) issues its own asset with a very large `totalSupply` (only constrained by `TotalSupply > 0`, see `AssetIssueActuator`/`AssetIssueContract`) and (b) creates/injects into an `Exchange` pool can drive `firstTokenBalance`/`secondTokenBalance`/account asset or TRX balances toward `Long.MAX_VALUE`, causing the plain `+`/`-` operations to wrap into a negative or otherwise incorrect value. This corrupts the persisted `ExchangeCapsule` (`ExchangeStore`/`ExchangeV2Store`) and `AccountCapsule` records written by `Manager`'s transaction-application path, and can also let a crafted `tokenQuant` bypass the `tokenBalance > balanceLimit` guard in `ExchangeTransactionActuator.doValidate()` (line 202-205) because that guard itself uses the same overflow-prone `addExact`.

### Impact Explanation
A successful overflow corrupts the authoritative on-chain state for the DEX pool and/or account balances: pool balances or account TRX/asset balances can wrap to negative or to unintended large values, which is functionally equivalent to unauthorized creation of unbacked balance (integrity impact) and can permanently freeze/break the affected exchange pool (availability impact for that market), matching the "unbacked balance" / "permanent freezing of funds" acceptance criteria. Because the corrupted data is written directly to the block-application-committed stores, it also causes accounting divergence across nodes that could contribute to consensus/state inconsistency if only some nodes have the flag toggled during a migration.

### Likelihood Explanation
Reachability requires only ordinary, unprivileged capabilities already available to any account: broadcasting `AssetIssueContract`, `ExchangeCreateContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract`/`ExchangeTransactionContract` transactions. No SR/witness/committee privilege is needed. The prerequisite is that `allowHardenExchangeCalculation` is disabled on the target network/chain (its default state before committee activation) — I could not fully confirm the compiled default value of this proposal flag with the available index (the getter/setter live in `chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java`, but I was unable to locate the exact default-initialization line), so likelihood should be treated as flag-dependent rather than universally exploitable on mainnet today.

### Recommendation
Remove the runtime toggle for exchange arithmetic overflow checking and make `addExact`/`subtractExact` in `AbstractExchangeActuator`, and the `hardenedCalc=true` path in `ExchangeCapsule.transaction()`, unconditional (always throw on overflow) regardless of `allowHardenExchangeCalculation`/`hardenedCalc`. If backward compatibility requires the flag to remain for historical replay, add an independent, always-on sanity check (e.g. `newBalance >= 0` and `newBalance <= exchangeBalanceLimit`) after every balance mutation in `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeTransactionActuator`, and `ExchangeCapsule.transaction()` before persisting.

### Proof of Concept
1. Attacker issues an asset via `AssetIssueContract` with `totalSupply` close to `Long.MAX_VALUE` (only validated as `> 0`), see `AssetIssueActuator.validate()`.
2. Attacker creates an `Exchange` pool pairing this asset with TRX or another asset via `ExchangeCreateContract`.
3. On a network where `allowHardenExchangeCalculation` is `0`, attacker sends `ExchangeInjectContract`/`ExchangeTransactionContract` transactions with a `quant` chosen so that `firstTokenBalance/secondTokenBalance + quant` (in `ExchangeTransactionActuator.doValidate()` line 202, or `ExchangeCapsule.transaction()` lines 140-156) exceeds `Long.MAX_VALUE`, wrapping to a negative/small number.
4. The wrapped value passes the `tokenBalance > balanceLimit` guard and/or is persisted directly into `ExchangeCapsule`/`AccountCapsule` via `Commons.putExchangeCapsule` and `accountStore.put`, corrupting the pool's/account's recorded balance permanently (test evidence of the intended overflow-checked behavior — and thus the unguarded default behavior it replaces — is visible in `framework/src/test/java/org/tron/core/actuator/ExchangeInjectActuatorTest.java` lines 1862-1899 and `ExchangeTransactionActuatorTest.java` lines 1872-1907, which only assert correct overflow handling when `saveAllowHardenExchangeCalculation(1)` is explicitly set). [7](#0-6) [8](#0-7)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-23)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }

  public long subtractExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.subtractExact(x, y) : x - y;
  }

  public long addExact(long x, long y) {
    return allowHarden() ? StrictMathWrapper.addExact(x, y) : x + y;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-99)
```java
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            secondTokenBalance, tokenQuant), firstTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, tokenQuant),
            addExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            firstTokenBalance, tokenQuant), secondTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, anotherTokenQuant),
            addExact(secondTokenBalance, tokenQuant));
      }

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .reduceAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L77-104)
```java
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, tokenQuant),
            subtractExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, anotherTokenQuant),
            subtractExact(secondTokenBalance, tokenQuant));
      }

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, tokenQuant));
      } else {
        accountCapsule.addAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L76-91)
```java

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-205)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
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

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeInjectActuatorTest.java (L1862-1899)
```java
  @Test
  public void hardenedAddExactOverflowThrows() throws Exception {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(1);
    dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(1);
    InitExchangeSameTokenNameActive();

    // Corrupt pool balance to near-MAX so addExact overflows on inject.
    long exchangeId = 1;
    ExchangeCapsule pool = dbManager.getExchangeV2Store().get(ByteArray.fromLong(exchangeId));
    pool.setBalance(Long.MAX_VALUE - 10L, 200000000L);
    dbManager.getExchangeV2Store().put(pool.createDbKey(), pool);

    String firstTokenId = "123";
    AssetIssueCapsule a1 = new AssetIssueCapsule(
        AssetIssueContract.newBuilder()
            .setName(ByteString.copyFrom(firstTokenId.getBytes())).build());
    a1.setId(String.valueOf(1L));
    dbManager.getAssetIssueStore().put(a1.getName().toByteArray(), a1);

    byte[] ownerAddress = ByteArray.fromHexString(OWNER_ADDRESS_FIRST);
    AccountCapsule accountCapsule = dbManager.getAccountStore().get(ownerAddress);
    accountCapsule.addAssetAmountV2(firstTokenId.getBytes(), 1000000000L,
        dbManager.getDynamicPropertiesStore(), dbManager.getAssetIssueStore());
    accountCapsule.setBalance(10000_000000L);
    dbManager.getAccountStore().put(ownerAddress, accountCapsule);

    ExchangeInjectActuator actuator = new ExchangeInjectActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_FIRST, exchangeId, firstTokenId, 1000000000L));
    try {
      Assert.assertThrows(ContractExeException.class,
          () -> actuator.execute(new TransactionResultCapsule()));
    } finally {
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(2L));
      dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
    }
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeTransactionActuatorTest.java (L1877-1906)
```java
  @Test
  public void hardenedExecuteOverflowThrowsArithmeticException() throws Exception {
    dbManager.getDynamicPropertiesStore().saveAllowSameTokenName(1);
    dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(1);
    InitExchangeSameTokenNameActive();

    long exchangeId = 1;
    // Corrupt pool to near-MAX TRX so addExact overflows when buying.
    ExchangeCapsule pool = dbManager.getExchangeV2Store().get(ByteArray.fromLong(exchangeId));
    pool.setBalance(Long.MAX_VALUE - 5L, 10_000_000L);
    dbManager.getExchangeV2Store().put(pool.createDbKey(), pool);

    String tokenId = "_";
    long quant = 100L;
    ExchangeTransactionActuator actuator = new ExchangeTransactionActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
        OWNER_ADDRESS_SECOND, exchangeId, tokenId, quant, 1));

    try {
      // addExact throws ArithmeticException, which is wrapped into ContractExeException.
      Assert.assertThrows(ContractExeException.class,
          () -> actuator.execute(new TransactionResultCapsule()));
    } finally {
      dbManager.getExchangeStore().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeStore().delete(ByteArray.fromLong(2L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(1L));
      dbManager.getExchangeV2Store().delete(ByteArray.fromLong(2L));
      dbManager.getDynamicPropertiesStore().saveAllowHardenExchangeCalculation(0);
    }
  }
```
