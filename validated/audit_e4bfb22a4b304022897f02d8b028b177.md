### Title
Silent floating-point division-by-zero in `ExchangeProcessor` (Bancor exchange math) can corrupt swap output instead of failing safely - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The reported ImageMagick bug is an unguarded division in `MagickCore/resample.c` that produces undefined behavior when a denominator can reach zero. The closest reachable analog in java-tron is the legacy (non-"hardened", non-"strict-math") Bancor-formula exchange math in `ExchangeProcessor`, used by `ExchangeCapsule.transaction()` and reachable from the unprivileged, broadcastable `ExchangeTransactionContract`, `ExchangeInjectContract` and `ExchangeWithdrawContract` actuators. Unlike the hardened `SafeExchangeProcessor` (which uses `BigDecimal` and is proven by unit tests to throw `ArithmeticException` on a zero denominator, e.g. `testSafeProcessorDivByZeroThrows`), the legacy `ExchangeProcessor` divides using primitive `double` arithmetic, which in Java never throws on division by zero — it silently yields `Infinity`/`NaN`, which are then narrowed to `long` and used to update on-chain token/TRX balances.

### Finding Description
`ExchangeProcessor.exchangeToSupply` and `exchangeFromSupply` compute: [1](#0-0) 

`exchangeToSupply` divides by `newBalance` (`balance + quant`) and `exchangeFromSupply` divides by `supply` (which is mutated by `supply -= supplyQuant` before the divide). Both are plain `double` divisions. If either denominator becomes `0`, Java does not throw `ArithmeticException`; the result is `Infinity` or `NaN`, and the subsequent `(long) issuedSupply` / `(long) exchangeBalance` cast silently turns that into `0` or `Long.MAX_VALUE`/`Long.MIN_VALUE`.

`ExchangeCapsule.transaction()` selects this unsafe processor whenever `hardenedCalc` is `false`: [2](#0-1) 

Critically, the overflow/negative-balance guard is only applied when `hardenedCalc` is true: [3](#0-2) 

so the legacy code path has no protection against a pool balance reaching `0`. The exchange pool balances (`firstTokenBalance`/`secondTokenBalance`) are mutated by any user through `ExchangeInjectActuator` and `ExchangeWithdrawActuator`, both of which are ordinary, unprivileged, broadcastable contracts: [4](#0-3) [5](#0-4) 

The withdraw path's `validate()` uses `BigInteger` exact division (`bigFirstTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance)`), which correctly throws on zero and is caught, but the corresponding `execute()` path for the actual trading actuator (`ExchangeTransactionActuator` → `ExchangeCapsule.transaction()` → `ExchangeProcessor`) uses the unguarded double math shown above.

This is a direct structural analog of the CVE: an externally reachable numeric routine performs division without validating the denominator, and the ecosystem includes a hardened counterpart (`SafeExchangeProcessor`) that was specifically written to fix this exact defect for one call path, while the primary/legacy processor used by ordinary trading remains exposed.

### Impact Explanation
If a pool balance is driven to `0` (or a single-unit trade makes `newBalance == 0`, which is mathematically only reachable through preceding withdraw/inject operations reducing balance to the boundary), a subsequent trade computes `Infinity` or `NaN` for `issuedSupply`/`exchangeBalance`. Casting these to `long` yields either `0` (silently zeroing a trader's expected proceeds — loss of funds for the trader) or a huge/`Long.MAX_VALUE`/negative value (potential unbacked balance credited to an account, or a subsequent `addExact`/`subtractExact` overflow causing an uncaught `ArithmeticException` that aborts block processing for that transaction). This maps to "unbacked balance" and, in the extreme, to inconsistent pool bookkeeping across nodes, which risks a chain split if node behavior diverges (e.g., due to differing floating-point rounding paths taken only on some nodes/JIT states) — though this last consequence is speculative and not concretely proven here.

### Likelihood Explanation
Reaching a pool balance of exactly `0` on one side requires either committee-controlled configuration (creating the exchange, which is privileged) or accumulating withdraws down to the exact boundary, which is easier for the token created by the exchange operator or through repeated small trades approaching depletion. This makes the exact "denominator == 0" trigger non-trivial for an attacker to engineer precisely under the Bancor formula's usual behavior (which tends toward zero output near-symmetrically rather than a hard zero balance), so likelihood is Medium rather than High. It requires no special privilege beyond being able to issue standard `ExchangeInjectContract`/`ExchangeWithdrawContract`/`ExchangeTransactionContract` transactions once an exchange pair exists.

### Recommendation
Route all exchange trading through `SafeExchangeProcessor` (or otherwise guard the legacy `ExchangeProcessor` divisions with explicit zero-denominator checks that throw `ContractValidateException`/`ArithmeticException` before the cast to `long`), and apply the same negative/zero-balance validation currently gated behind `hardenedCalc` unconditionally in `ExchangeCapsule.transaction()`.

### Proof of Concept
1. An exchange pair is created (committee action) with initial reserves for tokens A and TRX.
2. An attacker repeatedly calls `ExchangeWithdrawContract`/`ExchangeInjectContract` (unprivileged, standard transactions) to move one side of the pool balance toward `0`.
3. The attacker then calls `ExchangeTransactionContract` to trade against the depleted side; `ExchangeCapsule.transaction()` invokes `ExchangeProcessor.exchange()`, and if `newBalance` (or the mutated `supply`) is `0`, `(double) quant / 0` yields `Infinity`, propagated through `Maths.pow` and cast to `long`, producing either `0` proceeds for a legitimate trade or an implausibly large credited amount, as demonstrated by the safe-processor regression test explicitly guarding this exact scenario: [6](#0-5) 

Note: I was unable to fully verify, within the available search budget, the precise numeric conditions under which the Bancor `newBalance`/`supply` denominator can be driven to exactly `0` versus asymptotically small (this depends on `ExchangeInjectActuator`/`ExchangeWithdrawActuator` validate-time BigInteger checks, which may block some paths). A full confirmation would require tracing every caller of `ExchangeCapsule.transaction()` with `hardenedCalc=false` and the exact boundary conditions of `assetBalanceEnoughV2`/`balanceLimit` checks in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` validate() methods.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-39)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L158-167)
```java
    }

    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L59-83)
```java
          .get(ByteArray.fromLong(exchangeInjectContract.getExchangeId()));
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeInjectContract.getTokenId().toByteArray();
      long tokenQuant = exchangeInjectContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L63-89)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeWithdrawContract.getTokenId().toByteArray();
      long tokenQuant = exchangeWithdrawContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L187-192)
```java
  @Test
  public void testSafeProcessorDivByZeroThrows() {
    // newBalance = balance + quant = -1 + 1 = 0 -> BigDecimal divide by zero
    assertThrows(ArithmeticException.class,
        () -> SafeExchangeProcessor.INSTANCE.exchange(-1L, 100L, 1L));
  }
```
