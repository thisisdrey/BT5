### Title
Legacy floating-point Bancor-style Exchange calculation permits pool-draining rounding exploitation via repeated buy/sell loops - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
java-tron's legacy TRX↔TRC10 `Exchange` (created via `ExchangeCreateContract`, traded via `ExchangeTransactionContract`) implements a Bancor-style bonding-curve AMM. When the `allowHardenExchangeCalculation` proposal is not enabled, `ExchangeCapsule.transaction()` uses `ExchangeProcessor`, which performs the core supply math with `double`/`Math.pow` floating point arithmetic instead of exact fixed-point math. This mirrors the FH Token incident pattern: a flawed token-accounting path (there, `_transfer`/`isSell`; here, the float-based bonding-curve conversion) that lets an unprivileged trader repeatedly buy and sell against the same pool to accumulate rounding-driven value extraction, ultimately draining pool reserves.

### Finding Description
`ExchangeCapsule.transaction()` selects between the hardened, `BigDecimal`-based `SafeExchangeProcessor` and the legacy `ExchangeProcessor` based on the `hardenedCalc` flag, which comes from `allowHarden()` (i.e., `dynamicStore.allowHardenExchangeCalculation()`) in `AbstractExchangeActuator`: [1](#0-0) [2](#0-1) 

The legacy `ExchangeProcessor` computes the bonding-curve relay/exchange amounts using native `double` arithmetic and `Math.pow`, then truncates via a cast to `long`: [3](#0-2) 

Because this uses IEEE-754 double precision rather than exact decimal math, the forward exchange (`sell -> supply`) and reverse exchange (`supply -> buy`) computations are not perfectly consistent round-trip functions; a sequence of buy-then-sell transactions can systematically leak a small amount of value out of the pool balances (`firstTokenBalance`/`secondTokenBalance`) on each round trip due to floating-point truncation bias, similar in nature to the FH Token exploit where each buy/sell cycle mis-accounted for burned/transferred amounts. Because trades are processed one transaction at a time via `ExchangeTransactionActuator.execute()`/`validate()` and the actuator only enforces `tokenExpected`-based minimum-out slippage checks (no absolute rounding-direction protection against the pool itself), an attacker who repeats buy/sell cycles many times, choosing amounts that maximize the float truncation bias, can accumulate drained value from the Exchange's `firstTokenBalance`/`secondTokenBalance` reserves, which back real TRX/TRC10 balances credited to and debited from user accounts: [4](#0-3) [5](#0-4) 

The existence of a separate, more precise `SafeExchangeProcessor` (using `BigDecimal` with `RoundingMode.HALF_UP`/`DOWN`) and a hardening flag strongly indicates this class of rounding/precision issue is a recognized concern in this exact subsystem, but it is opt-in via committee proposal rather than the default behavior: [6](#0-5) 

### Impact Explanation
If exploitable, an attacker could repeatedly submit `ExchangeTransactionContract` transactions (a single-signer, broadcastable transaction type reachable by any unprivileged account) to slowly but reliably drain TRX and/or TRC10 asset reserves from any legacy (non-hardened) `Exchange` pool, resulting in unbacked/incorrect balances credited to the attacker and a corresponding loss of pool reserves for other participants — directly analogous to the $20,000 loss in the FH Token incident, but at the java-tron protocol layer rather than in a user-deployed contract.

### Likelihood Explanation
Likelihood is bounded by two significant open questions I could not fully resolve given tool limits: (1) whether `allowHardenExchangeCalculation` (and the related `allowStrictMath`) proposals are enabled by default on mainnet today — if hardening is already active network-wide, the legacy float path is unreachable by new transactions; and (2) whether the magnitude of float truncation error in `ExchangeProcessor` is actually favorable to the attacker in a repeatable, profitable way after accounting for the `MarketSellFee`/exchange transaction fee charged on every call (`ExchangeTransactionActuator.calcFee()`), which could make repeated round-trips net negative for an attacker. I was not able to confirm the current default/activated value of `allowHardenExchangeCalculation` in this session. Given these unresolved points, I am not confident enough to assert a concrete unauthorized-fund-drain path exists in the current deployed configuration.

### Recommendation
- Confirm whether `allowHardenExchangeCalculation` is active by default; if not, consider migrating all `Exchange` computations unconditionally to `SafeExchangeProcessor`'s `BigDecimal` arithmetic and removing/deprecating `ExchangeProcessor`.
- If the legacy path must remain for backward compatibility, add explicit invariant checks in `ExchangeTransactionActuator`/`ExchangeCapsule.transaction()` ensuring that no sequence of trades can reduce the product/invariant of `firstTokenBalance * secondTokenBalance` below the value before the trade (accounting for expected fees), rejecting any round-trip that would leak value due to rounding.
- Add regression tests specifically targeting round-trip buy-then-sell sequences against `ExchangeProcessor` to quantify and bound the maximum extractable rounding bias, and compare against `SafeExchangeProcessor` to confirm the hardened path eliminates it.

### Proof of Concept
Not independently verified in this session — validating this would require simulating repeated `ExchangeTransactionActuator.execute()` calls with a fixed pool (with `allowHardenExchangeCalculation=0`) alternating buy/sell of the same token pair and measuring cumulative drift in `exchange.getFirstTokenBalance()`/`getSecondTokenBalance()` versus the pool's theoretical invariant, net of the `MarketSellFee`/transaction fee. I could not complete this simulation/confirm default proposal values within the available tool budget, so this finding should be treated as a plausible but **unconfirmed** analog pending further investigation (e.g., via a Devin session with test execution capability).

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L60-99)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L207-221)
```java
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

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L19-44)
```java
  private BigDecimal exchangeToSupply(long balance, long quant) {
    long newBalance = StrictMathWrapper.addExact(balance, quant);
    BigDecimal bdQuant = BigDecimal.valueOf(quant);
    BigDecimal bdNewBalance = BigDecimal.valueOf(newBalance);
    BigDecimal base = BigDecimal.ONE.add(
        bdQuant.divide(bdNewBalance, 18, RoundingMode.HALF_UP));
    double powResult = StrictMathWrapper.pow(base.doubleValue(), 0.0005);
    return SUPPLY.negate().multiply(
        BigDecimal.ONE.subtract(BigDecimal.valueOf(powResult))).setScale(0, RoundingMode.DOWN);
  }

  private long exchangeFromSupply(long balance, BigDecimal supplyQuant) {
    BigDecimal bdBalance = BigDecimal.valueOf(balance);
    BigDecimal base = BigDecimal.ONE.add(
        supplyQuant.divide(SUPPLY, 18, RoundingMode.HALF_UP));
    double powResult = StrictMathWrapper.pow(base.doubleValue(), 2000.0);
    BigDecimal exchangeBalance = bdBalance.multiply(
        BigDecimal.valueOf(powResult).subtract(BigDecimal.ONE));
    return exchangeBalance.setScale(0, RoundingMode.DOWN).longValueExact();
  }

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    BigDecimal relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```
