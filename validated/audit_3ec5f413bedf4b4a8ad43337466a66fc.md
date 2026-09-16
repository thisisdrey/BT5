## Finding

### Title
Unbounded Bancor-curve output in exchange swaps allows minting unbacked TRX/TRC10 balances - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`)

### Summary
The InuSaitama report describes an attacker abusing an AMM-style bonding-curve swap to extract far more value than should be economically possible via a single round-trip trade. java-tron ships an analogous on-chain bonding-curve market — the `Exchange`/`ExchangeV2` TRC10↔TRX and TRC10↔TRC10 pools — reachable by any unprivileged account via a single `ExchangeTransactionContract`. The actuator that settles these swaps never validates that the computed "buy" amount is actually backed by the pool's real token balance, and the default (non-hardened) math path performs unchecked floating-point arithmetic that can produce an output exceeding the pool's holdings.

### Finding Description
`ExchangeCapsule.transaction()` computes the swap output using the `ExchangeProcessor` Bancor-style formula, which converts the sell side into an abstract "relay" supply and then back into the buy-side balance using `Math.pow` with an exponent of `2000.0` [1](#0-0)  This computation is fundamentally unbounded: for large `sellTokenQuant` values relative to the fixed `supply` constant (`1_000_000_000_000_000_000L`), `Maths.pow(1 + supplyQuant/supply, 2000.0)` can grow enormously, producing an `exchangeBalance` (the buy amount returned to the trader) that is not constrained to be `<= buyTokenBalance` anywhere in the code.

In the default (legacy) execution path, `ExchangeCapsule.transaction()` simply does `secondTokenBalance - buyTokenQuant` with plain long arithmetic and stores whatever the result is, without any check that it stays non-negative [2](#0-1)  Only the `hardenedCalc` branch (gated behind the `ALLOW_HARDEN_EXCHANGE_CALCULATION` chain parameter, off by default) adds a post-hoc `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` guard [3](#0-2) 

Crucially, `ExchangeTransactionActuator.doValidate()` only checks that the *seller* has enough balance/asset for the sell side and that the balance limit isn't exceeded, and separately checks `anotherTokenQuant < tokenExpected` (a user-supplied slippage floor, not a pool-liquidity ceiling) [4](#0-3)  There is no validation anywhere in `validate()` or `execute()` that the computed `anotherTokenQuant` does not exceed the exchange pool's actual `anotherTokenID` balance before crediting it to the trader's account [5](#0-4)  This is in contrast to `ExchangeWithdrawActuator`, which explicitly checks `firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant` and rejects with `"exchange balance is not enough"` [6](#0-5)  — the equivalent guard is absent from the swap path.

Because `tokenID`/`anotherTokenID` can be TRX itself (`TRX_SYMBOL_BYTES`), a successful over-large swap directly credits the attacker's TRX balance via `accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant))` [7](#0-6)  with no requirement that the pool actually held that TRX, and the pool's stored balance for that side can go negative under the default (non-hardened) math path.

### Impact Explanation
An attacker who identifies (or creates via `ExchangeCreateContract`) an `Exchange`/`ExchangeV2` pool and crafts a sell quantity that pushes the Bancor curve's exponential term (`(1+x)^2000`) into a regime where floating-point/BigDecimal output exceeds the pool's real counter-token balance can extract more TRX or TRC10 tokens than the pool holds. This is an unbacked-balance / theft-of-funds bug reachable by any unprivileged transaction broadcaster with a signed `ExchangeTransactionContract`, matching the "unbacked balance" and "theft of funds" impact categories.

### Likelihood Explanation
Exploitability depends on chain-parameter state (`allowHardenExchangeCalculation`, default `0`) and requires the attacker to size trades so that the floating-point/BigDecimal bonding-curve output outruns pool liquidity, which is deterministic and can be pre-computed offline against a target pool's known reserves before broadcasting a single transaction — no privileged role or race condition is required.

### Recommendation
Add an explicit liquidity check to `ExchangeTransactionActuator` (both `doValidate()` and `execute()`) enforcing `anotherTokenQuant <= exchangeCapsule`'s current balance of `anotherTokenID` (or that the resulting pool balance is `>= 0`), independent of the `allowHarden`/`ALLOW_HARDEN_EXCHANGE_CALCULATION` flag, and make this the unconditional default rather than an opt-in hardened path.

### Proof of Concept
1. Create (or locate) an `Exchange`/`ExchangeV2` pool via `ExchangeCreateContract` with a modest TRX/token ratio.
2. Compute offline, using the same Bancor formula as `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply`, a `sellTokenQuant` for which the resulting `(1 + supplyQuant/supply)^2000` term yields a `buyTokenQuant` that exceeds the pool's actual `buyTokenBalance`.
3. Broadcast a single `ExchangeTransactionContract` with that `quant`/`expected`; `doValidate()` passes because it only checks `anotherTokenQuant >= tokenExpected`, not pool sufficiency.
4. `execute()` credits the attacker with `anotherTokenQuant` (TRX or TRC10) exceeding the pool's real reserves, and (in the default non-hardened path) stores a negative or inconsistent pool balance with no error. [8](#0-7) [9](#0-8)

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L160-162)
```java
    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L221-223)
```java
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }
```
