### Title
Non-deterministic bancor-formula pricing in `ExchangeProcessor` can diverge between CPU architectures, causing a chain split - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The external report describes a token-sale contract whose pricing formula was intended to be a smooth linear function but instead behaves as a discontinuous step function because of how the math was implemented. The equivalent bug class in java-tron is the TRX/TRC10 bancor-relay pricing formula used by the Exchange actuators, which is computed with platform-dependent floating point (`Math.pow`) unless a governance-gated "hardened" path is active. Two different, functionally-inconsistent implementations of the same price curve exist side by side, and the legacy one is not guaranteed to produce identical results across CPU architectures.

### Finding Description
`ExchangeCapsule.transaction()` picks between two `Processor` implementations depending on chain parameters: [1](#0-0) 

- The legacy `ExchangeProcessor` computes the bancor-style price using `double` arithmetic and `Maths.pow(...)`, which dispatches to `MathWrapper.pow` unless `useStrictMath` is enabled: [2](#0-1) [3](#0-2) 

- On x86, `MathWrapper.pow` simply calls `Math.pow`, which the JVM is permitted to implement with hardware intrinsics that are not guaranteed to be bit-identical to `StrictMath.pow`: [4](#0-3) 

- On ARM, `MathWrapper.pow` instead consults a hardcoded lookup table of *previously observed* divergent `(base, exponent) -> result` triples collected from real chain history, falling back to `StrictMath.pow` for any input not already in the table: [5](#0-4) [6](#0-5) 

This table is finite and stops at block `9631452`; it is a patch for known historical divergences, not a general fix. Any new `(base, exponent)` pair produced by a transaction that has not previously been recorded will use `StrictMath.pow` on ARM but `Math.pow` (JIT/hardware intrinsic) on x86, and these two can legitimately return different IEEE-754 results for the same mathematical inputs. Because `exchangeToSupply`/`exchangeFromSupply` truncate the `double` result to a `long` with `(long) issuedSupply`, even a difference in the last bit of the `pow` result can change the truncated integer output: [7](#0-6) 

The safer, deterministic path (`SafeExchangeProcessor`, using `BigDecimal` and `StrictMathWrapper`) only activates when the `allowHardenExchangeCalculation` dynamic property is enabled (a network-wide committee/SR proposal parameter), and `useStrictMath`/hardening are separate flags gated similarly: [8](#0-7) [9](#0-8) 

Until both flags are turned on for a given network/exchange pool, any unprivileged account can trigger the vulnerable legacy code path simply by broadcasting an `ExchangeTransactionContract`, `ExchangeInjectContract`, or performing a withdraw on a bancor-relay TRX exchange pool.

### Impact Explanation
If a transaction drives the bancor calculation into `(base, exponent)` inputs that are not already present in the ARM hardcoded table and for which `Math.pow`/`StrictMath.pow` disagree in their last bit (a documented, real-world occurrence — the ARM table already lists dozens of historically observed mismatches), nodes running on different CPU architectures (or different JVM/JIT configurations) will persist different `firstTokenBalance`/`secondTokenBalance` values and different account asset balances after executing the identical transaction. This is a state-root divergence, i.e., a chain split, which is one of the explicitly accepted "Critical" impacts in scope (chain split / halted node).

### Likelihood Explanation
This is reachable by any account with TRX/TRC10 balance simply calling the standard `ExchangeTransactionActuator`/`ExchangeInjectActuator`/`ExchangeWithdrawActuator` APIs, no special privilege required. The likelihood of hitting an as-yet-unrecorded divergent `pow` input is non-trivial given that dozens of divergences have already been found in production history (as evidenced by the size of the hardcoded ARM table), and the network only has full protection once `allowHardenExchangeCalculation` (and `allowStrictMath`) are activated for all validators — a governance action that is not guaranteed to have happened for every legacy exchange pool.

### Recommendation
- Require `SafeExchangeProcessor` (BigDecimal + `StrictMathWrapper`) as the exclusive, non-optional pricing implementation for all exchange calculations, retiring `ExchangeProcessor`/`MathWrapper.pow` entirely rather than gating it behind an optional dynamic property.
- Remove architecture-specific `MathWrapper` implementations (`platform/x86`, `platform/arm`) for consensus-critical paths; consensus code should never depend on `Math.pow` hardware intrinsics.
- Audit all remaining call sites of `Maths.pow`/`MathWrapper.pow` for consensus-relevance and force them onto `StrictMathWrapper`/`BigDecimal`-based deterministic math.

### Proof of Concept
1. Deploy or use an existing bancor-relay TRX exchange pool (Exchange V1/V2) on a network where `allowHardenExchangeCalculation` and `allowStrictMath` are not enabled.
2. Have two nodes running on different CPU architectures (x86 vs ARM) validate the same block containing an `ExchangeTransactionContract` whose `sellTokenQuant`/balances yield a `(base, exponent)` pair for `Maths.pow` not present in the ARM `MathWrapper` static lookup table (populated only through block 9631452 in the current source).
3. Observe that `exchangeToSupply`/`exchangeFromSupply`'s truncated `long` outputs (`ExchangeProcessor.java` lines 17-38) can differ between the two nodes, producing different `ExchangeCapsule` balances and account balances, thereby diverging the chain state.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-38)
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
```

**File:** common/src/main/java/org/tron/common/math/Maths.java (L17-19)
```java
  public static double pow(double a, double b, boolean useStrictMath) {
    return useStrictMath ? StrictMathWrapper.pow(a, b) : MathWrapper.pow(a, b);
  }
```

**File:** platform/src/main/java/x86/org/tron/common/math/MathWrapper.java (L11-13)
```java
  public static double pow(double a, double b) {
    return Math.pow(a, b);
  }
```

**File:** platform/src/main/java/arm/org/tron/common/math/MathWrapper.java (L16-22)
```java
  private static final Map<PowData, Double> powData = Collections.synchronizedMap(new HashMap<>());
  private static final String EXPONENT = "3f40624dd2f1a9fc"; // 1/2000 = 0.0005

  public static double pow(double a, double b) {
    double strictResult = StrictMath.pow(a, b);
    return powData.getOrDefault(new PowData(a, b), strictResult);
  }
```

**File:** platform/src/main/java/arm/org/tron/common/math/MathWrapper.java (L27-79)
```java
  static {
    // init main-net pow data start
    addPowData("3ff0192278704be3", EXPONENT, "3ff000033518c576"); //  4137160(block)
    addPowData("3ff000002fc6a33f", EXPONENT, "3ff0000000061d86"); //  4065476
    addPowData("3ff00314b1e73ecf", EXPONENT, "3ff0000064ea3ef8"); //  4071538
    addPowData("3ff0068cd52978ae", EXPONENT, "3ff00000d676966c"); //  4109544
    addPowData("3ff0032fda05447d", EXPONENT, "3ff0000068636fe0"); //  4123826
    addPowData("3ff00051c09cc796", EXPONENT, "3ff000000a76c20e"); //  4166806
    addPowData("3ff00bef8115b65d", EXPONENT, "3ff0000186893de0"); //  4225778
    addPowData("3ff009b0b2616930", EXPONENT, "3ff000013d27849e"); //  4251796
    addPowData("3ff00364ba163146", EXPONENT, "3ff000006f26a9dc"); //  4257157
    addPowData("3ff019be4095d6ae", EXPONENT, "3ff0000348e9f02a"); //  4260583
    addPowData("3ff0123e52985644", EXPONENT, "3ff0000254797fd0"); //  4367125
    addPowData("3ff0126d052860e2", EXPONENT, "3ff000025a6cde26"); //  4402197
    addPowData("3ff0001632cccf1b", EXPONENT, "3ff0000002d76406"); //  4405788
    addPowData("3ff0000965922b01", EXPONENT, "3ff000000133e966"); //  4490332
    addPowData("3ff00005c7692d61", EXPONENT, "3ff0000000bd5d34"); //  4499056
    addPowData("3ff015cba20ec276", EXPONENT, "3ff00002c84cef0e"); //  4518035
    addPowData("3ff00002f453d343", EXPONENT, "3ff000000060cf4e"); //  4533215
    addPowData("3ff006ea73f88946", EXPONENT, "3ff00000e26d4ea2"); //  4647814
    addPowData("3ff00a3632db72be", EXPONENT, "3ff000014e3382a6"); //  4766695
    addPowData("3ff000c0e8df0274", EXPONENT, "3ff0000018b0aeb2"); //  4771494
    addPowData("3ff00015c8f06afe", EXPONENT, "3ff0000002c9d73e"); //  4793587
    addPowData("3ff00068def18101", EXPONENT, "3ff000000d6c3cac"); //  4801947
    addPowData("3ff01349f3ac164b", EXPONENT, "3ff000027693328a"); //  4916843
    addPowData("3ff00e86a7859088", EXPONENT, "3ff00001db256a52"); //  4924111
    addPowData("3ff00000c2a51ab7", EXPONENT, "3ff000000018ea20"); //  5098864
    addPowData("3ff020fb74e9f170", EXPONENT, "3ff00004346fbfa2"); //  5133963
    addPowData("3ff00001ce277ce7", EXPONENT, "3ff00000003b27dc"); //  5139389
    addPowData("3ff005468a327822", EXPONENT, "3ff00000acc20750"); //  5151258
    addPowData("3ff00006666f30ff", EXPONENT, "3ff0000000d1b80e"); //  5185021
    addPowData("3ff000045a0b2035", EXPONENT, "3ff00000008e98e6"); //  5295829
    addPowData("3ff00e00380e10d7", EXPONENT, "3ff00001c9ff83c8"); //  5380897
    addPowData("3ff00c15de2b0d5e", EXPONENT, "3ff000018b6eaab6"); //  5400886
    addPowData("3ff00042afe6956a", EXPONENT, "3ff0000008892244"); //  5864127
    addPowData("3ff0005b7357c2d4", EXPONENT, "3ff000000bb48572"); //  6167339
    addPowData("3ff00033d5ab51c8", EXPONENT, "3ff0000006a279c8"); //  6240974
    addPowData("3ff0000046d74585", EXPONENT, "3ff0000000091150"); //  6279093
    addPowData("3ff0010403f34767", EXPONENT, "3ff0000021472146"); //  6428736
    addPowData("3ff00496fe59bc98", EXPONENT, "3ff000009650a4ca"); //  6432355,6493373
    addPowData("3ff0012e43815868", EXPONENT, "3ff0000026af266e"); //  6555029
    addPowData("3ff00021f6080e3c", EXPONENT, "3ff000000458d16a"); //  7092933
    addPowData("3ff000489c0f28bd", EXPONENT, "3ff00000094b3072"); //  7112412
    addPowData("3ff00009d3df2e9c", EXPONENT, "3ff00000014207b4"); //  7675535
    addPowData("3ff000def05fa9c8", EXPONENT, "3ff000001c887cdc"); //  7860324
    addPowData("3ff0013bca543227", EXPONENT, "3ff00000286a42d2"); //  8292427
    addPowData("3ff0021a2f14a0ee", EXPONENT, "3ff0000044deb040"); //  8517311
    addPowData("3ff0002cc166be3c", EXPONENT, "3ff0000005ba841e"); //  8763101
    addPowData("3ff0000cc84e613f", EXPONENT, "3ff0000001a2da46"); //  9269124
    addPowData("3ff000057b83c83f", EXPONENT, "3ff0000000b3a640"); //  9631452
    // init main-net pow data end
    // add pow data
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L67-69)
```java
      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```
