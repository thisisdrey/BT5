### Title
TRC10 AMM Exchange pools treat token balances as fungible integers regardless of `precision`, causing mispriced trades and rounding-to-zero losses for low-precision tokens - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java])

### Summary
TRON's built-in TRC10 AMM ("Bancor-style" Exchange pools) let any account create a pool between two arbitrary TRC10 tokens (or TRX) with `ExchangeCreateContract`, then trade/inject/withdraw via `ExchangeTransactionContract`, `ExchangeInjectContract`, `ExchangeWithdrawContract`. TRC10 tokens can be issued with a `precision` field from 0 to 6 (`ActuatorConstant.PRECISION_DECIMAL`), meaning tokens have different "decimal" granularity in exactly the same way the ERC20 reserve tokens in the referenced Multipool.sol report do (e.g., USDC 6 vs WETH 18 decimals). None of the Exchange actuators or the `ExchangeCapsule`/`ExchangeProcessor`/`SafeExchangeProcessor` pricing math ever reads or normalizes for `precision` — all balances and quantities are treated as raw `long` integer units of equal weight.

### Finding Description
- `AssetIssueActuator.doValidate()` allows issuing TRC10 tokens with `precision` from 0 up to 6 (`ActuatorConstant.PRECISION_DECIMAL`) once `allowSameTokenName` is active: [1](#0-0) 
- `ExchangeCreateActuator.execute()` creates a pool directly from the raw `firstTokenBalance`/`secondTokenBalance` supplied by the user, with no reference to each token's `precision`: [2](#0-1) 
- The core AMM pricing logic in `ExchangeCapsule.transaction()` and its `Processor` implementations (`ExchangeProcessor`, `SafeExchangeProcessor`) operate purely on the raw `long` balances passed in, with no decimal/precision normalization anywhere in the bonding-curve math: [3](#0-2) [4](#0-3) 
- `ExchangeInjectActuator` and `ExchangeWithdrawActuator` similarly compute the "another token" amount as a straight ratio of raw balances (`bigSecondTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance)`), again with no precision scaling: [5](#0-4) [6](#0-5) 

Because a token with `precision=0` has its total supply expressed in whole units while a token with `precision=6` expresses the same economic value in units 1,000,000x smaller, pairing them in one pool makes the AMM's constant/bonding-curve math price the pool as if 1 raw unit of each token were equally valuable. This is functionally identical to the reported Multipool.sol bug of pairing reserve tokens with different decimals (e.g., USDC 6 decimals vs WETH 18 decimals) without decimal normalization, leading to a materially mispriced pool and integer-division rounding to zero for the lower-magnitude side.

### Impact Explanation
- Anyone can create such a mismatched-precision pool via a single signed `ExchangeCreateContract` transaction (no permission checks beyond owning the tokens/TRX), and any other user can then trade against it via `ExchangeTransactionContract`.
- Because the AMM curve treats raw integer balances as directly comparable value, the effective exchange rate is off by orders of magnitude equal to `10^(precision difference)`. A trader (attacker) can drain the higher-precision (i.e., smaller "significant" nominal balance side, larger real value) token from the pool cheaply, or a liquidity provider injecting via `ExchangeInjectActuator`/withdrawing via `ExchangeWithdrawActuator` will receive/give up wildly incorrect ratios of the paired token — a direct, unbacked-balance/theft-of-funds vector against other pool participants (the pool creator/injectors), not merely a display or informational bug.
- Small-quantity trades on the low-precision side can also round to zero in `anotherTokenQuant` computations, but the more severe outcome is that legitimate liquidity providers or the pool creator can lose real economic value to arbitrageurs exploiting the precision mismatch, satisfying the "unauthorized account operation / theft of funds" bar.

### Likelihood Explanation
Likelihood is straightforward and requires no privileged access: any account can issue two TRC10 tokens with different `precision` values (0–6) and create an Exchange pool pairing them, then trade against it — all via ordinary signed transactions (`AssetIssueContract`, `ExchangeCreateContract`, `ExchangeTransactionContract`). No malicious SR/witness/peer role is needed; this is purely an unprivileged wallet/transaction-broadcaster path.

### Recommendation
Either (a) disallow creating an `ExchangeCreateContract` pool where the paired tokens (including TRX, which has an implicit 6-decimal SUN unit) have different `precision` values, or (b) normalize all Exchange pool balances/quantities to a common decimal base (e.g., scale every token amount to a canonical 6-decimal unit) before they are fed into `ExchangeCapsule.transaction()`, `ExchangeProcessor`, `SafeExchangeProcessor`, and the ratio math in `ExchangeInjectActuator`/`ExchangeWithdrawActuator`.

### Proof of Concept
1. Issue Token A with `precision=0` and Token B with `precision=6` via two `AssetIssueContract` transactions (allowed by `AssetIssueActuator.doValidate()` precision check of 0–6).
2. Create an Exchange pool with `ExchangeCreateContract` pairing Token A and Token B using equal raw `firstTokenBalance`/`secondTokenBalance` (e.g., 1,000,000 raw units each) — accepted because `ExchangeCreateActuator` never inspects `precision`.
3. Because Token B's raw unit is worth 1,000,000x less real value than Token A's raw unit (due to the precision difference), the pool is created at a rate that is off by a factor of 10^6 relative to the tokens' true economic value.
4. An attacker calls `ExchangeTransactionContract` to sell the "cheap" (per raw-unit) token into the mispriced pool and receive a disproportionate amount of the other token back, extracting value from the pool/liquidity provider — mirroring the reserve-token-decimal exploitation pattern described in the original Multipool.sol report.

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
