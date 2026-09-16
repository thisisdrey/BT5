### Title
Non-deterministic floating-point AMM pricing in `ExchangeProcessor` can cause cross-architecture consensus divergence - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
`ExchangeTransactionContract`, a transaction any unprivileged account can broadcast to trade against a TRC10 Bancor-style liquidity pool, computes the swap output using platform-dependent double-precision `Math.pow` unless the chain-wide `ALLOW_HARDEN_EXCHANGE_CALCULATION` parameter has been activated (default 0/off). This is the same class of bug flagged in the Reserve Protocol report: an unsafe numeric operation used to derive an exchange rate/settlement amount produces results that are not guaranteed to be identical across execution environments, so the exchange pool state (and thus account balances) can diverge between nodes that must reach identical consensus on the same transaction.

### Finding Description
`ExchangeCapsule.transaction()` picks between two `Processor` implementations depending on the `hardenedCalc` flag (which is only true when `dynamicStore.allowHardenExchangeCalculation()` returns true): [1](#0-0) 

By default this flag is 0, so trades use `ExchangeProcessor`, whose bonding-curve math is implemented with raw `double` arithmetic and truncation via `(long) issuedSupply` / `(long) exchangeBalance`: [2](#0-1) 

The exponentiation is dispatched through `Maths.pow(a, b, useStrictMath)`, which explicitly chooses between `StrictMathWrapper.pow` (guaranteed bit-for-bit `StrictMath.pow`) and `MathWrapper.pow` when `useStrictMath` is false: [3](#0-2) 

Critically, `MathWrapper` is implemented separately per CPU architecture (`platform/.../arm/org/tron/common/math/MathWrapper.java` and `platform/.../x86/org/tron/common/math/MathWrapper.java`), which is the codebase's own acknowledgment that non-strict `Math.pow` results are not guaranteed identical across hardware/JIT implementations. `ExchangeTransactionActuator.execute()` reads `dynamicStore.allowStrictMath()` at execution time and feeds it straight into `exchangeCapsule.transaction(...)`, so the actual settlement amount (`anotherTokenQuant`) that gets written into on-chain account and exchange-pool balances depends on this non-strict, per-platform floating point path whenever the harden proposal hasn't been activated: [4](#0-3) 

This is directly analogous to the Reserve Protocol bug class: a quirk in the *pricing/settlement calculation* of an AMM-style exchange (rounding/precision behavior that isn't provably invariant) is used to derive a value (here, `anotherTokenQuant`, i.e., the exchange rate outcome) that is then committed to persistent state affecting balances of the calling account and the shared liquidity pool used by all future traders — exactly like Compound's redeem rounding affecting a shared `exchangeRate` that later actuators/consumers rely on.

### Impact Explanation
If two full nodes (e.g., one running on x86, one on ARM, or simply different JVM/JIT optimization levels) compute a different `long` result for the same `ExchangeTransactionContract`, they will persist different account balances, different TRC10 asset balances, and different `ExchangeCapsule` pool balances for the same block. Since `Manager` block application requires deterministic state transition and all nodes must derive the exact same state root, this produces a state/consensus divergence — a chain split — triggerable by any account simply broadcasting an ordinary `ExchangeTransactionContract` transaction against any active TRC10 exchange pool, with no special privileges required.

### Likelihood Explanation
Likelihood depends on whether `MathWrapper.pow` genuinely diverges from `StrictMath.pow` on real production hardware for these specific input ranges, and whether `ALLOW_HARDEN_EXCHANGE_CALCULATION` has already been activated (the mitigating fix appears to already exist in this codebase as an opt-in proposal, similar to how Reserve's fix was proposed as revenue-hiding). I could not verify from the index whether this proposal is active on current mainnet, nor could I inspect the exact platform-specific `MathWrapper.pow` implementations (`platform/src/main/java/arm/...` and `platform/src/main/java/x86/...`) in enough depth in this session to confirm an actual numeric divergence for concrete pool sizes — the index only confirmed their existence and role. This is a real architectural risk that the project itself has already partially mitigated via `StrictMathWrapper`/`ALLOW_HARDEN_EXCHANGE_CALCULATION`, which suggests the underlying non-strict math path was considered unsafe by the maintainers, but I cannot confirm exploitability with concrete numbers without deeper inspection of the arm/x86 `MathWrapper.pow` bodies.

### Recommendation
- Confirm whether `ALLOW_HARDEN_EXCHANGE_CALCULATION` (and the equivalent `SafeExchangeProcessor` BigDecimal-based path) is activated on production networks; if not, activate it, since it removes the platform-dependent floating point path entirely.
- Long term, deprecate/remove `ExchangeProcessor`'s double-based path and the legacy `Maths`/`MathWrapper` split from any code that materializes into consensus-critical state, keeping only `SafeExchangeProcessor` (BigDecimal, deterministic) or `StrictMathWrapper` (guaranteed `StrictMath` semantics).
- Add an explicit consensus-safety test that runs `ExchangeProcessor.exchange` under both `arm` and `x86` `MathWrapper` implementations for a wide swath of realistic pool balances/quantities and asserts identical `long` outputs (or removes the platform split altogether).

### Proof of Concept
Not independently reproduced in this session (would require running the same `ExchangeTransactionContract` swap through `MathWrapper.pow`'s ARM and x86 implementations with values near a rounding boundary, e.g. `quant` chosen so `1 + quant/newBalance` sits at the edge of double precision for `pow(x, 0.0005)`); the finding is based on static code review of [5](#0-4)  combined with the project's own architecture-specific `MathWrapper` split and the opt-in `ALLOW_HARDEN_EXCHANGE_CALCULATION`/`SafeExchangeProcessor` mitigation, which together indicate the maintainers recognize non-strict `Math.pow` as a genuine cross-platform divergence risk in this exact code path.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

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

**File:** common/src/main/java/org/tron/common/math/Maths.java (L17-19)
```java
  public static double pow(double a, double b, boolean useStrictMath) {
    return useStrictMath ? StrictMathWrapper.pow(a, b) : MathWrapper.pow(a, b);
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
