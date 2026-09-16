## Title
Exchange pools can be created with an arbitrarily skewed 1-unit initial balance, letting the creator set a manipulated price and guarantee arbitrage profit against later traders - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java`)

### Summary
`ExchangeCreateActuator` only rejects a new TRC10 exchange pool if either side's initial balance is `<= 0` or exceeds `getExchangeBalanceLimit()`; it never checks that the two sides are balanced or that either side holds a meaningful minimum amount of value. This mirrors the Y2K "null epoch" bug class: a party can seed a pool with an economically negligible amount (e.g. `1`) on one side and the maximum allowed amount on the other, fixing an extreme, arbitrary exchange rate at creation time with no counterparty risk, then extract guaranteed profit from anyone who trades against that pool via `ExchangeTransactionActuator`.

### Finding Description
`ExchangeCreateActuator.doValidate()` enforces only: [1](#0-0) 

There is no check that `firstTokenBalance` and `secondTokenBalance` are within a sane ratio of each other, nor any minimum-liquidity floor beyond `> 0`. A caller can set, for example, `firstTokenBalance = 1` and `secondTokenBalance = balanceLimit` (the max allowed), and `execute()` stores this ratio directly as the pool's price: [2](#0-1) 

Subsequent trades go through `ExchangeCapsule.transaction`, which uses the bancor-style `ExchangeProcessor`/`SafeExchangeProcessor` purely as a function of the two stored balances: [3](#0-2) 

Because the pool's initial price is entirely attacker-controlled and unconstrained (analogous to depositing "1 wei" into an empty vault to fix a favorable settlement outcome in the Y2K report), the creator can:
1. Create a pool with `firstTokenBalance = 1` (token A) and `secondTokenBalance = balanceLimit` (TRX or token B).
2. Immediately call `ExchangeInjectActuator`/`ExchangeTransactionActuator` (or wait for another user to trade) at this fixed skewed rate to buy out almost the entire `secondTokenBalance` for a negligible amount of token A, or to sell a large batch of newly-minted/cheap token A into the pool at a rate that yields disproportionate TRX/asset B.
3. Because the injection ratio (`ExchangeInjectActuator`) and the trade math (`ExchangeCapsule.transaction`) both derive strictly from these attacker-chosen initial balances, subsequent participants who interact with the pool receive the manipulated price with no protection.

`ExchangeInjectActuator` reinforces this because any later injector must match the attacker's already-skewed ratio, socializing the manipulated price: [4](#0-3) 

### Impact Explanation
An attacker-created pool with an extreme initial ratio lets the creator or a colluding party extract asset value from any unsuspecting trader or injector who interacts with the pool at the attacker-set price, resulting in unauthorized transfer/theft of TRX or TRC10 token value between accounts — matching the "unbacked balance / theft of funds" impact bar. This requires only a single signed `ExchangeCreateContract` transaction and follow-on `ExchangeTransactionContract`/`ExchangeInjectContract` transactions, all reachable by any unprivileged account through the normal actuator/transaction path.

### Likelihood Explanation
This is directly reachable by any account with sufficient balance to pay the exchange-create fee and seed 1 unit of a token; no special privileges (SR/witness/committee) are required. The `ExchangeCreateActuator` validation logic explicitly permits any strictly-positive, non-zero pair of balances up to `balanceLimit`, so the precondition is trivially satisfiable in normal operation.

### Recommendation
Add a minimum-liquidity/ratio safeguard to `ExchangeCreateActuator.doValidate()` (and correspondingly to `ExchangeInjectActuator`), for example:
- Require both `firstTokenBalance` and `secondTokenBalance` to be at least some minimum absolute amount (not just `> 0`), and/or
- Require the ratio between the two balances to stay within a bounded range, and/or
- Lock/burn a portion of the initial liquidity (similar to Uniswap's `MINIMUM_LIQUIDITY` approach) so the creator cannot unilaterally set and immediately exploit an arbitrary price.

### Proof of Concept
1. Attacker calls `ExchangeCreateContract` with `first_token_id = TRX`, `first_token_balance = 1`, `second_token_id = <TRC10 X>`, `second_token_balance = <dynamicStore.getExchangeBalanceLimit()>`.
2. `ExchangeCreateActuator.doValidate()` passes because both balances are `> 0` and `<= balanceLimit`.
3. `execute()` persists the exchange with this 1 : balanceLimit ratio in `ExchangeCapsule`.
4. Attacker (or a victim who is unaware of the skew) calls `ExchangeTransactionContract` to trade against the pool; `ExchangeCapsule.transaction()` computes the resulting quantities strictly from the fixed skewed balances, yielding an outsized amount of token X for a minimal TRX input, transferring value out of the pool disproportionately to any later injector/trader who is forced to match this ratio.

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
