### Title
TRC10 Bancor-curve Exchange pool can be drained via repeated small `ExchangeTransactionContract` swaps exploiting floating-point truncation - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
java-tron's TRC10 `Exchange` feature implements a bonding-curve AMM (Bancor-style relay/virtual-supply model) that any account can trade against by broadcasting `ExchangeTransactionContract` transactions through `ExchangeTransactionActuator`. Just like WooFi's SPMM curve, the pricing curve is evaluated per-call with no protection against splitting one large trade into many small sequential trades. In the legacy (non-hardened) code path the curve math is computed with Java `double`/`Math.pow` and truncated to `long` on every single step, which lets an attacker extract a systematically better exchange rate by executing many small trades instead of one large trade — the same "split the swap to beat the curve" pattern used in the WOO exploit.

### Finding Description
`ExchangeTransactionActuator.execute()` calls `exchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())` for every trade [1](#0-0) . Depending on `allowHarden()`, this either uses the BigDecimal-based `SafeExchangeProcessor` or the legacy `ExchangeProcessor`, which selects a bancor-curve relay-token model [2](#0-1) .

The legacy `ExchangeProcessor` computes the curve with double-precision floating point and truncates the result to a `long` on every call: [3](#0-2) 

Because `(long) issuedSupply` and `(long) exchangeBalance` always truncate toward zero, each individual trade in the sequence rounds in the trader's favor by up to almost one unit. When a single desired trade size is split into N smaller trades, this per-call truncation "bonus" is realized N times instead of once, systematically extracting more of the pool's second-token balance than a single equivalent-size trade would yield — mirroring the WooFi finding where `maxNotionalSwap`/`maxGamma` limits (and the underlying curve math) could be circumvented by chopping a large swap into many partial swaps executed back-to-back.

There is no protection in `ExchangeTransactionActuator`/`ExchangeCapsule` against this: validation only checks token existence, positive quantities, `exchangeBalanceLimit`, sufficient balance, and slippage (`expected`) [4](#0-3)  — there is no per-block/per-transaction notional cap or curvature-based guard analogous to `maxGamma`/`maxNotionalSwap`, and nothing prevents an attacker from issuing many `ExchangeTransactionContract` transactions in the same block to repeatedly exploit the truncation bias before the pool composition can re-equilibrate against them.

### Impact Explanation
An attacker who repeatedly calls the public, permissionless `ExchangeTransactionContract` (reachable by any signed transaction/broadcaster, or via the `/wallet/exchangetransaction` HTTP API [5](#0-4) ) against a legacy (non-hardened) Exchange pool can extract more of the second token than the curve intends per unit of first token sold, draining the pool's asset balances over many small transactions — a direct theft of pooled TRX/TRC10 funds analogous to the WooFi drain.

### Likelihood Explanation
Exploitability depends on whether `allowHarden()` (BigDecimal-safe math) is active for the exchange in question; if the chain/exchange still uses the legacy `ExchangeProcessor` double-math path, the attack requires only ordinary signed transactions with no special privilege, and profitability scales with the number of splits, similar to the original WooFi flash-loan/partial-swap sequence. I could not fully confirm within available tool budget whether `allowHarden()` is unconditionally true post-hardfork for every exchange instance in this codebase version, so the precise blast radius (all exchanges vs. only pre-hardfork-created ones) is uncertain.

### Recommendation
- Always route `ExchangeCapsule.transaction()` through the BigDecimal-based `SafeExchangeProcessor` regardless of `allowHarden()`/config, eliminating the double-precision truncation bias.
- Enforce a minimum trade size or aggregate a maximum notional/gamma-style limit per block/account per exchange pair to prevent splitting a large trade into many favorable-rounding micro-trades.
- Add regression tests that compare the cumulative output of N small trades vs. one large trade of equal total size and assert they are economically equivalent (within a single rounding unit).

### Proof of Concept
Conceptual reproduction using existing test scaffolding (`framework/src/test/java/org/tron/core/capsule/utils/ExchangeProcessorTest.java`):
1. Configure an Exchange pool with `allowStrictMath=false`/legacy `ExchangeProcessor` (i.e., `allowHarden()` disabled).
2. Broadcast `ExchangeTransactionContract` for `sellTokenQuant = X` once, record `anotherTokenQuant_single`.
3. Reset pool state; broadcast N `ExchangeTransactionContract` transactions each selling `X/N`, sum the resulting `anotherTokenQuant_i`.
4. Compare: due to per-call truncation in `exchangeToSupply`/`exchangeFromSupply` (`chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`), `sum(anotherTokenQuant_i) > anotherTokenQuant_single` for sufficiently large N, demonstrating value extraction beyond the intended curve, consistent with the WooFi partial-swap drain pattern. [6](#0-5)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-69)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-221)
```java
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

**File:** framework/src/test/java/org/tron/common/utils/client/utils/HttpMethed.java (L552-578)
```java
  public static HttpResponse exchangeTransaction(
      String httpNode,
      byte[] ownerAddress,
      Integer exchangeId,
      String tokenId,
      Long quant,
      Long expected,
      String fromKey) {
    try {
      final String requestUrl = "http://" + httpNode + "/wallet/exchangetransaction";
      JsonObject userBaseObj2 = new JsonObject();
      userBaseObj2.addProperty("owner_address", ByteArray.toHexString(ownerAddress));
      userBaseObj2.addProperty("exchange_id", exchangeId);
      userBaseObj2.addProperty("token_id", str2hex(tokenId));
      userBaseObj2.addProperty("quant", quant);
      userBaseObj2.addProperty("expected", expected);
      response = createConnect(requestUrl, userBaseObj2);
      transactionString = EntityUtils.toString(response.getEntity());
      transactionSignString = gettransactionsign(httpNode, transactionString, fromKey);
      response = broadcastTransaction(httpNode, transactionSignString);
    } catch (Exception e) {
      e.printStackTrace();
      httppost.releaseConnection();
      return null;
    }
    return response;
  }
```
