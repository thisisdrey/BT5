### Title
Lack of slippage protection in `ExchangeWithdrawActuator` allows a bancor-exchange creator to receive an unexpected (manipulable) amount of the paired token on withdrawal - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeWithdrawContract`, unlike `ExchangeTransactionContract`, carries no `expected`/minimum-out field, so `ExchangeWithdrawActuator` computes the "another token" payout purely from whatever `firstTokenBalance`/`secondTokenBalance` ratio exists in the `ExchangeCapsule` at the moment the transaction is actually executed, not at the moment it was signed and broadcast.

### Finding Description
`ExchangeTransactionContract` (the bancor-style swap) includes an explicit `expected` field that is enforced in `ExchangeTransactionActuator.doValidate()`, reverting the trade if the output would be less than the caller's expectation: [1](#0-0) 

`ExchangeWithdrawContract`, however, only carries `exchange_id`, `token_id`, and `quant` — no minimum/maximum bound for the paired ("another") token: [2](#0-1) 

In `ExchangeWithdrawActuator.execute()`, the amount of the paired token returned to the exchange creator is derived from the *current* on-chain pool ratio (`firstTokenBalance`/`secondTokenBalance`) at execution time, using the same proportional-withdraw math flagged in the external report for `Pool.sol`: [3](#0-2) 

Because any account can freely trade against the same bancor exchange with `ExchangeTransactionContract` (which changes `firstTokenBalance`/`secondTokenBalance` via `ExchangeCapsule.transaction()`): [4](#0-3) 

the exchange-creator's pending `ExchangeWithdrawContract` transaction can be sandwiched: an unprivileged transaction broadcaster observes the withdraw transaction in the mempool, submits an `ExchangeTransactionContract` swap that skews the pool ratio just before the withdraw is packed into a block, and the withdraw actuator then computes `anotherTokenQuant` off that skewed ratio — with no `expected`/minimum bound the withdrawer could have specified to abort the transaction. `doValidate()` only checks that balances are sufficient and that rounding precision is acceptable; it never re-validates against the ratio the user observed when they signed the transaction: [5](#0-4) 

This is the same root cause identified in the external report for Sentiment's `Pool.sol`/`SuperPool.sol`: a withdrawal function whose payout is computed from a mutable, attacker-influenceable exchange rate at execution time, with no slippage bound supplied by the caller.

### Impact Explanation
An exchange creator withdrawing liquidity (`withdraw()`) can receive materially less of the "another token" than the ratio they observed when signing, because any address can move the pool ratio via `ExchangeTransactionContract` between broadcast and inclusion. This is a direct, unauthorized value transfer from the withdrawing party to whoever manipulates the ratio (sandwiching), i.e., concrete loss of funds for a normal, unprivileged operation — matching the Medium severity class established for the analogous Sentinel V2 issue.

### Likelihood Explanation
Any account can create an exchange (`ExchangeCreateContract`) and later withdraw from it; any other unprivileged account can submit `ExchangeTransactionContract` swaps against the same exchange at will. Sandwiching a visible, pending `ExchangeWithdrawContract` transaction requires no special privilege — just observing the mempool and submitting an ordinary signed transaction before the withdraw is included, making this practically reachable by any transaction broadcaster.

### Recommendation
Add a minimum-amount-out (and/or maximum-quant-in) field to `ExchangeWithdrawContract`, analogous to the `expected` field already present on `ExchangeTransactionContract`, and enforce it in `ExchangeWithdrawActuator.doValidate()`/`execute()` so the transaction reverts if the computed `anotherTokenQuant` falls below the caller-specified minimum.

### Proof of Concept
1. Alice creates a bancor exchange (`ExchangeCreateContract`) and later signs `ExchangeWithdrawContract` to withdraw `tokenQuant` of `firstTokenID`, expecting `anotherTokenQuant ≈ secondTokenBalance * tokenQuant / firstTokenBalance` based on the pool state she observes.
2. Before Alice's transaction is packed into a block, Bob (any unprivileged account) submits and gets included an `ExchangeTransactionContract` that sells a large amount of `firstTokenID` into the same exchange, sharply reducing `secondTokenBalance` relative to `firstTokenBalance` (per `ExchangeCapsule.transaction()`).
3. Alice's `ExchangeWithdrawContract` then executes against the now-skewed balances in `ExchangeWithdrawActuator.execute()` (lines 74-89), yielding a much smaller `anotherTokenQuant` than Alice anticipated, with no field available in `ExchangeWithdrawContract` to set a minimum acceptable amount and abort.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** Tron protobuf protocol document.md (L1403-1420)
```markdown
     - message `ExchangeWithdrawContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to withdraw.
    
       `quant`: token amount to withdraw.
    
      ```java
      message ExchangeWithdrawContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
      }
      ```
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-243)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-168)
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
```
