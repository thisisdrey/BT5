### Title
Thin, permissionlessly-created on-chain Exchange pools allow price manipulation and fund extraction via front-run inject/withdraw sandwiching - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java`)

### Summary
Any account can create a TRON on-chain bancor-relay `Exchange` pool (`ExchangeCreateContract`) with only a token-balance-greater-than-zero requirement and no floor on liquidity, then that thin, easily manipulated pool is used directly as the pricing curve for subsequent `ExchangeTransactionContract` trades. This mirrors the reported "low liquidity pool used as price source" bug class: a pool with negligible reserves lets an attacker cheaply move the implied exchange rate before a victim's trade executes and reverse it afterward, extracting value from the victim.

### Finding Description
`ExchangeCreateActuator.doValidate` only checks that `firstTokenBalance` and `secondTokenBalance` are `> 0` and below the configurable upper bound `getExchangeBalanceLimit()` — there is no minimum liquidity requirement: [1](#0-0) 

This means anyone can create a pool with dust-level reserves (e.g. 1 TRX vs 1 unit of a TRC10 token). Trading against such a pool is done via `ExchangeCapsule.transaction`, which applies the bancor-style continuous relay formula in `ExchangeProcessor`/`SafeExchangeProcessor`: [2](#0-1) [3](#0-2) 

With tiny reserves, this formula produces extreme price sensitivity to reserve changes. `ExchangeInjectActuator` and the corresponding withdraw actuator let anyone atomically and cheaply shift the pool's token ratio (no swap fee, ratio recalculated from raw balances): [4](#0-3) 

`ExchangeTransactionActuator` only guards a victim's trade with a caller-supplied minimum output (`tokenExpected`), not a maximum acceptable price impact or TWAP-style protection: [5](#0-4) 

Because transactions within a block are ordered by the producing witness and observable pre-confirmation, an attacker can sandwich a victim's `ExchangeTransactionContract` against a thin pool: inject/withdraw to move price adversely, let the victim's trade execute (still satisfying its loose `tokenExpected` floor), then reverse the injection/withdrawal to capture the difference — directly analogous to the reported low-liquidity-oracle manipulation, but built into the protocol's own actuators rather than a third-party contract.

### Impact Explanation
This allows unauthorized extraction of victim funds (theft) through price manipulation of the protocol's built-in exchange mechanism, reachable by any account issuing standard, permissionless transactions (`ExchangeCreateContract`, `ExchangeInjectContract`, `ExchangeTransactionContract`, `ExchangeWithdrawContract`). No privileged role is required.

### Likelihood Explanation
Creating a low-liquidity pool costs only the `ExchangeCreate` fee and minimal token balances; injecting/withdrawing to shift price costs only gas/fee and is instantaneous within the pool's on-chain state. Any victim relying on the default `tokenExpected` slippage guard (rather than an unrealistically tight one) is exposed whenever they trade against a pool they don't control the liquidity of, making this practically exploitable whenever a low-liquidity pool exists and a victim transaction is observable before confirmation.

### Recommendation
Enforce a protocol-level minimum liquidity/reserve threshold in `ExchangeCreateActuator.doValidate` (and after `ExchangeWithdrawActuator` operations) so pools cannot be created or drained to near-zero reserves, and consider adding maximum price-impact protection (e.g., a TWAP or per-trade impact cap) in `ExchangeTransactionActuator` rather than relying solely on the caller-provided `tokenExpected` floor.

### Proof of Concept
1. Attacker calls `ExchangeCreateActuator` to create a pool with `firstTokenBalance = 1` (TRX) and `secondTokenBalance = 1` (TRC10 token) — accepted because validation only requires `> 0` [6](#0-5) .
2. Victim broadcasts an `ExchangeTransactionContract` to trade TRX for the token against this pool with a loosely set `tokenExpected`.
3. Before the victim's transaction is packed/executed, attacker submits `ExchangeInjectContract`/`ExchangeWithdrawContract` transactions to skew the reserve ratio favorably, using the reserve-ratio formula in `ExchangeInjectActuator` [4](#0-3) .
4. Victim's trade executes at the manipulated rate via `ExchangeCapsule.transaction` [2](#0-1) , still passing the `tokenExpected` check [7](#0-6)  but receiving far less value than fair price.
5. Attacker reverses the injection/withdrawal, capturing the difference extracted from the victim.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L201-208)
```java
    if (firstTokenBalance <= 0 || secondTokenBalance <= 0) {
      throw new ContractValidateException("token balance must greater than zero");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (firstTokenBalance > balanceLimit || secondTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
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
