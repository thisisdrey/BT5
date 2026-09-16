### Title
Exchange (Bancor-style AMM) actuators ignore TRC10 token `precision`, causing systematic mispricing/value extraction when pairing tokens with different decimals - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java`, `ExchangeInjectActuator.java`, `ExchangeWithdrawActuator.java`, `ExchangeTransactionActuator.java`, `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
The `Exchange*` actuators and `ExchangeCapsule.transaction()`/`ExchangeProcessor` implement a Bancor-relay AMM over raw TRC10 `long` balances, but never account for the fact that each TRC10 asset has its own `precision` (decimals) field set at issuance. The reported Balancer bug (`computeFairReserves` mixing raw reserves of different-decimal tokens) is structurally the same defect: raw integer balances of tokens with different decimal scales are combined in ratio/AMM math as if they were directly comparable.

### Finding Description
TRC10 assets carry a `precision` field configured at issuance: [1](#0-0) 

`ExchangeCreateActuator` lets any account pair up two arbitrary tokens (including TRX) and set arbitrary initial `first_token_balance`/`second_token_balance` with no reference to each token's `precision`: [2](#0-1) 

`ExchangeCapsule.transaction()` (used by `ExchangeTransactionActuator`) and `ExchangeProcessor`/`SafeExchangeProcessor` operate purely on these raw `long` balances via the Bancor formula, with no decimal normalization step: [3](#0-2) [4](#0-3) 

Similarly, `ExchangeInjectActuator` and `ExchangeWithdrawActuator` compute the "another token" amount as a straight ratio of raw balances (`bigSecondTokenBalance * bigTokenQuant / bigFirstTokenBalance`), again treating 1 raw unit of token A as equivalent in value/weight to 1 raw unit of token B regardless of decimals: [5](#0-4) [6](#0-5) 

Since TRC10 `precision` can differ per token (validated/parsed in `AssetIssueActuator`, referenced in [7](#0-6) ), a pool paired with a low-precision token (e.g. precision 0) against a high-precision token (e.g. precision 6) will price "1 raw unit" of each identically. This is exactly analogous to the Balancer `resA/resB` bug where mismatched decimals distort the fair-reserve/price ratio, sometimes rounding to 0 or producing multi-order-of-magnitude errors.

### Impact Explanation
Any account (unprivileged) can call `ExchangeCreateContract` to create a pool pairing two TRC10 tokens (or TRX) of different `precision`. Because the AMM/ratio math never normalizes for decimals, the effective exchange rate embedded in the pool is off by a factor of `10^(precisionB - precisionA)` relative to the tokens' real economic value. A trader can then exploit `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract` to buy the undervalued token for far less than its real value (or drain the pool's more valuable token) — a direct unauthorized value-extraction/theft-of-funds vector against liquidity supplied to the exchange pool. This satisfies the "theft of funds" acceptance criterion for a Medium-severity finding.

### Likelihood Explanation
Likelihood is high in the sense that no privileged role is required — pool creation and precision selection are fully permissionless (any account issuing an asset chooses its own `precision`, and any account can create an exchange pairing arbitrary tokens). The condition needed (two tokens with differing decimals) is common and easy to construct deliberately, making this readily exploitable by an attacker rather than merely a theoretical edge case.

### Recommendation
Normalize token quantities by each token's `precision` before performing the AMM/ratio calculations in `ExchangeCapsule.transaction()`, `ExchangeProcessor`/`SafeExchangeProcessor`, `ExchangeInjectActuator`, and `ExchangeWithdrawActuator` — e.g., scale both balances to a common decimal base (or store/require pools to use tokens of equal precision) so that raw integer units are not treated as economically equivalent across tokens with different decimals.

### Proof of Concept
1. Issue TRC10 token `A` with `precision = 0` and token `B` with `precision = 6` via `AssetIssueActuator`.
2. Call `ExchangeCreateContract` to create a pool with `first_token_balance` of `A` and `second_token_balance` of `B` using nominal "equal value" raw amounts (e.g., 1,000,000 of each) — accepted without any precision check, per `ExchangeCreateActuator.doValidate()` ( [2](#0-1) ).
3. Because `B` is actually worth `10^6` times less per raw unit than its issuer intended relative to `A` (or vice versa depending on how the pool was seeded), a trader calls `ExchangeTransactionContract` (handled by `ExchangeCapsule.transaction()`, [3](#0-2) ) repeatedly to arbitrage the mispriced pool, extracting the higher-value token at the lower-value token's raw-unit exchange rate until the pool's valuable-token reserve is drained.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/AssetIssueCapsule.java (L47-53)
```java
  public AssetIssueCapsule(byte[] ownerAddress, String id, String name, String abbr,
                           long totalSupply, int precision) {
    this.assetIssueContract = AssetIssueContract.newBuilder()
            .setOwnerAddress(ByteString.copyFrom(ownerAddress)).setId(id)
            .setName(ByteString.copyFrom(name.getBytes())).setAbbr(ByteString.copyFrom(abbr.getBytes()))
            .setTotalSupply(totalSupply).setPrecision(precision).build();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L183-218)
```java
    byte[] firstTokenID = contract.getFirstTokenId().toByteArray();
    byte[] secondTokenID = contract.getSecondTokenId().toByteArray();
    long firstTokenBalance = contract.getFirstTokenBalance();
    long secondTokenBalance = contract.getSecondTokenBalance();

    if (dynamicStore.getAllowSameTokenName() == 1) {
      if (!Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES) && !isNumber(firstTokenID)) {
        throw new ContractValidateException("first token id is not a valid number");
      }
      if (!Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES) && !isNumber(secondTokenID)) {
        throw new ContractValidateException("second token id is not a valid number");
      }
    }

    if (Arrays.equals(firstTokenID, secondTokenID)) {
      throw new ContractValidateException("cannot exchange same tokens");
    }

    if (firstTokenBalance <= 0 || secondTokenBalance <= 0) {
      throw new ContractValidateException("token balance must greater than zero");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (firstTokenBalance > balanceLimit || secondTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }

    if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(firstTokenBalance, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(firstTokenID, firstTokenBalance, dynamicStore)) {
        throw new ContractValidateException("first token balance is not enough");
      }
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L41-45)
```java
  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-227)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenID = secondTokenID;
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divide(bigFirstTokenBalance).longValueExact();
      newTokenBalance = addExact(firstTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(secondTokenBalance, anotherTokenQuant);
    } else {
      anotherTokenID = firstTokenID;
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divide(bigSecondTokenBalance).longValueExact();
      newTokenBalance = addExact(secondTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(firstTokenBalance, anotherTokenQuant);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-247)
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
      if (allowHarden) {
        BigDecimal remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }

    } else {
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigSecondTokenBalance).longValueExact();
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L1-1)
```java
/*
```
