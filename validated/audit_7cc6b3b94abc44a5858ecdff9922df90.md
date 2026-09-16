### Title
Unbacked TRC10 Exchange pool via unchecked negative-balance path when hardened calculation is disabled - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java)

### Summary
The TRC10 Bancor-style AMM (`ExchangeCapsule.transaction`) is java-tron's closest analog to Synapse's manipulable metapool. Its virtual-balance ratio is computed with a floating-point Bancor relay formula, and the only sanity check that prevents the pool balances from going negative (i.e., becoming unbacked/insolvent) is gated behind an opt-in `hardenedCalc` flag driven by the `allowHardenExchangeCalculation` dynamic property.

### Finding Description
`ExchangeCapsule.transaction()` computes the new pool balances via `ExchangeProcessor.exchange()`, which relies on `double`-based `Math.pow` relay-token math [1](#0-0) . This is the same class of virtual-price computation later exploited in curve/metapool-style bridges (as in the Synapse report): repeated skewed trades against a low-liquidity pool can push the computed output disproportionately relative to the pool's actual backing.

Critically, the post-trade sanity check that would reject a resulting negative balance is only executed when `hardenedCalc` is true: [2](#0-1) 

`hardenedCalc` is derived from `allowHarden()` in `AbstractExchangeActuator`, which reads the `allowHardenExchangeCalculation` dynamic property — a governance-controlled/proposal-activated flag, not a value that is guaranteed to be enabled on every chain/network by default: [3](#0-2) 

When this flag is off (its default/unactivated state), `ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())` and commits whatever balances the double-precision Bancor formula produces, without ever checking that `newFirstTokenBalance`/`newSecondTokenBalance` remain non-negative: [4](#0-3) 

This is directly reachable by any unprivileged, fee-paying transaction broadcaster via a signed `ExchangeTransactionContract` — no special privilege required, matching the report's "unprivileged order placer" reachability requirement.

### Impact Explanation
If the unguarded (non-hardened) path allows the relay-supply math to drive one side of the pool balance negative (or effectively drain it below what real depositors are entitled to), the on-chain `Exchange` capsule balances no longer reflect real backing. Later liquidity providers calling `ExchangeWithdrawActuator` compute their proportional payout from these corrupted balances using `BigInteger`/`BigDecimal` ratios [5](#0-4) , so an attacker who first skews the pool can extract more value than they contributed, at the expense of other LPs/traders — a direct funds-theft/unbacked-balance outcome, the same fund-safety failure mode described in the Synapse post-mortem (funds moved out of the pool beyond what the pool's true reserves support).

### Likelihood Explanation
The attack requires only ordinary signed transactions (`ExchangeTransactionContract`) against a real, low-liquidity TRC10 exchange pool — something any address can create via `ExchangeCreateActuator` and then trade against. The exploit condition depends on the `allowHardenExchangeCalculation` proposal not being active on the target network; since this flag is proposal-gated (not permanently on), any chain/testnet/sidechain where the community hasn't activated hardening remains exposed to this unchecked path. The extensive dedicated "hardened" test suite (`hardenedSuccessExchangeTransaction`, `hardenedPrecisionCheckFailsWhenImprecise`, etc.) confirms the developers recognized this class of balance/precision risk and built a safety net — but that safety net is explicitly optional.

### Recommendation
Make the negative-balance/backing check in `ExchangeCapsule.transaction()` unconditional (not gated by `hardenedCalc`), and consider migrating the core relay-token math (`ExchangeProcessor`) to the `BigDecimal`-based `SafeExchangeProcessor` unconditionally rather than as an opt-in feature, to eliminate floating-point precision-driven balance manipulation regardless of proposal activation state.

### Proof of Concept
1. Create a TRC10 exchange pool with intentionally low `secondTokenBalance` relative to `firstTokenBalance` via `ExchangeCreateActuator`.
2. On a network where `allowHardenExchangeCalculation` has not been activated, submit an `ExchangeTransactionContract` with a large `tokenQuant` of the abundant token.
3. `ExchangeCapsule.transaction()` computes `newSecondTokenBalance` via the double-precision Bancor formula in `ExchangeProcessor`; because `hardenedCalc` is `false`, the `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` guard is skipped and the resulting (potentially negative or over-drained) balances are persisted via `Commons.putExchangeCapsule`.
4. Subsequent `ExchangeWithdrawActuator` calls by legitimate LPs compute payouts from the now-corrupted balances, allowing the attacker (who already withdrew the disproportionate `anotherTokenQuant`) to have extracted more value than contributed, at the expense of the pool/other LPs.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-99)
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

      ret.setExchangeReceivedAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-89)
```java
      BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
      BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
      BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
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
```
