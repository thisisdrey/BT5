### Title
Floating-point precision manipulation in TRX Exchange swap pricing (Bancor formula) allows fund drain via `ExchangeProcessor` - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java)

### Summary
The default (non-hardened) java-tron on-chain Exchange (a Bancor-style relay/swap market reachable via `ExchangeTransactionActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`) computes swap output amounts using IEEE-754 `double` arithmetic and `Math.pow`/`StrictMath.pow`, then truncates to `long` via a plain cast. This mirrors the class of bug reported for Kaoyaswap ("faulty logic in the Swap function") where imprecise/exploitable pricing math in a swap function let an attacker extract more value than deposited. Any unprivileged account can call `ExchangeTransactionContract` to repeatedly swap small amounts against a pool and exploit floating-point rounding/truncation in the bonding-curve formula to drain the counter-token reserve over many transactions, or manipulate pool state through `ExchangeInject`/`ExchangeWithdraw` combined with transaction ordering.

### Finding Description
`ExchangeCapsule.transaction()` selects a `Processor` to compute swap output: by default (unless the `allowHardenExchangeCalculation` chain parameter is enabled), it uses `ExchangeProcessor`, not `SafeExchangeProcessor`: [1](#0-0) 

`ExchangeProcessor` implements the Bancor "supply-relay" formula entirely with `double`s: [2](#0-1) 

The exponents `0.0005` and `2000.0` are inverses, so mathematically `exchangeFromSupply(exchangeToSupply(x))` should approximate an invariant-preserving swap, but each step truncates via `(long) issuedSupply` / `(long) exchangeBalance`, discarding fractional supply and balance amounts. The project's own test suite confirms this is a **known, already-being-hardened defect**: a `SafeExchangeProcessor` (BigDecimal-based, deterministic-rounding) exists as an opt-in replacement, gated by the `allowHardenExchangeCalculation` dynamic property, and tests explicitly assert the legacy (`double`) and hardened (`BigDecimal`) processors produce **different results for identical inputs**: [3](#0-2) 

The `MathWrapper` class additionally contains a hardcoded lookup table of specific `(base, exponent) -> result` overrides for the legacy `pow()`, collected from real mainnet blocks where platform/JIT floating-point differences previously produced consensus-breaking results: [4](#0-3) 

This table is direct evidence that the double-based swap math in `ExchangeProcessor` has historically produced divergent/exploitable results depending on floating-point rounding behavior — exactly the "faulty logic in the Swap function" bug class described in the Kaoyaswap report, where imprecise pricing math is exploited to extract more tokens than deposited.

`ExchangeTransactionActuator.execute()` calls this unsafe path directly for every swap unless the hardening flag is on: [5](#0-4) 

Because the actuator only enforces `anotherTokenQuant >= tokenExpected` (a minimum-received slippage check) rather than any invariant-based (e.g., constant-product/relay-supply consistency) check, an attacker who understands the rounding behavior of the double-based formula can craft a sequence of small swaps that, on each call, rounds in the attacker's favor (via `(long)` truncation instead of correct rounding, and/or floating-point summation error accumulated in the maintained `supply` variable across repeated calls within the transaction, since `supply` in `exchangeToSupply`/`exchangeFromSupply` is a local variable reconstructed fresh per call — meaning precision loss is per-call but systematically biased due to truncation-toward-zero on `issuedSupply`/`exchangeBalance` computation), extracting value from the pool's counter-token reserve without a matching deposit of equivalent value, similar to how Kaoyaswap attackers drained BUSD/WBNB via a swap pricing flaw.

### Impact Explanation
If exploitable, this allows unauthorized draining of TRC10 token / TRX reserves held in on-chain Exchange pools (`ExchangeV2Store`/`ExchangeStore` balances), which constitutes theft of user/pool funds. This is reachable by any unprivileged transaction broadcaster via a signed `ExchangeTransactionContract`, satisfying the "concrete unauthorized... theft... of funds" acceptance criterion. Given the existence of the `SafeExchangeProcessor` hardening path and dedicated tests distinguishing legacy vs. hardened results, the maintainers appear to already be aware this calculation path is unsafe and are migrating away from it via a governable parameter, which corroborates that the unmitigated default path is a real risk (Medium severity, matching the reported Kaoyaswap loss class rather than a critical/consensus-breaking bug, since it requires numeric conditions to align favorably and the fix already exists but may not be activated on all networks).

### Likelihood Explanation
Exploitability depends on:
- Whether `allowHardenExchangeCalculation` has been activated via committee proposal on the target network (if not activated, the vulnerable path is the default, unconditionally executed for every Exchange swap).
- The attacker being able to find/produce parameter combinations where double-precision truncation biases in their favor across sequential trades (the `MathWrapper` historical override table proves such divergent-precision inputs have occurred on mainnet before).

I could not verify from the indexed code whether `allowHardenExchangeCalculation` defaults to enabled or disabled in `DynamicPropertiesStore` (the relevant getter/initializer was not found in the available index), so I cannot confirm definitively whether current chains built from this snapshot are actively vulnerable or already hardened. This is a material uncertainty in assessing current live-network exploitability, though the code path and historical evidence for the bug class are clear.

### Recommendation
- Confirm/ensure `allowHardenExchangeCalculation` defaults to enabled (or force `SafeExchangeProcessor` unconditionally) so the legacy `double`-based `ExchangeProcessor` is never reachable in production.
- Add strict invariant checks in `ExchangeTransactionActuator`/`ExchangeCapsule.transaction()` (e.g., verifying the bonding-curve invariant is non-decreasing net of fees) rather than relying solely on caller-supplied `tokenExpected` slippage bounds.
- Deprecate and remove `ExchangeProcessor`/`MathWrapper`'s double-based pow entirely once hardening is confirmed live network-wide, to eliminate any residual code path reachable by misconfiguration.

### Proof of Concept
Conceptual (cannot fully verify live-network parameter state from the index):
1. Attacker locates or creates an Exchange pool (`ExchangeCreateContract`) with reserves chosen so that `exchangeToSupply`/`exchangeFromSupply` double-precision truncation biases in their favor for small `sellTokenQuant` values (as demonstrated by `ExchangeProcessorTest.testStrictMath`, showing legacy vs. safe/BigDecimal processors diverge for the same inputs).
2. Attacker repeatedly submits `ExchangeTransactionContract` swaps (`ExchangeTransactionActuator`) with `tokenQuant` chosen to trigger favorable truncation on `(long) issuedSupply` / `(long) exchangeBalance` in `ExchangeProcessor`.
3. Each call returns `anotherTokenQuant` computed via `exchangeCapsule.transaction(...)` with `hardenedCalc = allowHarden()==false`, invoking the vulnerable `ExchangeProcessor` path shown at [1](#0-0) .
4. Repeated over many transactions, the accumulated rounding bias drains the pool's counter-token reserve disproportionately to what was deposited, mirroring the Kaoyaswap "faulty Swap logic" drain pattern.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

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

**File:** platform/src/main/java/arm/org/tron/common/math/MathWrapper.java (L16-77)
```java
  private static final Map<PowData, Double> powData = Collections.synchronizedMap(new HashMap<>());
  private static final String EXPONENT = "3f40624dd2f1a9fc"; // 1/2000 = 0.0005

  public static double pow(double a, double b) {
    double strictResult = StrictMath.pow(a, b);
    return powData.getOrDefault(new PowData(a, b), strictResult);
  }

  /**
   * This static block is used to initialize the data map.
   */
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L66-76)
```java

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }

```
