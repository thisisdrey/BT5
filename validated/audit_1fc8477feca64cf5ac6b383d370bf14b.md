Confirmed: `ExchangeCreateActuator.doValidate()` has no check comparing the two tokens' `precision` fields at all — it only validates that the token IDs differ, balances are positive and below `getExchangeBalanceLimit()`, and that the owner holds enough balance [1](#0-0) . Any account can invoke `ExchangeCreateContract` to pair two TRC10 tokens (`AssetIssueContract.precision`, 0–6, is per-token and self-declared at issuance) with wildly different `precision` values [2](#0-1) [3](#0-2) .

### Title
Exchange pool AMM math treats all TRC10 token amounts as equal-precision units, enabling drainage of mismatched-precision pools - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java)

### Summary
The `Exchange*` actuators implement a bancor-style constant-product/constant-supply AMM (`ExchangeCapsule.transaction`, `ExchangeProcessor`, `SafeExchangeProcessor`) that operates purely on the raw `long` on-chain integer balances of two arbitrary TRC10 tokens, with no normalization for each token's declared `precision` (analogous to "decimals"). Just like the Cooler contract hardcoding 18 decimals for all ERC20 tokens, java-tron's exchange logic implicitly assumes both paired tokens use the same unit scale.

### Finding Description
`ExchangeCreateActuator.doValidate()` lets any account pair two different TRC10 tokens (or TRX) into a liquidity pool by only checking that IDs differ, balances are positive, below the configured limit, and that the creator has sufficient balance [4](#0-3) . There is no validation that `AssetIssueCapsule.getPrecision()` matches between `firstTokenID` and `secondTokenID`, nor any scaling by `10^precision` when the pool balances are set via `exchangeCapsule.setBalance(firstTokenBalance, secondTokenBalance)` [5](#0-4) .

The price/quantity computation in `ExchangeCapsule.transaction` (used by `ExchangeTransactionActuator` and `ExchangeInjectActuator`/`ExchangeWithdrawActuator`) feeds these raw balances directly into the bancor relay math (`ExchangeProcessor`/`SafeExchangeProcessor.exchange`), which computes `buyTokenQuant` purely from the ratio of the two integer balances, oblivious to how many "decimals" each balance actually represents [6](#0-5) [7](#0-6) . `AssetIssueCapsule` supports a per-token `precision` field ranging 0–6 that is set independently at issuance [8](#0-7) [3](#0-2) , so a token with precision 0 (whole-unit token) can be pooled against one with precision 6 (micro-unit token, like TRX itself uses `TRX_PRECISION = 1_000_000`) [9](#0-8) , with the exchange treating "1 unit" of each identically.

### Impact Explanation
Because the pool math does not account for the differing decimal scale between paired tokens, an attacker can create an exchange pairing a low-precision token against a high-precision one (or TRX) and immediately perform round-trip swaps that are mispriced by orders of magnitude (up to `10^6`), extracting the counter-token liquidity essentially for free — a direct theft-of-funds vector against any user who deposits liquidity or the pool creator's own funds interacting with third-party liquidity, and it can also cause legitimate `ExchangeTransactionContract`/`ExchangeWithdrawActuator` calls to receive drastically wrong `anotherTokenQuant`, matching the "wrong value → loss of funds" impact from the reported issue.

### Likelihood Explanation
Any unprivileged account holding a small amount of a TRC10 token can issue such an asset via `AssetIssueActuator` with an arbitrary precision, then call `ExchangeCreateContract` to pair it with any other token, and thereafter call `ExchangeTransactionContract` — all reachable directly through a signed transaction with no special permission, making this trivially reachable and repeatable.

### Recommendation
When creating/injecting/withdrawing/transacting in an `Exchange`, normalize both token balances to a common base unit using `AssetIssueCapsule.getPrecision()` (treating TRX as `precision = 6`, i.e. `TRX_PRECISION`) before feeding them into `ExchangeProcessor`/`SafeExchangeProcessor`, or reject `ExchangeCreateContract` calls where the two tokens' precisions are incompatible without an explicit conversion factor stored and applied consistently throughout `ExchangeCreateActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, and `ExchangeTransactionActuator`.

### Proof of Concept
1. Attacker calls `AssetIssueActuator` to issue token `A` with `precision = 0` and total supply `1_000_000`.
2. Attacker calls `AssetIssueActuator` to issue token `B` with `precision = 6` and total supply `1_000_000_000_000` (same nominal "real world" value as `A` if decimals were honored).
3. Attacker calls `ExchangeCreateContract` via `ExchangeCreateActuator` with `firstTokenBalance = 1_000_000` (token A) and `secondTokenBalance = 1_000_000` (token B) — validation in `ExchangeCreateActuator.doValidate()` passes because it never compares precision [4](#0-3) .
4. Attacker calls `ExchangeTransactionContract` selling a small amount of token A; because the AMM math in `ExchangeCapsule.transaction`/`SafeExchangeProcessor.exchange` treats the pool balances as equal-unit quantities, the attacker receives token B at a rate `10^6` times more favorable than token B's real decimal-adjusted value, draining the pool's token B liquidity [6](#0-5) .

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L104-116)
```java
      {
        // only save to new asset store
        ExchangeCapsule exchangeCapsuleV2 =
            new ExchangeCapsule(
                exchangeCreateContract.getOwnerAddress(),
                id,
                now,
                firstTokenID,
                secondTokenID
            );
        exchangeCapsuleV2.setBalance(firstTokenBalance, secondTokenBalance);
        exchangeV2Store.put(exchangeCapsuleV2.createDbKey(), exchangeCapsuleV2);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L183-228)
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

    if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(secondTokenBalance, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(secondTokenID, secondTokenBalance, dynamicStore)) {
        throw new ContractValidateException("second token balance is not enough");
      }
    }
```

**File:** protocol/src/main/protos/core/contract/asset_issue_contract.proto (L17-22)
```text
  bytes name = 2;
  bytes abbr = 3;
  int64 total_supply = 4;
  repeated FrozenSupply frozen_supply = 5;
  int32 trx_num = 6; // The fields trx_num and num define the exchange rate: num tokens can be purchased with trx_num TRX. This avoids using decimals.
  int32 precision = 7;
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L176-181)
```java
    int precision = assetIssueContract.getPrecision();
    if (precision != 0
        && dynamicStore.getAllowSameTokenName() != 0
        && (precision < 0 || precision > ActuatorConstant.PRECISION_DECIMAL)) {
      throw new ContractValidateException("precision cannot exceed 6");
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

**File:** chainbase/src/main/java/org/tron/core/capsule/AssetIssueCapsule.java (L87-95)
```java
  public int getPrecision() {
    return this.assetIssueContract.getPrecision();
  }

  public void setPrecision(int precision) {
    this.assetIssueContract = this.assetIssueContract.toBuilder()
        .setPrecision(precision)
        .build();
  }
```

**File:** common/src/main/java/org/tron/core/config/Parameter.java (L77-86)
```java
    public static final long PRECISION = 1_000_000;
    public static final long WINDOW_SIZE_MS = 24 * 3600 * 1000L;
    public static final long MAINTENANCE_SKIP_SLOTS = 2;
    public static final int SINGLE_REPEAT = 1;
    public static final int BLOCK_FILLED_SLOTS_NUMBER = 128;
    public static final int MAX_FROZEN_NUMBER = 1;
    public static final int BLOCK_VERSION = 37;
    public static final long FROZEN_PERIOD = 86_400_000L;
    public static final long DELEGATE_PERIOD = 3 * 86_400_000L;
    public static final long TRX_PRECISION = 1000_000L;
```
