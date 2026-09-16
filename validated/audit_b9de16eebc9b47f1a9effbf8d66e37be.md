### Title
Non-hardened bancor Exchange calculation performs no post-trade solvency check, allowing precision-driven exchange-pool imbalance/drain - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java)

### Summary
`ExchangeTransactionActuator`, reachable by any signed account with a valid `ExchangeTransactionContract`, computes the counter-token amount via `ExchangeCapsule.transaction()`. When `hardenedCalc` is false (the default/legacy path, `dynamicStore.allowHarden()` gated by a separate proposal), the result of `ExchangeProcessor.exchange()` — computed with `double`/`Math.pow` floating point arithmetic — is applied directly to the pool balances with **no check that the new balances are non-negative or internally consistent**. The negative-balance guard (`if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) throw ...`) is only executed for the hardened path. [1](#0-0) 

### Finding Description
`ExchangeProcessor.exchange()` implements a Bancor-style relay-token conversion using raw `double` math (`Maths.pow`, division, and casts to `long`), which is inherently lossy at the bounds of large/small balances and quantities: [2](#0-1) 

`ExchangeCapsule.transaction()` calls this processor and then unconditionally updates `firstTokenBalance`/`secondTokenBalance` with the result, only validating for negative resulting balances when `hardenedCalc` (i.e., `allowHarden()`) is true: [3](#0-2) 

`ExchangeTransactionActuator.execute()` invokes this transaction with `dynamicStore.allowStrictMath()` and `allowHarden()`, and directly credits `anotherTokenQuant` (the amount computed by the lossy processor) to the caller's account, without cross-checking it against the actual pool state after the update: [4](#0-3) 

`doValidate()` for the transaction only checks that the *pre-trade* token balance doesn't exceed a global limit and that the seller has enough balance of the input token — it never re-validates the *post-trade* invariant (i.e., that `buyTokenBalance` after the trade remains within bounds relative to what the actuator will actually credit): [5](#0-4) 

Because the safety check exists only behind `allowHarden()`/`SafeExchangeProcessor` (a separately-gated hardened calculation path introduced later), any deployment where this hardened flag is not enabled continues to run the legacy floating-point path with no bound on resulting pool balances. An attacker can craft `tokenQuant` values (e.g., extreme ratios of `sellTokenBalance`/`quant` chosen to maximize double-precision rounding error in `Math.pow`) to receive an `anotherTokenQuant` that is inconsistent with the actual bonding-curve output, extracting more value from `secondTokenBalance` than the trade should yield, while the pool's recorded balance can drift to an inconsistent (or in edge cases negative-implying) state that is never rejected. This directly parallels the "DeFi Saver ... exchange leak" root cause: fund loss stemming from a decentralized exchange/AMM computing balances without solvency verification after price computation.

### Impact Explanation
If exploitable at extreme values, this allows an unprivileged caller to drain a `ExchangeTransactionContract`-based (V1) or `ExchangeV2Store`-based liquidity pool of `TRX`/TRC10 assets beyond what the bonding-curve math should allow, resulting in permanent, unbacked loss of pooled funds for other holders of that exchange pair — a direct theft-of-funds impact reachable purely from a signed transaction.

### Likelihood Explanation
Medium: the legacy `ExchangeProcessor` path is still reachable whenever `allowHarden()` (a chain parameter driven by proposal) is not enabled, and the actuator performs no independent bound-check on the computed `anotherTokenQuant` against actual pool solvency in that mode. Exploitation requires finding specific balance/quantity combinations that maximize `double` rounding error in `Maths.pow`, which needs some analysis but no privileged access — any account can submit `ExchangeTransactionContract` transactions.

### Recommendation
- Enforce the same non-negative/solvency invariant check (`newFirstTokenBalance >= 0 && newSecondTokenBalance >= 0`, plus consistency with the constant-product/relay-token invariant) unconditionally in `ExchangeCapsule.transaction()`, regardless of `hardenedCalc`.
- Migrate the legacy `ExchangeProcessor` double-based math fully to the `BigDecimal`-based `SafeExchangeProcessor` and deprecate/retire the floating point path, rather than gating the fix behind an opt-in dynamic parameter.
- Add regression tests using boundary balances/quantities designed to maximize floating-point rounding drift to confirm the invariant holds under all `allowHarden()` settings.

### Proof of Concept
Not independently reproducible from the index alone — pinpointing an exact `(firstTokenBalance, secondTokenBalance, tokenQuant)` triple that causes exploitable double-precision drift in `Maths.pow`/`StrictMathWrapper.pow` would require running `ExchangeProcessor.exchange()` locally across a large search space of balances/quantities to find a case where the legacy (non-hardened) path yields a `buyTokenQuant` inconsistent with a `BigDecimal`-computed reference, while passing all `doValidate()` checks in `ExchangeTransactionActuator`. This is a concrete, actionable next step for a Devin session with code-execution access, but I could not execute it in this read-only analysis.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-97)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L182-216)
```java
    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
      throw new ContractValidateException("token is not in exchange");
    }

    if (tokenQuant <= 0) {
      throw new ContractValidateException("token quant must greater than zero");
    }

    if (tokenExpected <= 0) {
      throw new ContractValidateException("token expected must greater than zero");
    }

    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }

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

```
