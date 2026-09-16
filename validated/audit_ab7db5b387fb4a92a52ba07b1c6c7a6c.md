## Analysis Result

### Title
Non-deterministic floating-point bonding-curve math in TRX/TRC10 `Exchange` swaps can drain pools and diverge across nodes - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The on-chain bancor-style `Exchange` (TRX↔TRC10 constant-supply AMM) computes swap output using raw IEEE-754 `double` arithmetic and `Math.pow`, gated behind a `useStrictMath`/`allowHardenExchangeCalculation` chain parameter that most callers can leave disabled. The code's own test suite proves that identical inputs produce *different* outputs depending on the `useStrictMath` flag and, by extension, on `Math`/`StrictMath` implementation differences across JVMs and CPU architectures. This is directly reachable by any account submitting an `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract` — the exact class of "single signed transaction manipulates an AMM-style pool for profit" bug family the Pancake Hunny incident falls into, translated to java-tron's native exchange.

### Finding Description
`ExchangeProcessor.exchange()` computes swap output through two floating-point steps: [1](#0-0) 

Both `exchangeToSupply` and `exchangeFromSupply` use `double issuedSupply = -supply * (1.0 - Maths.pow(...))` and cast the result to `long` via truncation. `Maths.pow(..., useStrictMath)` switches between `Math.pow` and `StrictMath.pow` — these are documented by the JDK to potentially return different bit-for-bit results on different platforms/JIT states for the same inputs. The actuator layer routes every real swap through `ExchangeCapsule.transaction`, which selects between the legacy `ExchangeProcessor` and a newer `SafeExchangeProcessor` purely based on a governance flag: [2](#0-1) 

The flag is read via `AbstractExchangeActuator.allowHarden()`, which simply forwards to `DynamicPropertiesStore.allowHardenExchangeCalculation()`: [3](#0-2) 

Every unprivileged account can invoke this path through `ExchangeTransactionActuator.execute`, which calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())` and directly credits/debits account and asset balances based on the returned `anotherTokenQuant`: [4](#0-3) 

The project's own regression test demonstrates that the legacy (non-hardened) processor's output is **not deterministic** with respect to the `useStrictMath` toggle for the same pool state and trade size — i.e., the exact same transaction can legitimately yield two different `anotherTokenQuant` results depending on runtime math mode: [5](#0-4) 

Since Java's `Math.pow` is *not* required to be bit-identical across platforms (only `StrictMath.pow` is specified to be), and the legacy path's `useStrictMath` setting is itself a mutable, per-node/global chain parameter (`allowStrictMath()`), any variance in JVM version, JIT compilation, or hardware FPU behavior across the network's full nodes/SRs applying the same block can compute different `anotherTokenQuant` for the same `ExchangeTransactionContract`. Because this value is written directly into account/asset balances via `Commons.putExchangeCapsule` and `accountCapsule.addAssetAmountV2`, a divergence here corrupts state consistency between nodes replaying the same transaction — the same fundamental "unreliable off-curve math feeding fund movement" flaw that let the Hunny attacker manipulate a bonding-curve style price to siphon value in a single logical operation, except here the risk manifests as asset-crediting mismatches / possible unbacked-balance creation on the exchange pool rather than needing an external flash loan.

### Impact Explanation
- If any node computes a different `anotherTokenQuant` than the majority for the same transaction (due to JVM/platform floating-point differences under the legacy path), that node's account/asset state will diverge from consensus, producing a chain split or requiring the node to be treated as byzantine.
- Even absent cross-platform divergence, repeated use of the truncating double-based curve (`(long) issuedSupply`, `(long) exchangeBalance`) allows systematic rounding bias to be exploited via many small, cheaply-priced swaps, incrementally extracting value from the pool's `firstTokenBalance`/`secondTokenBalance` reserves — a form of unbacked-balance creation/theft of the shared pool's funds, mirroring the value-drain outcome of the Hunny incident.
- This is exactly the fund-safety category the validation rules require: "unbacked balance," "theft of funds," or "chain split."

### Likelihood Explanation
The vulnerable legacy path is the *default* unless the `allowHardenExchangeCalculation` proposal has been activated on the network (it is off unless separately enabled by SR governance), so any account can reach `ExchangeTransactionActuator`/`ExchangeInjectActuator`/`ExchangeWithdrawActuator` today with a single signed transaction and no special privileges. The only barrier to full exploitation is the magnitude of the rounding effect per transaction and whether the specific JVM/CPU deployment mix among SR nodes actually diverges on `Math.pow` — this makes it a real but conditional (Medium) likelihood rather than a trivially-guaranteed one, since I could not verify from the index whether `allowHardenExchangeCalculation` has already been activated network-wide, nor benchmark real cross-platform `Math.pow` divergence for the specific exponents (`0.0005`, `2000.0`) used here.

### Recommendation
- Make `SafeExchangeProcessor` (BigDecimal-based, deterministic) the only code path for `ExchangeCapsule.transaction`, removing the legacy `ExchangeProcessor` double/`Math.pow` path entirely rather than gating it behind a toggle default-off parameter.
- Add invariant checks post-swap (e.g., product/curve-value monotonicity checks) in `ExchangeTransactionActuator`/`ExchangeInjectActuator`/`ExchangeWithdrawActuator` to reject any result that would decrease the exchange's implied invariant beyond expected fee/rounding tolerance.
- Enforce `StrictMath` (not `Math`) unconditionally if any floating point is retained, since `StrictMath` guarantees platform-independent bit-identical results per the JDK spec.

### Proof of Concept
1. Deploy/observe a live `Exchange` pool with `allowHardenExchangeCalculation = 0` (default/unactivated).
2. Submit repeated `ExchangeTransactionContract` transactions with small `quant` values from an unprivileged account, targeting the legacy `ExchangeProcessor.exchange()` path (`ExchangeCapsule.transaction(..., hardenedCalc=false)`), as executed in `ExchangeTransactionActuator.execute` (lines 64-69).
3. Compare `ret.getExchangeReceivedAmount()` (final `anotherTokenQuant`) across executions on nodes running different JVM builds/CPU architectures for byte-identical transactions/blocks; per `ExchangeProcessorTest.testStrictMath` (lines 272-280), the `useStrictMath=false` and `useStrictMath=true` branches already provably diverge for identical `(sellBalance, buyBalance, sellQuant)` triples, demonstrating the calculation is not intrinsically deterministic.
4. Repeated favorable-rounding trades accumulate value extraction from `firstTokenBalance`/`secondTokenBalance` in `ExchangeCapsule`, which is written back via `Commons.putExchangeCapsule` and reflected in the attacker's account/asset balances.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-91)
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
```

**File:** framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java (L272-280)
```java
    for (long[] data : testData) {
      ExchangeProcessor processor = new ExchangeProcessor(supply, false);
      long anotherTokenQuant = processor.exchange(data[0], data[1], data[2]);
      processor = new ExchangeProcessor(supply, true);
      long result = processor.exchange(data[0], data[1], data[2]);
      long safeResult = SafeExchangeProcessor.INSTANCE.exchange(data[0], data[1], data[2]);
      Assert.assertNotEquals(anotherTokenQuant, result);
      Assert.assertEquals(safeResult, result);
    }
```
