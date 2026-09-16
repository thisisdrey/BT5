## Title
Exchange creator can sandwich trader `ExchangeTransaction` swaps with `ExchangeWithdraw`/`ExchangeInject` to steal value from traders via manipulated price impact - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`)

### Summary
TRON's Bancor-style `Exchange` is a single-owner AMM: only the account that created the pool (the "creator") is authorized to add or remove liquidity via `ExchangeInjectContract`/`ExchangeWithdrawContract`, while any account can trade against the pool via `ExchangeTransactionContract`. Because the creator fully controls the pool's reserve balances at will, they can transiently shrink the pool right before a victim's swap and restore it right after, worsening the victim's execution price and capturing the difference — the on-chain analog of the reported LP sandwich attack.

### Finding Description
`ExchangeInjectActuator` and `ExchangeWithdrawActuator` both restrict liquidity changes to the exchange's creator: [1](#0-0) [2](#0-1) 

Withdrawal removes tokens from both reserves in the exact ratio dictated by the current balances: [3](#0-2) 

`ExchangeTransactionActuator` computes the counterparty's output purely from the *current* reserve balances at execution time via `ExchangeCapsule.transaction`, with no minimum-price or slippage-limit protections other than the trader-specified `tokenExpected` check performed against the pool state at that instant: [4](#0-3) [5](#0-4) 

The Bancor-formula pricing curve (`exchangeToSupply`/`exchangeFromSupply`) is a function of the *absolute* reserve balances relative to the *fixed* trade quantity requested — it is not scale-invariant with respect to a fixed-size trade: [6](#0-5) [7](#0-6) 

Because the creator can proportionally shrink the pool with `ExchangeWithdrawContract` immediately before a pending victim `ExchangeTransactionContract`, the same fixed-size trade becomes a much larger fraction of the (now smaller) reserves, causing the trader to receive substantially less output for the same input (increased effective slippage/price impact). The creator then restores the reserves with `ExchangeInjectContract` right after the trade executes, recapturing the withdrawn liquidity plus the extra value siphoned from the victim's degraded execution price, with no fee or penalty on inject/withdraw (`calcFee()` returns 0 for both): [8](#0-7) [9](#0-8) 

This is the same root-cause bug class as the referenced report: a liquidity-controlling party using unrestricted, fee-free add/remove-liquidity operations to sandwich an ordinary trade and extract otherwise-unavailable value from an unprivileged trader, without needing any elevated node/consensus privilege — only the (permissionlessly obtainable) role of exchange creator via `ExchangeCreateActuator`.

### Impact Explanation
Any account can become an exchange "creator" by creating an `Exchange` pair and later attract trade volume. The creator can then use fee-free `ExchangeWithdraw`/`ExchangeInject` calls to systematically degrade execution for every `ExchangeTransaction` trader interacting with their pool, siphoning value that traders would otherwise receive. This is a theft-of-funds vector against unprivileged traders, matching the "Medium/High" bar of concrete unauthorized value extraction from ordinary users.

### Likelihood Explanation
Exploitation only requires the attacker to be the creator of an `Exchange` (permissionless via `ExchangeCreateActuator`) and to time a withdraw transaction before and an inject transaction after a target trade — both of which are ordinary, fee-free transactions reachable by any signed transaction. No special block-producer or witness privilege is required, only reasonably favorable transaction sequencing (e.g., submitting withdraw ahead of a known pending large trade and inject shortly after), which is realistic on any chain without private mempools since the trade is broadcast publicly before confirmation.

### Recommendation
Consider disallowing (or rate-limiting/cooldown-gating) `ExchangeInject`/`ExchangeWithdraw` from the same creator address within the same block or a short block window as trades on that exchange, or charge a proportional fee on inject/withdraw so that transient liquidity manipulation is not free, or require inject/withdraw to preserve a minimum time-weighted reserve size (TWAP-based) to prevent single-block liquidity-shrink attacks against traders.

### Proof of Concept
1. Attacker calls `ExchangeCreateActuator` to create an Exchange for token pair A/B with reserves `R_A, R_B` and becomes `creatorAddress`.
2. A victim broadcasts `ExchangeTransactionContract` intending to sell a fixed quantity `q` of A for B against the current curve computed from `R_A, R_B` (see `ExchangeCapsule.transaction`, `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java` lines 64-69).
3. Before the victim's transaction is packed, the attacker (as creator) submits `ExchangeWithdrawContract` withdrawing a large proportional share of `R_A, R_B` (see `ExchangeWithdrawActuator.execute`, lines 74-89), shrinking the reserves to `R_A', R_B'` while preserving the reserve ratio.
4. The victim's `ExchangeTransactionContract` executes against the now much smaller reserves, receiving significantly less output B for the same input A than it would have against `R_A, R_B`.
5. The attacker submits `ExchangeInjectContract` to restore the reserves (see `ExchangeInjectActuator.execute`, lines 71-99), recapturing the temporarily withdrawn tokens plus the extra value extracted from the victim's degraded trade, at zero fee (`calcFee()` returns 0 for both inject and withdraw).

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L283-286)
```java
  @Override
  public long calcFee() {
    return 0;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L232-235)
```java

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (newTokenBalance > balanceLimit || newAnotherTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-69)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/SafeExchangeProcessor.java (L19-28)
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
