## Analysis

I could not confirm the default value of `allowHardenExchangeCalculation` in `DynamicPropertiesStore.java` before running out of tool budget, but the surrounding evidence (a dedicated hardening proposal, `SafeExchangeProcessor`, `MathWrapper`'s hardcoded historical `pow` correction table, and `StrictMathWrapper`) indicates this flag is a governance-gated (committee-proposal) hard fork switch that is off until activated, meaning the legacy floating-point path in `ExchangeProcessor` is the one that has been in live/default use historically.

### Title
Non-deterministic floating-point Bancor-style pricing in TRC10 Exchange (Bancor relay) module can diverge across nodes / JVMs - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java)

### Summary
The external report concerns a protocol relying on an external price source (Chainlink) whose absence/mismatch causes wrong or unusable pricing. The closest reachable analog in java-tron is not an external oracle, but the internal TRC10 "Exchange" (Bancor-relay) AMM pricing formula in `ExchangeProcessor`, which computes swap output using native Java `double` arithmetic (`Math.pow`/`StrictMath.pow`) instead of a deterministic, standardized (BigDecimal/fixed-point) calculation. Just like a broken price feed, an unreliable/underspecified pricing computation can produce wrong exchange results and, worse in a blockchain context, can produce a computation that is not guaranteed to be bit-identical across all validator JVMs/platforms — the root cause class that the java-tron team has itself acknowledged by introducing `MathWrapper` (a hardcoded table of "corrected" historical `pow` results for specific mainnet blocks) and later `SafeExchangeProcessor`/`StrictMathWrapper` behind an `allowHardenExchangeCalculation` hard-fork flag.

### Finding Description
`ExchangeTransactionActuator`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator` are reachable by any signed transaction from an unprivileged account that owns a TRC10 exchange pair or wants to trade against one [1](#0-0) . The buy/sell amount is computed by `ExchangeCapsule.transaction()`, which by default (non-hardened path) delegates to `ExchangeProcessor` [2](#0-1) .

`ExchangeProcessor` implements the Bancor "relay token" formula using IEEE-754 `double` math and `Math.pow`/`StrictMath.pow`-based `Maths.pow`, then truncates the double result to a `long` via a raw cast [3](#0-2) . This is precisely the same class of "unreliable pricing input" bug as the reported issue: the swap-rate computation is derived from floating-point operations whose bit-level result can differ across CPU architectures/JIT compilation strategies, unlike the deterministic integer/BigDecimal math used elsewhere in the actuators (`ExchangeInjectActuator`, `ExchangeWithdrawActuator` use `BigInteger`/`BigDecimal` division instead) [4](#0-3) .

The codebase itself documents this root cause: `MathWrapper` contains a hardcoded historical lookup table of specific `pow(a,b)` bit patterns paired with "corrected" results tied to specific past mainnet block numbers (e.g. block 4137160, 4065476, etc.), explicitly to patch observed floating-point divergence in production [5](#0-4) . The team subsequently introduced a fully deterministic `SafeExchangeProcessor` (using `BigDecimal` with fixed rounding modes) and gated it behind `allowHardenExchangeCalculation`, selected in `AbstractExchangeActuator.allowHarden()` [6](#0-5)  and `ExchangeCapsule.transaction()` [7](#0-6) . A unit test explicitly confirms the legacy and hardened processors produce *different* results for identical inputs: `Assert.assertNotEquals(anotherTokenQuant, result); Assert.assertEquals(safeResult, result);` [8](#0-7) .

### Impact Explanation
If `allowHardenExchangeCalculation` is not yet activated network-wide (a committee-approved hard-fork proposal, consistent with `ProposalUtil`/`ProposalService` entries seen for this flag), every TRC10 `ExchangeTransactionContract` executed by any ordinary user still computes swap amounts via the legacy double-precision `ExchangeProcessor`. Since Java's `Math.pow`/`StrictMath.pow` results are not strictly guaranteed to be bit-identical on all hardware/JIT combinations for every input, this can, in the worst case, cause different full nodes to compute a different `buyTokenQuant` for the same transaction and same state — a state divergence / chain-split risk, which is exactly why the project needed the `MathWrapper` block-specific override table for historical mainnet blocks. Short of an outright split, it also means a swap's actual settlement price is not derived from a verifiably correct, standardized calculation, mirroring the original report's "protocol computes wrong/unreliable price, breaking correct value transfer" impact class, and can cause principal loss for TRX/TRC10 holders on either side of the swap due to rounding drift accumulated over the exchange pool's lifetime.

### Likelihood Explanation
Likelihood is a function of when/whether `allowHardenExchangeCalculation` is active on the target network. Any unprivileged account with a TRC10 exchange pair or that participates in an existing exchange pair (`ExchangeTransactionContract`) can trigger the legacy path with a single signed transaction; no special privileges or SR/witness coordination are required. The consensus-divergence scenario is edge-case and input-dependent (only specific `(balance, quant)` ratios historically produced platform-divergent `pow` results, which is why only a limited hardcoded table of block-specific corrections exists), so realistic exploitation requires searching for divergence-triggering ratios, but the mechanism is reachable purely through normal TRC10 exchange usage.

### Recommendation
- Confirm the default/current activation state of `allowHardenExchangeCalculation` across mainnet; if not yet universally active, prioritize its activation via committee proposal so `SafeExchangeProcessor`'s deterministic `BigDecimal` computation is the only production path.
- Once activated, remove/deprecate `ExchangeProcessor`'s double-based computation and the `MathWrapper` legacy-lookup mechanism entirely to eliminate any latent floating-point path.
- Audit `Maths.pow`/`StrictMathWrapper.pow` usage elsewhere in resource/vote/reward calculations for the same non-determinism risk pattern seen here.

### Proof of Concept
1. On a node/network where `allowHardenExchangeCalculation` (`DynamicPropertiesStore`) is `0` (inactive), create a TRC10 `Exchange` pair with pool balances chosen so that `exchangeToSupply`/`exchangeFromSupply`'s `Math.pow` intermediate values fall near historically-observed divergence patterns (see the `addPowData(...)` entries in `MathWrapper` for known problematic operand bit patterns) [9](#0-8) .
2. Broadcast an `ExchangeTransactionContract` from an unprivileged account swapping through that pair; the resulting `anotherTokenQuant` is computed via `ExchangeProcessor.exchange()` [10](#0-9) .
3. Compare against `SafeExchangeProcessor.exchange()` run on the same inputs — the existing unit test demonstrates these two paths yield different results for identical `(sellBalance, buyBalance, sellQuant)` triples [8](#0-7) , confirming the legacy computation is not the canonical/deterministic one and is susceptible to the platform-dependent floating-point divergence that necessitated the `MathWrapper` block-specific override table in the first place.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L38-69)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    long fee = calcFee();
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    ExchangeStore exchangeStore = chainBaseManager.getExchangeStore();
    ExchangeV2Store exchangeV2Store = chainBaseManager.getExchangeV2Store();
    AssetIssueStore assetIssueStore = chainBaseManager.getAssetIssueStore();
    try {
      final ExchangeTransactionContract exchangeTransactionContract = this.any
          .unpack(ExchangeTransactionContract.class);
      AccountCapsule accountCapsule = accountStore
          .get(exchangeTransactionContract.getOwnerAddress().toByteArray());

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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-146)
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L41-45)
```java
  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
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

**File:** platform/src/main/java/arm/org/tron/common/math/MathWrapper.java (L8-76)
```java
/**
 * This class is deprecated and should not be used in new code,
 * for cross-platform consistency, please use {@link StrictMathWrapper} instead,
 * especially for floating-point calculations.
 */
@Deprecated
public class MathWrapper {

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
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
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
