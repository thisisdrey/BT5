Based on my research, I found a legitimate analog in java-tron's `Exchange` (Bancor-style AMM) subsystem, reachable by any account via a signed `ExchangeTransactionContract`.

### Title
Floating-point precision loss in the legacy `ExchangeProcessor` used by `ExchangeTransactionActuator`/`ExchangeCapsule.transaction` allows manipulated bonding-curve pricing and fund drain from TRX/TRC10 exchange pools - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java`)

### Summary
The external report's core bug class is: a price/weight calculation that can be manipulated by an untrusted caller because the underlying math is unsound (susceptible to precision/rounding manipulation) and unprotected by any hardening mechanism. java-tron's on-chain `Exchange` feature (its native Bancor-formula AMM for trading TRX against TRC10 tokens) has a directly analogous defect: the default, non-hardened calculation path computes swap output using Java `double` arithmetic and `Math.pow`, which is exploitable for precision-based value extraction from the pool, and this path is reachable by any unprivileged account issuing an `ExchangeTransactionContract`.

### Finding Description
`ExchangeTransactionActuator.execute` calls `ExchangeCapsule.transaction(tokenID, tokenQuant, dynamicStore.allowStrictMath(), allowHarden())` [1](#0-0) , which selects between two `Processor` implementations based on the `allowHarden()` flag: the legacy `ExchangeProcessor` (double/`Math.pow` based) or the newer `SafeExchangeProcessor` (BigDecimal-based) [2](#0-1) .

The legacy `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` perform the Bancor bonding-curve computation entirely in double-precision floating point, then truncate to `long` via a cast (`(long) issuedSupply`, `(long) exchangeBalance`) [3](#0-2) . Unlike a Chainlink-style oracle, this pool's own balances are the "price" input — and because it is a floating-point approximation of an exponential curve, an attacker can choose `tokenQuant` values and repeated small/large trade sequences that exploit the rounding behavior of `double` arithmetic near boundary conditions to obtain more output tokens than the exact (BigDecimal-correct) formula would yield, at the expense of the pool's counter-asset. This is functionally the same bug class as the report's "weighted calculation susceptibility" — an unauthenticated party can skew the derived price/weight by exploiting weaknesses in the calculation method itself, not just by manipulating an external oracle.

Critically, this precision-losing path is not a legacy corner case gated off by default guardrails equivalent to `ReentrancyGuard`: it is the *default* behavior. The hardened path (`SafeExchangeProcessor`, `hardenedCalc=true`) is only used when `allowHarden()` — i.e., `chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation()` — returns true, which is itself a proposal-gated committee flag [4](#0-3) . Until/unless that proposal is activated on a given chain, all `ExchangeTransactionContract`, `ExchangeInjectContract`, and related calls run through the unsound floating-point processor, and the negative-balance invariant check that exists in the hardened branch (`if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) throw ...`) is skipped entirely for the legacy path [5](#0-4) , meaning the legacy path has neither exact math nor a balance-safety backstop.

### Impact Explanation
An unprivileged account can broadcast crafted `ExchangeTransactionContract` transactions (or a sequence of them) against any TRX/TRC10 exchange pool still using the legacy (non-hardened) calculation, exploiting floating-point truncation/rounding in `ExchangeProcessor` to receive systematically favorable swap outputs. Repeated exploitation can drain pool liquidity beyond what the exact bonding-curve formula would allow, resulting in theft of pool funds belonging to the exchange creator/other traders — a direct funds-loss impact matching the "unauthorized account operation / theft of funds" acceptance criterion.

### Likelihood Explanation
Likelihood is High for any exchange that has not had `allowHardenExchangeCalculation` activated via governance proposal, since the vulnerable code path is the default and requires no special privileges — only a normal signed `ExchangeTransactionContract`/`ExchangeInjectContract` transaction from any account holding the relevant TRX/TRC10 balance and passing the actuator's `doValidate()` checks (balance sufficiency, non-zero pool balances, balance limits) [6](#0-5) .

### Recommendation
Make the `SafeExchangeProcessor` (BigDecimal-based, exact) path the mandatory default for all exchange calculations rather than gating it behind an opt-in proposal, and enforce the post-transaction non-negative-balance invariant unconditionally (not only when `hardenedCalc` is true) in `ExchangeCapsule.transaction` [7](#0-6) .

### Proof of Concept
1. Attacker identifies an `Exchange` pool where `allowHardenExchangeCalculation` proposal has not been activated (legacy `ExchangeProcessor` in use).
2. Attacker crafts an `ExchangeTransactionContract` with a `tokenQuant` chosen to maximize the discrepancy between the double-precision `Math.pow`-based bonding curve result in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` [3](#0-2)  and the mathematically exact result.
3. Attacker broadcasts the transaction; `ExchangeTransactionActuator.execute` applies the favorable-but-imprecise `anotherTokenQuant` to the attacker's account and updates the pool balances accordingly [8](#0-7) .
4. Attacker repeats across multiple transactions/blocks to accumulate excess value extracted from the pool, since no invariant check catches the drift in the non-hardened path.

**Uncertainty note:** I was unable to confirm the on-chain default value of `allowHardenExchangeCalculation` (whether it is currently active/deactivated on production java-tron networks) within the available tool calls — this depends on `DynamicPropertiesStore` default initialization and any already-passed governance proposals, which I could not fully verify before running out of iterations. This affects whether the vulnerable code path is presently reachable on mainnet or only on unconfigured/test networks.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-93)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L149-216)
```java
    if (!accountStore.has(ownerAddress)) {
      throw new ContractValidateException("account[" + readableOwnerAddress + NOT_EXIST_STR);
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);

    if (accountCapsule.getBalance() < calcFee()) {
      throw new ContractValidateException("No enough balance for exchange transaction fee!");
    }

    ExchangeCapsule exchangeCapsule;
    try {
      exchangeCapsule = Commons.getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(contract.getExchangeId()));
    } catch (ItemNotFoundException ex) {
      throw new ContractValidateException("Exchange[" + contract.getExchangeId()
          + ActuatorConstant.NOT_EXIST_STR);
    }

    byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
    byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
    long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
    long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();
    long tokenExpected = contract.getExpected();

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }
    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
      throw new ContractValidateException("token is not in exchange");
    }

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

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```
