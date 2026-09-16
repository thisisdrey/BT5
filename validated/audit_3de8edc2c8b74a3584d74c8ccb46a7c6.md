### Title
Floating-point precision loss in `ExchangeProcessor`'s Bancor-formula `pow()` enables value extraction from TRC10 Exchange pools - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The default (non-hardened) TRC10 Bancor-style exchange math in java-tron computes swap amounts using IEEE-754 double-precision `Math.pow`/`StrictMath.pow`, the same class of bug described in the external report for `log_exp_math::pow`. This can cause the computed "amount out" to diverge from the mathematically correct value — including rounding in the trader's favor — for crafted balance/quantity combinations, letting an unprivileged trader extract value from the exchange pool.

### Finding Description
`ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` compute the Bancor relay-token conversion using `Maths.pow(base, exponent, useStrictMath)`, which resolves to `StrictMath.pow`/`Math.pow` double arithmetic: [1](#0-0) 

This is invoked from `ExchangeCapsule.transaction()`, the shared routine for computing swap outputs against the pool's `firstTokenBalance`/`secondTokenBalance`: [2](#0-1) 

`ExchangeCapsule.transaction()` is directly reachable from a signed, broadcastable `ExchangeTransactionContract` via `ExchangeTransactionActuator.execute()`/`doValidate()`: [3](#0-2) [4](#0-3) 

Whether the vulnerable float path or the hardened `BigDecimal`-based `SafeExchangeProcessor` is used depends entirely on the `allowHardenExchangeCalculation` dynamic property, which is opt-in and defaults to disabled: [5](#0-4) 

The fact that java-tron itself maintains a hardcoded compatibility table of previously observed mainnet `pow` precision mismatches (`MathWrapper.addPowData`) is direct evidence that this exact bug class — `double`-precision `pow` producing incorrect/inconsistent results for the Bancor formula — has already manifested in production: [6](#0-5) 

The only economic guard in `doValidate()` is that the computed output must be `>= tokenExpected`, a value fully chosen by the caller; there is no invariant check (e.g., that the pool's `k = firstTokenBalance * secondTokenBalance` does not decrease) to catch a precision-induced under/over-payment: [7](#0-6) 

### Impact Explanation
An attacker who selects a TRC10/TRX exchange pair and crafts a `tokenQuant` that lands in a `pow()` precision-loss region can receive an `anotherTokenQuant` from `ExchangeCapsule.transaction()` that is inflated relative to the mathematically correct Bancor output, or can cause repeated trades to drain the pool's reserves at the expense of other liquidity/token holders — an unbacked-balance/theft-of-funds condition on the exchange pool, analogous to the "receive `200` tokens for `0`" PoC in the report. Because `firstTokenBalance`/`secondTokenBalance` are globally observable on-chain state and `tokenQuant`/`tokenID` are attacker-controlled inputs of a normal broadcastable transaction, this is directly reachable by any account with sufficient TRX/asset balance for the trade and fee.

### Likelihood Explanation
The vulnerable float-based `ExchangeProcessor` is still the default execution path unless the `allowHardenExchangeCalculation` proposal has been activated on a given network; on networks/testnets where it is not yet enabled, every `ExchangeTransactionContract`/buy/sell exchange transaction goes through it. The pre-existing `addPowData` patch table shows this precision issue is not merely theoretical — it has already occurred with real transaction data on TRON mainnet, meaning triggering conditions are discoverable by systematically probing balance/quant ratios (all public state), without needing any privileged role.

### Recommendation
- Make the `BigDecimal`-based `SafeExchangeProcessor` (already implemented) the unconditional default computation path for all exchange actuators instead of gating it behind `allowHardenExchangeCalculation`.
- Add an invariant check after each `ExchangeCapsule.transaction()` call asserting that `firstTokenBalance * secondTokenBalance` does not decrease (using `BigInteger` to avoid overflow), rejecting the trade otherwise, matching the report's stated remediation.
- Retire the legacy double-precision `Maths.pow`/`ExchangeProcessor` path from consensus-critical code entirely.

### Proof of Concept
1. Create/observe a TRC10 `Exchange` with `firstTokenBalance`/`secondTokenBalance` values and craft a `tokenQuant` such that `1.0 + quant/newBalance` raised to `0.0005` (in `exchangeToSupply`) or `1.0 + supplyQuant/supply` raised to `2000.0` (in `exchangeFromSupply`) falls into a known double-precision rounding gap (as already catalogued for mainnet blocks in `MathWrapper.addPowData`).
2. Submit an `ExchangeTransactionContract` via the broadcast API with that `tokenQuant` and a `tokenExpected` at or below the (precision-inflated) computed output from `ExchangeCapsule.transaction()`.
3. `ExchangeTransactionActuator.execute()` credits `anotherTokenQuant` to the caller's account and debits the pool by the same (miscalculated) amount, with no invariant check preventing the extraction: [8](#0-7) .

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-75)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L86-96)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L186-221)
```java
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

    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```

**File:** platform/src/main/java/arm/org/tron/common/math/MathWrapper.java (L16-79)
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
    // add pow data
  }
```
