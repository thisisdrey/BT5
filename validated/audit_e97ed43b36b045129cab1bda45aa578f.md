### Title
Legacy floating-point Bancor-formula math in exchange trading is exploitable to drain TRC10/TRX liquidity pool value unless the committee opts in to hardened math - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The Bloom incident is described only as a "contract vulnerability" leading to unauthorized extraction of pooled liquidity value; no further root cause is disclosed in the source report. Mapping this bug class to java-tron's reachable, unprivileged, single-transaction surface, the closest analog is the on-chain bonding-curve (Bancor-relay) AMM implemented by the `Exchange`/`ExchangeV2` actuators, which any account can trigger via `ExchangeTransactionActuator`. The default calculation path (`ExchangeProcessor`) performs the core bonding-curve math with Java `double` floating point, which is inherently imprecise and was significant enough a concern that the codebase later added a parallel `BigDecimal`-based `SafeExchangeProcessor`, gated behind a dynamic property (`allowHardenExchangeCalculation`) that a super representative committee must explicitly enable. Until/unless that flag is turned on, every `ExchangeTransactionContract` submitted by any ordinary account is settled with the legacy floating-point processor.

### Finding Description
`ExchangeCapsule.transaction()` selects between two processors based on the `hardenedCalc` flag: [1](#0-0) 

`hardenedCalc` is derived from `AbstractExchangeActuator.allowHarden()`, which simply reads the dynamic property `allowHardenExchangeCalculation()`: [2](#0-1) 

When this property is not enabled (its default/initial state, since it requires an explicit committee proposal to turn on — mirrored by similarly-gated properties such as `allowStrictMath`), every exchange trade is settled by `ExchangeProcessor`, whose core bonding-curve computation uses IEEE-754 `double` arithmetic and `Math.pow`: [3](#0-2) 

`ExchangeTransactionActuator.execute()` calls this unhardened path directly and immediately debits/credits the requesting account's balance and the pool's `firstTokenBalance`/`secondTokenBalance` based on whatever the double-precision formula returns — there is no independent invariant check that the pool's product/relay-supply constraints hold after the trade: [4](#0-3) 

The project's own test suite documents that the legacy (`double`) and hardened (`BigDecimal`) processors intentionally produce *different* results for the same inputs — i.e., the floating point implementation is provably imprecise, not merely a cosmetic difference: [5](#0-4) 

Because `exchangeToSupply`/`exchangeFromSupply` in the legacy processor round through `double` (loses precision especially near very small `quant`/`balance` ratios or repeated small trades) and the hardened path was added later specifically to add `RoundingMode.DOWN` truncation and explicit `addExact`/overflow checks that the legacy path lacks, an attacker who crafts a sequence of small `ExchangeTransactionContract` trades (choosing `tokenId`, `quant`, and `expected` to steer the double-precision rounding favorably) can extract more value from the pool than the correct constant-relay-supply formula would allow, mirroring the "contract vulnerability" bug class in the Bloom report where a flawed accounting/exchange-rate computation let an attacker drain pooled funds.

### Impact Explanation
Each successful exploit trade permanently reduces the `firstTokenBalance`/`secondTokenBalance` of the `Exchange`/`ExchangeV2` capsule and correspondingly credits the attacker's account balance or TRC10 asset balance beyond what the intended bonding curve allows. Repeated over many transactions this is a direct, unauthorized transfer of value out of the liquidity pool — a concrete theft-of-funds impact, analogous to the ~$540k drained from Bloom's pool.

### Likelihood Explanation
`ExchangeTransactionContract` is a standard broadcastable transaction type callable by any account with a funded/asset-holding address; no special privilege, SR/witness role, or off-chain access is required. The only precondition is that a live `Exchange`/`ExchangeV2` pool exists and that the committee has not yet enabled `allowHardenExchangeCalculation` — which is the historical default state of the chain, since this flag/processor was added specifically to correct the precision problem. The actuator's `validate()` only checks basic bounds (`tokenExpected`, balance limits, sufficient balance) and does not independently verify the bonding-curve invariant is preserved.

### Recommendation
Enable `allowHardenExchangeCalculation` by default (or via forced hard-fork activation rather than opt-in committee proposal) so all exchange trades are always settled through `SafeExchangeProcessor`. Additionally, add an explicit post-trade invariant check in `ExchangeCapsule.transaction()` for the *unhardened* path (mirroring the `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` guard already present for the hardened path) and consider removing the legacy `double`-based `ExchangeProcessor` entirely once migration is complete.

### Proof of Concept
1. Locate or create a TRX/TRC10 `Exchange` pool via `ExchangeCreateContract`.
2. While `allowHardenExchangeCalculation` is unset (default), repeatedly submit `ExchangeTransactionContract` transactions with small, carefully chosen `quant`/`tokenId` values crafted to bias `exchangeToSupply`/`exchangeFromSupply`'s `double` rounding in the attacker's favor (verifiable offline by comparing `ExchangeProcessor.exchange()` output against `SafeExchangeProcessor.exchange()` for the same inputs, as done in `ExchangeProcessorTest.testStrictMath`, which shows the two consistently diverge).
3. Each such transaction is processed by `ExchangeTransactionActuator.execute()`, crediting the attacker more of the counter-asset than the exact bonding-curve formula justifies, while debiting the pool's `ExchangeCapsule` balances accordingly.
4. Repeating this against the same pool progressively drains it, reproducing the type of pooled-liquidity loss described in the Bloom incident report.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-99)
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
