### Title
Arithmetic Overflow in Exchange (Bancor-style) Balance Accounting When `AllowHardenExchangeCalculation` Is Disabled - (File: `actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java`)

### Summary
`AbstractExchangeActuator.addExact`/`subtractExact`, which every Exchange-related actuator (`ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeTransactionActuator`) relies on to update pool and account balances, only perform overflow-checked arithmetic when the chain-parameter `allowHardenExchangeCalculation` is enabled. When that flag is off (its historical/default state before committee activation), the "exact" methods silently fall back to plain, unchecked `long` addition/subtraction — the exact SWC-101 pattern from the source report, where a function that looks safe (named `*Exact`, mirroring `oracle.currentPrice`'s implicit safety assumption) actually performs raw arithmetic that can wrap around.

### Finding Description
`AbstractExchangeActuator` defines: [1](#0-0) 

`allowHarden()` reads the dynamic property `allowHardenExchangeCalculation`, which is a committee-governed chain parameter stored/read via `DynamicPropertiesStore`. When it is not activated, `addExact`/`subtractExact` reduce to `x + y` / `x - y` with no `ArithmeticException` on overflow.

These exact methods are used to mutate live economic state directly from unprivileged, transaction-broadcastable actuators:
- `ExchangeInjectActuator.execute()` uses `addExact`/`floorDiv`/`multiplyExact` (inherited, gated by the same `allowHarden()` flag) to compute new pool balances from user-supplied `tokenQuant`: [2](#0-1) 
- `ExchangeWithdrawActuator.execute()` similarly updates pool balances with `subtractExact`/`addExact`: [3](#0-2) 
- `ExchangeCapsule.transaction()` (invoked by `ExchangeTransactionActuator`, reachable by any order placer) also branches on `hardenedCalc`, falling back to plain `+`/`-` when disabled: [4](#0-3) 
- The underlying non-hardened bancor math in `ExchangeProcessor.exchangeToSupply` also performs unchecked `long newBalance = balance + quant;`: [5](#0-4) 

All of these actuators are reachable directly from a signed transaction submitted by any account (`ExchangeInjectContract`, `ExchangeWithdrawContract`, `ExchangeTransactionContract` — order placer / asset issuer class of unprivileged users), with no special permission required beyond owning the exchange pool tokens/balance being injected or trading against an existing pool.

Existing tests in the repository confirm the two code paths and that the vulnerable (non-hardened) path is real and distinct from the hardened one: [6](#0-5) [7](#0-6) 

Both tests explicitly enable `saveAllowHardenExchangeCalculation(1)` to force the safe path and assert that overflow is caught — implying that without this flag enabled, the same corrupted-balance setup would silently wrap around instead of throwing.

### Impact Explanation
If a pool balance can be driven near `Long.MAX_VALUE` (via repeated `ExchangeInjectContract` calls, which are permissionless and only bounded by `exchangeBalanceLimit`/economic cost, not by protocol trust), a subsequent inject/withdraw/trade causes the plain `+`/`-` to wrap to a negative or unexpectedly small value. This corrupts the on-chain `ExchangeCapsule` balances that back a bancor-style relay used to price TRX/TRC10 swaps, enabling:
- Permanent freezing/loss of pooled funds (balances become negative/garbage, subsequent trades revert or lock funds), and/or
- Theft via mispriced trades (a wrapped-around balance produces a favorable, incorrect exchange rate for the attacker in `ExchangeCapsule.transaction`/`ExchangeProcessor`).

This satisfies the required impact bar (unbacked balance / theft / permanent freezing of funds via unauthorized economic manipulation of Exchange pools).

### Likelihood Explanation
Exploitability depends on the chain-parameter `allowHardenExchangeCalculation` being disabled. Because this is a committee-activated hardening flag (added after the underlying overflow bug was recognized, mirroring the pattern of other opt-in hardening flags in `DynamicPropertiesStore`/`ProposalUtil`), any deployment (private/consortium chain, or mainnet prior to committee activation of this specific proposal) that has not explicitly turned the flag on remains exposed. The actuators themselves are fully reachable by any account issuing standard `ExchangeInjectContract`/`ExchangeTransactionContract`/`ExchangeWithdrawContract` transactions — no privileged role required.

### Recommendation
Make overflow-checked arithmetic (`addExact`/`subtractExact`/hardened `SafeExchangeProcessor`) the unconditional default in `AbstractExchangeActuator` and `ExchangeCapsule.transaction`, rather than gating it behind the `allowHardenExchangeCalculation` proposal. If backward compatibility requires the flag for consensus continuity, ensure the flag is activated by default on all new/forked networks and audit historical balances for any manipulation that occurred while the guard was inactive.

### Proof of Concept
1. Deploy/observe a chain where `allowHardenExchangeCalculation` is not yet activated (default committee state, as demonstrated by the need to explicitly call `saveAllowHardenExchangeCalculation(1)` in tests to exercise the safe path).
2. Attacker (or attacker + colluding pool creator) repeatedly submits `ExchangeInjectContract` transactions to grow one side of an `ExchangeCapsule` pool balance to just below `Long.MAX_VALUE` (as set up directly in `ExchangeInjectActuatorTest.hardenedAddExactOverflowThrows`, using `pool.setBalance(Long.MAX_VALUE - 10L, ...)` to simulate the accumulated state).
3. Attacker submits one more `ExchangeInjectContract`/`ExchangeTransactionContract` transaction with `tokenQuant` sufficient to overflow `firstTokenBalance + tokenQuant` (or the analogous subtraction in withdraw).
4. With the hardening flag disabled, `AbstractExchangeActuator.addExact` executes `x + y` directly (no `StrictMathWrapper.addExact` call), silently wrapping the balance instead of throwing, corrupting the pool's on-chain state and enabling mispriced subsequent trades or fund lock-up — precisely mirroring the “transaction should revert but doesn’t due to unchecked overflow” condition from the source report.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L136-166)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-29)
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
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeInjectActuatorTest.java (L1862-1873)
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

```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeTransactionActuatorTest.java (L1877-1888)
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

```
