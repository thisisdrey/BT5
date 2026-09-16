### Title
Exchange AMM pool balances can silently go negative via unchecked legacy floating-point math - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java)

### Summary
`ExchangeTransactionActuator` lets any account execute trades against a TRC10 Bancor-style liquidity pool (`ExchangeCapsule`). The pool math is delegated to `ExchangeCapsule.transaction()`, which only validates that the resulting pool balances are non-negative when the chain-wide "hardened" calculation mode is enabled. In the default/legacy path the same invariant is never checked, and the underlying arithmetic uses unchecked `double`/floating-point `pow` operations instead of safe integer math.

### Finding Description
`ExchangeCapsule.transaction()` computes new pool balances using either `SafeExchangeProcessor` (BigDecimal-based, invariant-checked) or `ExchangeProcessor` (raw `double` math via `Maths.pow`), selected by the `hardenedCalc` flag: [1](#0-0) 

Critically, the negative-balance guard is gated behind `hardenedCalc`: [2](#0-1) 

When `hardenedCalc` is false (the legacy `ExchangeProcessor` path), `newFirstTokenBalance`/`newSecondTokenBalance` are computed with plain `+`/`-` on `long` (no overflow check) from a `double`-precision Bancor formula (`exchangeToSupply`/`exchangeFromSupply` truncate via cast to `long`): [3](#0-2) 

`ExchangeTransactionActuator.execute()` and `.doValidate()` trust this returned quantity directly to credit/debit the trader and to update the stored pool balances, with no post-hoc sanity check on the pool state: [4](#0-3) 

Because rounding/truncation and floating-point error in `exchangeToSupply`/`exchangeFromSupply` are not bounded, repeated small trades (especially against a low-liquidity pool, e.g. one created via `ExchangeCreateActuator` with attacker-chosen initial balances) can accumulate rounding bias in the trader's favor. Since the non-hardened path performs no invariant check, the pool's stored `firstTokenBalance`/`secondTokenBalance` can drift to values that no longer reflect real backing (including going negative or effectively unbacked), while the trader's own account balance/asset amount is legitimately credited via `addAssetAmountV2`/`setBalance`. This is analogous to the MakinaFi class of bug: a DeFi execution/accounting engine trusting an internal price/exchange calculation that is not bounded by an invariant check, allowing extraction of value beyond what the pool actually holds.

### Impact Explanation
This directly enables theft of TRX/TRC10 assets from the exchange pool and/or creation of unbacked balances: an attacker can extract more value from `ExchangeInjectActuator`/`ExchangeTransactionActuator`-created pools than the pool's real backing supports, since the safety check that would reject such an outcome (`newFirstTokenBalance < 0 || newSecondTokenBalance < 0`) is only enforced when `AllowHardenExchangeCalculation` is active. If that proposal is not enabled network-wide, every TRC10 exchange pool on the network is exposed. This is a High severity, unauthorized-value-extraction class of bug reachable purely by broadcasting `ExchangeCreateContract`/`ExchangeInjectContract`/`ExchangeTransactionContract` transactions.

### Likelihood Explanation
Reachable by any unprivileged account: create a low-liquidity exchange pool with `ExchangeCreateActuator`, inject minimal amounts, then repeatedly call `ExchangeTransactionActuator` with crafted small quantities to accumulate favorable rounding via the floating-point `Maths.pow`-based formula. No special permission or witness/committee role is required — this is a pure actuator validate/execute path issue. Exploitability depends on whether `AllowHardenExchangeCalculation` (a chain parameter, presumably toggled by an SR proposal, per `DynamicPropertiesStore`/`ProposalUtil`) is enabled on the target network; I was not able to confirm within the available index whether this proposal defaults to enabled on mainnet — this should be verified against live chain parameters before treating this as immediately exploitable in production.

### Recommendation
Make the pool-balance invariant check (`newFirstTokenBalance >= 0 && newSecondTokenBalance >= 0`) unconditional in `ExchangeCapsule.transaction()`, regardless of `hardenedCalc`, and route all exchange math through overflow-checked arithmetic (as `SafeExchangeProcessor`/`StrictMathWrapper` already do) rather than allowing the legacy `double`-based `ExchangeProcessor` path to bypass invariant enforcement.

### Proof of Concept
1. Broadcast `ExchangeCreateContract` to create a TRC10/TRX pool with small `firstTokenBalance`/`secondTokenBalance` (`ExchangeCreateActuator.execute`, `actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java:36-135`).
2. On a network where `AllowHardenExchangeCalculation` is not enabled, repeatedly broadcast `ExchangeTransactionContract` trades with quantities chosen to exploit floating-point truncation bias in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` (`chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java:17-45`).
3. Observe that `ExchangeTransactionActuator.execute()` (`actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java:61-96`) credits the attacker's account via `addAssetAmountV2`/`setBalance` while the pool's stored balances (via `ExchangeCapsule.transaction`) are never checked for going negative/unbacked, since that check only fires when `hardenedCalc` is true (`chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java:160-162`).

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-162)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-96)
```java
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

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);
```
