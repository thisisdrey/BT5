## Analog Found

### Title
Unchecked floating-point precision loss in the legacy Bancor `Exchange` calculation path can be exploited with a self-issued, near-worthless TRC10 token to drain real reserves — analog of the Drift CVT fake-collateral exploit ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java])

### Summary
Drift's attackers minted a worthless, wash-traded token (CVT), paired it with an oracle they controlled, and used the resulting "credible" but fabricated price to extract far more real value from the protocol's vaults than the token was ever worth. java-tron's on-chain `Exchange` (TRON's built-in Bancor-style AMM, reachable by any unprivileged account) has an analogous structural weakness: any account can freely issue a TRC10 token via `AssetIssueContract`, pair it against a real asset (TRX or another TRC10) via `ExchangeCreateContract`, and then execute `ExchangeTransactionContract` trades that are settled by `ExchangeCapsule.transaction()` [1](#0-0) . That method only enforces a post-trade non-negative balance invariant when the "hardened" calculation path is used (`hardenedCalc == true`), a flag that is itself gated by the `allowHardenExchangeCalculation()` dynamic property [2](#0-1) . When `hardenedCalc` is `false`, the legacy `ExchangeProcessor` performs the Bancor formula purely in `double` arithmetic with no invariant check afterward [3](#0-2) , so accumulated floating-point rounding error across many small trades against a self-created, effectively-worthless token pool can drift the real-asset reserve below what the invariant should allow, letting the attacker extract more of the paired real asset than was ever deposited — an unbacked-balance / theft-of-funds outcome directly analogous to CVT's fabricated collateral value in the Drift hack.

### Finding Description
The `Exchange` feature lets any unprivileged account:
1. Issue a new TRC10 token cheaply (their own "CVT" equivalent).
2. Create an `Exchange` pool pairing that token against a real asset via `ExchangeCreateActuator`, seeding it with minimal real capital, exactly mirroring how the Drift attacker seeded a $500 Raydium pool [4](#0-3) .
3. Repeatedly call `ExchangeTransactionActuator`, which delegates pricing to `ExchangeCapsule.transaction()` [5](#0-4) .

Inside `transaction()`, the sold/bought quantities are computed by `Processor.exchange()`, and only the `hardenedCalc` (`SafeExchangeProcessor`, `BigDecimal`-based) path validates `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` before committing the new balances [6](#0-5) . The legacy `ExchangeProcessor` path relies purely on `double`/`Math.pow` computations with no such floor check [7](#0-6) . Whenever the network/store state has `allowHardenExchangeCalculation()` returning `false` (its default/legacy state prior to activation), an attacker who controls both sides of a self-issued/self-liquidity pool can perform a sequence of carefully sized trades that exploit `double` rounding to push the real-asset side of the pool's accounting inconsistent with actual conservation, extracting more of the paired real token than the invariant permits — the same "negligible real value in, disproportionate real value out" pattern the Drift report describes for CVT collateral.

### Impact Explanation
A successful sequence lets an unprivileged token issuer/order placer withdraw real TRX or TRC10 reserves from an `Exchange` pool beyond what was legitimately deposited, i.e., an unbacked-balance/theft-of-funds condition reachable purely through `AssetIssueContract` + `ExchangeCreateContract`/`ExchangeInjectContract` + `ExchangeTransactionContract`, all ordinary user-signable transactions with no special privilege required.

### Likelihood Explanation
Exploitability depends on `allowHardenExchangeCalculation()` being disabled for the target chain state; where it is enabled the floor check blocks the negative-balance drift, but the underlying `double`-based formula and rounding behavior otherwise remain reachable and self-triggerable by any account willing to issue a token and create/trade an exchange pool, requiring no counterparty cooperation beyond the attacker's own accounts.

### Recommendation
Enforce the same non-negative reserve invariant (and ideally the `BigDecimal`/`StrictMathWrapper` hardened math) unconditionally in `ExchangeCapsule.transaction()` and `ExchangeProcessor`, regardless of the `allowHardenExchangeCalculation` flag, so no code path can commit an `Exchange` balance update that violates conservation of the underlying reserves.

### Proof of Concept
1. Attacker calls `AssetIssueActuator` to mint a new TRC10 token (`FAKE`) with negligible cost.
2. Attacker calls `ExchangeCreateActuator` to pair `FAKE` with TRX, seeding a small amount of real TRX [8](#0-7) .
3. On a chain/state where `allowHardenExchangeCalculation()` is `false`, attacker repeatedly submits `ExchangeTransactionContract` calls with quantities chosen to maximize floating-point rounding drift in `ExchangeProcessor.exchange()` [9](#0-8) .
4. Because no floor check exists in this path (unlike the hardened path's explicit check at [10](#0-9) ), accumulated rounding lets the attacker withdraw more real TRX than the pool's true reserves justify.

**Uncertainty note:** I was unable to directly confirm within the available index the exact default/activation state of `allowHardenExchangeCalculation()` on current mainnet (whether it is enabled by default on new networks or requires an SR proposal), nor did I find a precise numeric bound proving how much rounding drift is achievable per trade or over N trades. Confirming exploitability at scale (i.e., whether accumulated `double` error is large enough to matter given TRON's typical token precision/quantities) would require running the actual `ExchangeProcessor` arithmetic against realistic values or a Devin session with test execution access — this could not be validated purely via static code reading.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L49-76)
```java
    try {
      final ExchangeCreateContract exchangeCreateContract = this.any
          .unpack(ExchangeCreateContract.class);
      AccountCapsule accountCapsule = accountStore
          .get(exchangeCreateContract.getOwnerAddress().toByteArray());

      byte[] firstTokenID = exchangeCreateContract.getFirstTokenId().toByteArray();
      byte[] secondTokenID = exchangeCreateContract.getSecondTokenId().toByteArray();
      long firstTokenBalance = exchangeCreateContract.getFirstTokenBalance();
      long secondTokenBalance = exchangeCreateContract.getSecondTokenBalance();

      long newBalance = subtractExact(accountCapsule.getBalance(), fee);

      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, firstTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(firstTokenID, firstTokenBalance, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, secondTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(secondTokenID, secondTokenBalance, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L57-91)
```java
      ExchangeCapsule exchangeCapsule = Commons
          .getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeTransactionContract.getExchangeId()));

      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }

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
