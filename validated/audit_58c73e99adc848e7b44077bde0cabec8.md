### Title
Unlimited slippage in `ExchangeWithdrawActuator` allows front-run sandwich attack on liquidity withdrawal - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeWithdrawContract`, unlike `ExchangeTransactionContract`, has no user-specified minimum-output ("expected") field. The amount of the paired token a withdrawing exchange creator receives is computed purely from the token pool's *current* balances at execution time, with no protection against the ratio having shifted since the transaction was signed and broadcast.

### Finding Description
`ExchangeTransactionContract` (a TRC10 AMM-style swap) includes an `expected` field that is validated against the actual output amount, protecting the trader from slippage: [1](#0-0) 

In contrast, `ExchangeWithdrawContract` has no analogous minimum-output field in its protobuf definition — only `owner_address`, `exchange_id`, `token_id`, and `quant`: [2](#0-1) 

In `ExchangeWithdrawActuator.execute()`, the amount of the counterpart token (`anotherTokenQuant`) returned to the withdrawer is computed strictly from the pool's live `firstTokenBalance`/`secondTokenBalance` at the moment the transaction executes, using the proportional pool ratio: [3](#0-2) 

The `doValidate()` method only checks that `anotherTokenQuant` is non-zero and that the rounding "precision" is within an internal tolerance band — it never checks it against any value chosen or expected by the caller: [4](#0-3) 

Because TRX and TRC10 token trading against the same exchange pool (`ExchangeTransactionContract`) is permissionless and can be broadcast by any account, an attacker who observes a pending `ExchangeWithdrawContract` transaction in the mempool can submit a large trade against the same `exchange_id` first (front-run), shifting `firstTokenBalance`/`secondTokenBalance` unfavorably, and then trade back afterward (back-run) to restore the pool and pocket the difference — a classic sandwich attack against the withdrawer, since the withdraw computation blindly trusts whatever ratio exists at execution time.

### Impact Explanation
The exchange creator withdrawing their liquidity can receive an arbitrarily small amount of the counterpart token relative to what the pool ratio was when they signed the transaction, because there is no floor/minimum enforced on `anotherTokenQuant`. This is a direct value-extraction vector against liquidity providers on TRON's on-chain TRC10 exchange, resulting in economic loss of funds during withdrawal — the same underlying flaw as the referenced report's "unlimited slippage" during exit due to a zero/absent minimum-out check.

### Likelihood Explanation
Any account can submit `ExchangeTransactionContract` trades against a given `exchange_id`, so front-running a pending withdraw only requires observing the mempool and broadcasting a higher-priority transaction (or, for a block-producing witness, simply reordering transactions within a block it produces). This is a normal-user/attacker-reachable path requiring no special privileges — only the victim needs to be the exchange creator performing a normal withdraw operation.

### Recommendation
Add a caller-specified minimum output field to `ExchangeWithdrawContract` (analogous to `expected` in `ExchangeTransactionContract`) and validate the actual `anotherTokenQuant` against it in `ExchangeWithdrawActuator.doValidate()`/`execute()`, rejecting the transaction if the computed value falls below the caller's specified minimum.

### Proof of Concept
1. Attacker observes a pending `ExchangeWithdrawContract` for `exchange_id=X`, `token_id=firstTokenID`, `quant=Q` from the pool creator.
2. Attacker broadcasts an `ExchangeTransactionContract` trade that sells a large amount of `secondTokenID` into the pool, sharply reducing `firstTokenBalance` relative to `secondTokenBalance` (per `ExchangeCapsule.transaction()` at [5](#0-4) ), ensuring it lands in the block before the withdraw.
3. The victim's withdraw executes against the now-skewed pool, computing `anotherTokenQuant` from the manipulated ratio at [6](#0-5) , yielding far less `secondTokenID` than the creator would have received under the original ratio.
4. Attacker submits a reverse trade to restore the pool and realize the extracted value, with no on-chain check ever having required a minimum output for the withdraw.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L225-243)
```java
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
