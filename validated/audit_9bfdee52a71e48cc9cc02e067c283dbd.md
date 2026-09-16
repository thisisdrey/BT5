### Title
Exchange pools mix TRC10 token balances with different `precision` values without decimal normalization, causing mispriced bancor calculations - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java])

### Summary
The reported JOJO bug is a decimal-mismatch class: amounts of assets with different token decimals (e.g., collateral vs. JUSD) are combined arithmetically without normalizing to a common scale, corrupting collateral/borrow accounting. The same bug class is reachable in java-tron's TRC10 Exchange (bancor-relay AMM) subsystem: `firstTokenBalance`/`secondTokenBalance` for two different TRC10 tokens (each with an independently configurable `precision` field, 0–6) are stored and combined in raw integer units with no reference to each token's `precision`, so the on-chain "price" computed by `ExchangeProcessor`/`SafeExchangeProcessor` is only correct when both sides happen to share the same precision.

### Finding Description
`AssetIssueContract.precision` is a per-token field (0–6) validated only for range in `AssetIssueActuator`: [1](#0-0) 

Different TRC10 tokens can therefore legitimately have different decimal precisions (e.g., token A precision 0, token B precision 6), as exercised in tests: [2](#0-1) 

`ExchangeCreateActuator` creates a two-token exchange pool by taking `first_token_balance`/`second_token_balance` directly from the transaction and storing them as raw `long` balances via `ExchangeCapsule.setBalance`, with no reference anywhere to either token's `precision`: [3](#0-2) 

Subsequent trades run through `ExchangeCapsule.transaction`, which calls the bancor-relay `ExchangeProcessor`/`SafeExchangeProcessor`, again purely on raw balances with no decimal normalization: [4](#0-3) [5](#0-4) 

`ExchangeInjectActuator` and `ExchangeWithdrawActuator` likewise perform ratio math (`anotherTokenQuant = secondTokenBalance * tokenQuant / firstTokenBalance`) directly on the two token quantities regardless of each token's decimal precision: [6](#0-5) [7](#0-6) 

A grep for `precision` across the codebase confirms it is never referenced in any of `ExchangeCreateActuator`, `ExchangeInjectActuator`, `ExchangeTransactionActuator`, `ExchangeWithdrawActuator`, `ExchangeCapsule`, or `ExchangeProcessor` — precision is stored purely as informational metadata for the token, not fed into the AMM's exchange-rate math. This is exactly the JUSDBank pattern: raw integer amounts of assets with differing decimal scales are combined in the same formula (deposit/borrow ledgers there; bancor-relay pool balances here) as if they were denominated identically.

### Impact Explanation
Because the pool's implied exchange rate is a pure function of the two raw integer balances (with no decimal correction), any pair of TRC10 tokens issued with different `precision` values will have a bancor "price" that is off by a factor of `10^(precisionA - precisionB)` relative to the tokens' true (decimal-adjusted) value. Anyone who creates an exchange (`ExchangeCreateContract`), injects liquidity (`ExchangeInjectContract`), trades (`ExchangeTransactionContract`), or withdraws (`ExchangeWithdrawContract`) is interacting with this mispriced pool from a single broadcast transaction. An attacker can create a pool between a low-precision and a high-precision TRC10 token, then trade against it (or against liquidity providers who assume decimal-correct pricing) to drain the more valuable token from the pool at a favorable raw-unit rate — an unbacked-balance/theft-of-funds outcome for anyone who deposits liquidity assuming the standard decimal-adjusted exchange-rate semantics.

### Likelihood Explanation
`precision` differences between TRC10 tokens are a normal, expected, user-controlled configuration (any account can issue an asset with any precision 0–6 via `AssetIssueContract`), and creating/trading an Exchange pool is a permissionless, single-transaction operation available to any account with the small creation fee and token balance. No special privilege, validator collusion, or multi-step exploit chain is required — an ordinary asset issuer/order-placer can trigger the mispricing simply by pairing tokens of different precision in one `ExchangeCreateContract`.

### Recommendation
When computing bancor relay math and validating token-quant ratios in `ExchangeCreateActuator`, `ExchangeInjectActuator`, `ExchangeTransactionActuator`, `ExchangeWithdrawActuator`, and `ExchangeCapsule`/`ExchangeProcessor`, normalize both token balances/quantities to a common decimal base (e.g., scale each amount by `10^(maxPrecision - tokenPrecision)`) using each token's `AssetIssueCapsule.getPrecision()` (and treating TRX as its fixed 6-decimal `precision`) before performing any addition, multiplication, or ratio comparison, then de-normalize the result before persisting/returning it.

### Proof of Concept
1. Issue TRC10 token `A` with `precision = 0` and TRC10 token `B` with `precision = 6` via `AssetIssueContract` (both pass validation in `AssetIssueActuator.doValidate`, lines 176-181, since each individually satisfies `0 <= precision <= 6`).
2. Call `ExchangeCreateContract` with `first_token_id = A`, `first_token_balance = 1_000_000`, `second_token_id = B`, `second_token_balance = 1_000_000` — accepted by `ExchangeCreateActuator.doValidate`/`execute` with no precision check (lines 55-90, 197-228).
3. Because `B` has 6 decimals and `A` has 0 decimals, `1_000_000` units of `A` represents 1,000,000 whole tokens of A but `1_000_000` units of `B` represents only 1 whole token of B — yet the pool is created as if they are of equal value.
4. Trade via `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract`; the resulting `anotherTokenQuant` computed in `ExchangeCapsule.transaction` (lines 124-158) and `ExchangeProcessor.exchange` (lines 41-45) reflects the mispriced 1:1-ish raw-unit rate rather than the true 1,000,000:1 decimal-adjusted rate, letting the trader extract far more of token `B` (or `A`) than the true economic exchange rate would allow.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L176-181)
```java
    int precision = assetIssueContract.getPrecision();
    if (precision != 0
        && dynamicStore.getAllowSameTokenName() != 0
        && (precision < 0 || precision > ActuatorConstant.PRECISION_DECIMAL)) {
      throw new ContractValidateException("precision cannot exceed 6");
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/AssetIssueActuatorTest.java (L93-98)
```java
            .setName(ByteString.copyFromUtf8(NAME)).setTotalSupply(TOTAL_SUPPLY).setTrxNum(TRX_NUM)
            .setNum(NUM)
            .setStartTime(nowTime).setEndTime(nowTime + 24 * 3600 * 1000)
            .setDescription(ByteString.copyFromUtf8(DESCRIPTION))
            .setUrl(ByteString.copyFromUtf8(URL)).setPrecision(6)
            .build());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L55-90)
```java
      byte[] firstTokenID = exchangeCreateContract.getFirstTokenId().toByteArray();
      byte[] secondTokenID = exchangeCreateContract.getSecondTokenId().toByteArray();
      long firstTokenBalance = exchangeCreateContract.getFirstTokenBalance();
      long secondTokenBalance = exchangeCreateContract.getSecondTokenBalance();

      long newBalance = subtractExact(accountCapsule.getBalance(), fee);

      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, firstTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(firstTokenID, firstTokenBalance, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, secondTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(secondTokenID, secondTokenBalance, dynamicStore, assetIssueStore);
      }

      long id = addExact(dynamicStore.getLatestExchangeNum(), 1);
      long now = dynamicStore.getLatestBlockHeaderTimestamp();
      if (dynamicStore.getAllowSameTokenName() == 0) {
        //save to old asset store
        ExchangeCapsule exchangeCapsule =
            new ExchangeCapsule(
                exchangeCreateContract.getOwnerAddress(),
                id,
                now,
                firstTokenID,
                secondTokenID
            );
        exchangeCapsule.setBalance(firstTokenBalance, secondTokenBalance);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-158)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L41-45)
```java
  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-227)
```java
    BigDecimal bigFirstTokenBalance = new BigDecimal(String.valueOf(firstTokenBalance));
    BigDecimal bigSecondTokenBalance = new BigDecimal(String.valueOf(secondTokenBalance));
    BigDecimal bigTokenQuant = new BigDecimal(String.valueOf(tokenQuant));
    final boolean allowHarden = allowHarden();
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }
```
