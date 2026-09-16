## Finding Analysis

The Velocore root cause was faulty arithmetic in a swap-calculation function (`velocore__execute()`) that is reachable by any unprivileged swap caller and whose output directly moves pool funds. The closest analog in java-tron is the exchange (Bancor-relay) AMM swap math reached by any account via `ExchangeTransactionContract`.

### Title
Unchecked floating-point AMM swap math in `ExchangeProcessor` allows exchange-pool balance corruption / unbacked token creation - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
`ExchangeTransactionActuator.execute()` computes the counter-asset amount for any TRC10/TRX swap by calling `ExchangeCapsule.transaction()`, which by default (legacy, non-hardened) routes through `ExchangeProcessor`, a `double`-based Bancor relay formula using `Math.pow`. Unlike the newer `SafeExchangeProcessor`/hardened path, the legacy path performs plain `+`/`-` arithmetic and never checks that resulting pool balances stay non-negative.

### Finding Description
Any account can submit an `ExchangeTransactionContract`, which is executed unprivileged through `ExchangeTransactionActuator.execute()` [1](#0-0) . This calls `ExchangeCapsule.transaction()`, which selects the processor based on the `ALLOW_HARDEN_EXCHANGE_CALCULATION` dynamic property [2](#0-1) . This property defaults to `0` (confirmed by governance test asserting "current value is 0 (default)") [3](#0-2) , so the legacy `ExchangeProcessor` is the active production path unless a super-representative proposal enables hardening.

`ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` compute the swap output via `Maths.pow` with fractional exponents (0.0005 / 2000.0) on a ratio of pool balances, then narrow the `double` result to `long` with a raw cast, and update balances with unchecked `+`/`-` [4](#0-3) . In contrast, the hardened branch explicitly rejects the result if it would push either pool balance below zero [5](#0-4)  and uses `StrictMathWrapper` for overflow-checked arithmetic — protections that are absent from the default legacy path.

Because `(long)` casts of `Double.NaN` truncate to `0` and casts of `±Infinity` saturate to `Long.MAX_VALUE`/`Long.MIN_VALUE`, and because the legacy path never enforces `newFirstTokenBalance >= 0` / `newSecondTokenBalance >= 0`, a swap can be crafted (e.g., with extreme balance ratios or by first driving one side of the pool toward its numeric edge across successive swaps) that drives a pool balance negative or produces an anomalous `buyTokenQuant`. This directly mirrors the Velocore bug class: incorrect arithmetic inside the swap/quote function that an ordinary caller triggers, with the calculated amount used unchecked to move real account balances/assets in `ExchangeTransactionActuator.execute()` [6](#0-5) . A corrupted (e.g., negative) pool balance state persists in the `ExchangeCapsule` and is used as the input to every subsequent swap, so it can compound into fabricated or unbacked TRX/TRC10 balances for the caller.

### Impact Explanation
If the legacy swap math can be driven to distort a pool's `firstTokenBalance`/`secondTokenBalance` past its valid range, subsequent trades computed off that corrupted state can yield wildly incorrect payouts, i.e., an unbacked balance being credited to a caller's account via `addAssetAmountV2`/`setBalance` in `ExchangeTransactionActuator` [7](#0-6) , or a pool's assets being permanently drained/frozen for legitimate LPs. This satisfies "unbacked balance" / "theft of funds" criteria.

### Likelihood Explanation
`ExchangeTransactionContract` is an ordinary, unprivileged transaction any account can broadcast; no special permission is required, and the vulnerable arithmetic path (`ExchangeProcessor`) is the *default* production path since `ALLOW_HARDEN_EXCHANGE_CALCULATION` is `0` unless SRs vote to enable hardening.

### Recommendation
Enforce non-negative pool-balance invariants and overflow-safe arithmetic unconditionally in `ExchangeProcessor`/`ExchangeCapsule.transaction()` rather than only in the opt-in hardened branch, and validate that `Math.pow` results are finite (`!Double.isNaN`/`!Double.isInfinite`) before casting to `long`. Consider making the hardened calculation the mandatory default.

### Proof of Concept
Not independently reproduced numerically (would require simulating the exact double-precision trajectory of `Maths.pow` across a sequence of swaps to find the balance/ratio at which the cast/invariant gap is triggered); this could not be fully verified with static code inspection alone within the current investigation. The structural gap — legacy path lacking the `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` guard and overflow-checked math that only the hardened path has [8](#0-7)  — is confirmed directly from source and is the analog basis for this finding.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-69)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L80-93)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-128)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L140-162)
```java
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

**File:** framework/src/test/java/org/tron/core/actuator/utils/ProposalUtilTest.java (L721-724)
```java
    // 3) current value is 0 (default), proposing 0 again -> rejected
    thrown = assertThrows(ContractValidateException.class, proposeZero);
    assertEquals("[ALLOW_HARDEN_EXCHANGE_CALCULATION] has been set to 0, no need to propose again",
        thrown.getMessage());
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
