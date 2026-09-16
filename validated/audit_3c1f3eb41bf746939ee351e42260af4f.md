### Title
Oracle-less AMM price manipulation via un-bounded Bancor-curve rounding in TRON's on-chain Exchange (`ExchangeInjectActuator`/`ExchangeTransactionActuator`) - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`)

### Summary
The Full Sail incident describes an attacker who abused a manipulable price source (a Switchboard oracle key) to publish a wildly distorted price, deposit against that distorted valuation, then restore the price and withdraw disproportionately more than deposited. The closest reachable analog in java-tron is its own on-chain "oracle" for TRC10 pairs: the Bancor-formula `Exchange` object (`ExchangeCreateContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract`/`ExchangeTransactionContract`). Anyone can permissionlessly create an exchange pool and become its "creator," then use `ExchangeInjectActuator` to shift the pool's internal price (the value later read by `ExchangeTransactionActuator` for every trader) with asymmetric rounding, and follow with `ExchangeWithdrawActuator`/`ExchangeTransactionActuator` to extract more value than was contributed — the same "distort → act on distorted value → restore/withdraw" pattern as the external report.

### Finding Description
`ExchangeInjectActuator.execute` computes the counter-token amount using floor division with no minimum-output or slippage protection: [1](#0-0) 

This differs from `ExchangeWithdrawActuator`, which explicitly checks for rounding drift and rejects imprecise withdrawals ("Not precise enough") using a `BigDecimal` precision guard: [2](#0-1) 

No equivalent precision/ratio-drift check exists on the inject path, so repeated small injections can nudge the pool's `firstTokenBalance`/`secondTokenBalance` ratio away from its "fair" value at near-zero cost due to `floorDiv` truncation, since the injector only has to relinquish a rounded-down amount of the counter-asset while the pool records that same rounded-down amount as the new balance (both sides consistent internally, but the *ratio itself* — the "price" all subsequent traders and injectors read via `ExchangeCapsule.transaction`/`ExchangeProcessor.exchange` — drifts). [3](#0-2) 

Because the pool price is entirely on-chain and self-referential (no external price feed), the pool's own state functions as the "oracle" that `ExchangeTransactionActuator` (open to any unprivileged transaction broadcaster) reads for every trade: [4](#0-3) 

An attacker who creates their own exchange pool controls this "oracle" directly (analogous to the Switchboard attacker controlling an oracle key), can shift it via `ExchangeInjectActuator`'s unguarded rounding, trade/withdraw at the distorted ratio, then reverse the injection — netting value at the expense of the pool's other liquidity/other traders in the same block.

### Impact Explanation
If exploitable, this allows unbacked value extraction from a TRC10 Bancor exchange pool without any privileged access — a direct violation of the AMM's invariant, resulting in some accounts receiving more asset value than they contributed (unbacked balance) at the expense of other pool participants. This matches the "theft of funds / unbacked balance" impact bar for Medium severity.

### Likelihood Explanation
Likelihood is constrained because `ExchangeInjectContract`/`ExchangeWithdrawContract` require the caller to be the exchange's `creatorAddress`: [5](#0-4) 
so an attacker must first create their own pool (`ExchangeCreateContract`, permissionless), fund it, and induce third-party liquidity/trading against it via `ExchangeTransactionActuator` (which is open to anyone) to realize profit beyond their own capital — i.e., real-world exploitability depends on other users trading against the attacker-controlled pool while it is in a distorted state, and on the magnitude of extractable drift being economically meaningful relative to gas/fee costs. This is a rounding-arithmetic issue in long-standing production logic rather than a newly introduced regression, so it likely requires careful parameter selection (large balances, many small injections) to accumulate meaningful drift.

### Recommendation
Add the same rounding/precision-drift guard used in `ExchangeWithdrawActuator` (a `BigDecimal`-based ratio-drift bound, e.g. the "Not precise enough" check) to `ExchangeInjectActuator`, and/or require injections to preserve the pool ratio within a strict tolerance (reject injections that would move `firstTokenBalance/secondTokenBalance` beyond a negligible epsilon from the pre-injection ratio). Consider also bounding per-transaction/per-block cumulative ratio drift for both inject and transaction paths.

### Proof of Concept
1. Attacker calls `ExchangeCreateContract` to create a new TRC10↔TRX exchange pool they control as `creatorAddress`.
2. Attacker repeatedly calls `ExchangeInjectContract` with token quantities chosen so that `floorDiv(secondTokenBalance * tokenQuant, firstTokenBalance)` truncates non-trivially each time (see `ExchangeInjectActuator.java:71-83`), progressively skewing the pool ratio while paying a rounded-down (cheaper) counter-asset amount each time.
3. A victim (or the attacker under a second address) calls `ExchangeTransactionContract` against the now-skewed pool, receiving a distorted `anotherTokenQuant` computed by `ExchangeCapsule.transaction` (`ExchangeCapsule.java:124-146`).
4. Attacker calls `ExchangeWithdrawContract` to unwind their position, capturing the accumulated rounding drift as net profit, since `ExchangeInjectActuator` never validated ratio-preservation the way `ExchangeWithdrawActuator` does.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L228-243)
```java
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-146)
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

```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-76)
```java
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

```
