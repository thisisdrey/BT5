### Title
Missing slippage/minimum-output protection in `ExchangeWithdrawContract` allows front-run to force the exchange creator to redeem far less than expected - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
The Sherlock finding centers on `ProtectionPool.withdraw()` computing a redemption amount from live, mutable exchange-rate state with no user-supplied minimum-output guard, so a state change sandwiched between transaction submission and execution can make the withdrawer receive far less than what they calculated off-chain. `ExchangeWithdrawActuator` in java-tron reproduces the same structural weakness: it derives the counter-token amount to pay out purely from the exchange's live `firstTokenBalance`/`secondTokenBalance` ratio at execution time, and the `ExchangeWithdrawContract` protobuf message carries no `expected`/minimum-amount field, unlike its sibling `ExchangeTransactionContract`, which was explicitly hardened with an `expected` parameter and a revert check.

### Finding Description
`ExchangeWithdrawContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` [1](#0-0)  — there is no field for a minimum acceptable amount of the paired token to receive.

In `ExchangeWithdrawActuator.doValidate()`, the amount of the other token to be paid out (`anotherTokenQuant`) is computed strictly from the exchange's current on-chain balances at validation time: [2](#0-1) 

Contrast this with `ExchangeTransactionActuator`, which was hardened against exactly this class of bug by requiring the caller to supply `expected` and reverting if the computed output falls short: [3](#0-2) 

`ExchangeWithdrawActuator` has no equivalent check. `execute()` simply recomputes the ratio again from the (potentially different, if other transactions executed first) live balances and pays out whatever that yields: [4](#0-3) 

Any other account can shift the exchange's `firstTokenBalance`/`secondTokenBalance` ratio arbitrarily (within balance limits) by submitting an `ExchangeTransactionContract` trade via `ExchangeCapsule.transaction()` [5](#0-4) . If such a trade is broadcast and included in a block before a pending `ExchangeWithdrawContract`, the withdrawer's `anotherTokenQuant` payout is computed against the post-trade, skewed ratio instead of the ratio they observed when they signed and sized their withdrawal request — with no on-chain mechanism to reject an unacceptable outcome.

### Impact Explanation
The exchange creator who submits `ExchangeWithdrawContract` (the analog of the LP/depositor in the report) can have their withdrawal executed against a manipulated or naturally-shifted price ratio and receive materially less of the counter-token than they expected when constructing the transaction, exactly mirroring the "withdraw() probably gets much less than expected" bug class: an economically meaningful, unbacked loss of value for a legitimate signed transaction with no attacker privilege required to trigger the ratio shift.

### Likelihood Explanation
Any account can submit an `ExchangeTransactionContract` against the same `exchange_id` — this requires no special permission, is reachable purely from broadcasting standard signed transactions, and is trivially timed by observing the mempool/pending transaction pool, since transaction ordering within a block is influenced by transmission/inclusion order that any unprivileged broadcaster can attempt to front-run.

### Recommendation
Add an `expected`/minimum-output field to `ExchangeWithdrawContract` (mirroring `ExchangeTransactionContract.expected`) and enforce it in `ExchangeWithdrawActuator.doValidate()`/`execute()`, reverting with a `ContractValidateException` if the freshly computed `anotherTokenQuant` falls below the caller-specified minimum.

### Proof of Concept
1. Exchange E has `firstTokenBalance = 100,000,000` (token A) and `secondTokenBalance = 200,000,000` (token B).
2. Creator observes this ratio and constructs `ExchangeWithdrawContract{exchange_id: E, token_id: A, quant: 10,000,000}` expecting to receive proportionally ~20,000,000 of token B, per the ratio math in `doValidate()` [6](#0-5) .
3. Before the creator's transaction is packed into a block, any account submits `ExchangeTransactionContract` selling a large amount of token B into the exchange, shifting `secondTokenBalance` upward and `firstTokenBalance` downward via `ExchangeCapsule.transaction()`.
4. The creator's `ExchangeWithdrawContract` then executes against the new, skewed balances, and `anotherTokenQuant` is recomputed from the post-trade ratio, paying out a smaller-than-expected amount of token B, with no field or check in `ExchangeWithdrawContract`/`ExchangeWithdrawActuator` to reject this outcome.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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
