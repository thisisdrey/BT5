This confirms a clear analog. `ExchangeTransactionContract` (the trading path through the Bancor-style relay exchange) explicitly has an `expected` field checked via `token required must greater than expected` at [1](#0-0) , i.e., slippage protection exists for traders. However, `ExchangeWithdrawContract` — used by the exchange creator (LP) to withdraw/burn their pooled liquidity — has no such parameter at all: it only carries `owner_address`, `exchange_id`, `token_id`, and `quant` with no minimum-received field.

### Title
LPs cannot set a minimum received amount when withdrawing exchange liquidity via `ExchangeWithdrawActuator`, exposing them to front-run slippage losses - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
`ExchangeWithdrawActuator` lets an exchange creator burn their share of a TRC10 Bancor-style liquidity pool (`ExchangeCapsule`) and receive a proportional amount of both pooled tokens. Unlike `ExchangeTransactionActuator`, which enforces a caller-supplied `expected` minimum, `ExchangeWithdrawContract` has no minimum-output parameter, so a withdrawer cannot bound the `anotherTokenQuant` they will actually receive.

### Finding Description
In `ExchangeWithdrawActuator.execute`/`doValidate`, the amount of the "other" token returned to the LP (`anotherTokenQuant`) is computed strictly from the exchange's *current* `firstTokenBalance`/`secondTokenBalance` at execution time: [2](#0-1)  and re-derived again during validation at [3](#0-2) .

Those pool balances can shift between when the withdrawer signs/broadcasts the transaction and when it is actually applied on-chain, because any other account can submit an `ExchangeTransactionContract` (buy/sell) against the same `exchange_id` first — mutating `firstTokenBalance`/`secondTokenBalance` via `ExchangeCapsule.transaction` at [4](#0-3) , or another `ExchangeInjectActuator`/`ExchangeWithdrawActuator` call can land first. Since transaction ordering within a block/mempool is not controlled by the withdrawer, and `ExchangeWithdrawContract`'s protobuf definition carries only `owner_address`, `exchange_id`, `token_id`, and `quant` (confirmed by the contract-construction helper used throughout `ExchangeWithdrawActuatorTest`, e.g. [5](#0-4) ), there is no way for the LP to bound the minimum `anotherTokenQuant` (or the primary `tokenQuant`) they are willing to accept.

This is the exact bug class from the report: LPs cannot specify a minimum-received amount on a burn/withdraw path whose payout is computed from pool state that can move between submission and execution. Contrast this with `ExchangeTransactionActuator`, which does carry and enforce `expected`: [1](#0-0) .

### Impact Explanation
An LP calling `ExchangeWithdrawContract` to redeem their share of a TRC10 exchange pool can receive materially less of the "other" token than they calculated at signing time if the pool's ratio moves against them before their transaction executes (e.g., a large trade executes first, or the network is congested and the withdraw transaction lands several blocks later than intended). Because there is no `expected`/minimum field, the actuator will happily execute at whatever the degraded ratio yields, as long as `anotherTokenQuant` stays `> 0` and passes the "Not precise enough" check — both are satisfiable at a much worse price than the withdrawer intended, causing a real fund loss for that unprivileged account. This matches Medium severity per the source report: a real but conditional/bounded loss of funds that requires specific ordering of a preceding trade or injection.

### Likelihood Explanation
Any account can trigger this: an LP or exchange creator submits `ExchangeWithdrawContract`; any other unprivileged account can submit an `ExchangeTransactionContract` against the same pool that lands earlier, shifting `firstTokenBalance`/`secondTokenBalance` before the withdraw is applied in `Manager`'s block-application/transaction-processing path. No special privilege, precompile, or malicious-SR/witness behavior is required — this is reachable purely through ordinary, unprivileged transaction broadcasting and standard block processing.

### Recommendation
Add a minimum-received parameter (e.g., `expected_another_token_quant`, mirroring `ExchangeTransactionContract.expected`) to `ExchangeWithdrawContract`, and enforce it in `ExchangeWithdrawActuator.doValidate`/`execute` by rejecting the transaction if the computed `anotherTokenQuant` (and/or primary `tokenQuant`) falls below the caller-specified floor.

### Proof of Concept
1. Alice creates/joins an exchange pool with `firstTokenBalance = 100_000_000`, `secondTokenBalance = 200_000_000` (as in `InitExchangeBeforeSameTokenNameActive` at [6](#0-5) ).
2. Alice computes that withdrawing `firstTokenQuant = 100_000_000` should yield `secondTokenQuant = 200_000_000` and broadcasts `ExchangeWithdrawContract{exchangeId, tokenId=firstTokenID, quant=100_000_000}` with no way to bound the payout.
3. Before Alice's transaction is applied, Bob broadcasts and gets included first with an `ExchangeTransactionContract` that sells a large amount of the second token into the pool, shifting the ratio (via `ExchangeCapsule.transaction`, `ExchangeWithdrawActuator.java` lines 59-89).
4. When Alice's withdraw executes, `anotherTokenQuant` is recomputed from the now-degraded `secondTokenBalance`/`firstTokenBalance` ratio at execution time (`ExchangeWithdrawActuator.java` lines 74-89), yielding far less than the `200_000_000` Alice expected — and the actuator has no field to let Alice reject this outcome, unlike `ExchangeTransactionActuator`'s `expected` check (`ExchangeTransactionActuator.java` lines 217-221).

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-166)
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
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeWithdrawActuatorTest.java (L82-90)
```java
  private Any getContract(String address, long exchangeId, String tokenId, long quant) {
    return Any.pack(
        ExchangeWithdrawContract.newBuilder()
            .setOwnerAddress(ByteString.copyFrom(ByteArray.fromHexString(address)))
            .setExchangeId(exchangeId)
            .setTokenId(ByteString.copyFrom(tokenId.getBytes()))
            .setQuant(quant)
            .build());
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/ExchangeWithdrawActuatorTest.java (L109-139)
```java
    //V1
    ExchangeCapsule exchangeCapsule =
        new ExchangeCapsule(
            ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS_FIRST)),
            1,
            1000000,
            "abc".getBytes(),
            "def".getBytes());
    exchangeCapsule.setBalance(100000000L, 200000000L);
    ExchangeCapsule exchangeCapsule2 =
        new ExchangeCapsule(
            ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS_FIRST)),
            2,
            1000000,
            TRX_SYMBOL_BYTES,
            "def".getBytes());
    exchangeCapsule2.setBalance(1_000_000_000000L, 10_000_000L);
    ExchangeCapsule exchangeCapsule3 =
        new ExchangeCapsule(
            ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS_FIRST)),
            3,
            1000000,
            "abc".getBytes(),
            "def".getBytes());
    exchangeCapsule3.setBalance(903L, 737L);
    dbManager.getExchangeStore()
        .put(exchangeCapsule.createDbKey(), exchangeCapsule);
    dbManager.getExchangeStore()
        .put(exchangeCapsule2.createDbKey(), exchangeCapsule2);
    dbManager.getExchangeStore()
        .put(exchangeCapsule3.createDbKey(), exchangeCapsule3);
```
