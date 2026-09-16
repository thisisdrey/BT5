### Title
Front-Running/Sandwich of ExchangeInject and ExchangeWithdraw Due to Missing Slippage Protection - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
`ExchangeInjectContract` and `ExchangeWithdrawContract` compute the counter-token amount from the exchange's *live* pool ratio (`firstTokenBalance`/`secondTokenBalance`) at execution time, with no minimum-received / maximum-slippage field in the protobuf message. This is the same bug class as the reported Unitas `SwapFunctions` issue: a rate calculated from mutable pool state at execution time, with no user-supplied bound, is manipulable by an attacker who reorders/inserts a transaction before the victim's.

### Finding Description
`ExchangeTransactionContract` (the actual swap, callable by any account) was hardened with a caller-supplied `expected` field [1](#0-0) , enforced in `ExchangeTransactionActuator.doValidate` via `if (anotherTokenQuant < tokenExpected) throw ...` [2](#0-1) .

However, `ExchangeInjectContract` and `ExchangeWithdrawContract` have no equivalent bound field — only `owner_address`, `exchange_id`, `token_id`, `quant` [3](#0-2) . Both actuators derive `anotherTokenQuant` purely from the pool's current `firstTokenBalance`/`secondTokenBalance` ratio at the moment of execution:
- `ExchangeInjectActuator.execute`: `anotherTokenQuant = floorDiv(multiplyExact(secondTokenBalance, tokenQuant), firstTokenBalance)` (or the symmetric branch) [4](#0-3) .
- `ExchangeWithdrawActuator.execute`: same ratio-based calculation using `BigInteger` division [5](#0-4) .

The `doValidate` "precision" checks in `ExchangeWithdrawActuator` (the "Not precise enough" comparisons) only verify that the client's off-chain estimate is close to the on-chain recomputed value at validation time; they do not bound against pool-ratio movement caused by an intervening transaction, since both validate and execute independently recompute from the *current* store state [6](#0-5) .

Both Inject and Withdraw are restricted to the exchange's `creatorAddress` [7](#0-6) [8](#0-7) . But `ExchangeTransactionContract` (the swap) is callable by any unprivileged account and directly mutates `firstTokenBalance`/`secondTokenBalance` via `ExchangeCapsule.transaction` [9](#0-8) , using the AMM-style bonding-curve `ExchangeProcessor`/`SafeExchangeProcessor` [10](#0-9) . Since block producers order transactions within a block and multiple transactions can be included in the same block, an unprivileged actor can broadcast an `ExchangeTransactionContract` swap timed to execute immediately before a known/pending `ExchangeInjectContract` or `ExchangeWithdrawContract`, skewing the pool ratio so that the creator's inject/withdraw settles at a distorted rate, and then reverse the swap afterward — a classic sandwich attack extracting value from the creator's liquidity operation.

### Impact Explanation
A successful sandwich shifts value from the exchange creator (and indirectly other liquidity in the pool) to the attacker, resulting in the creator receiving a smaller `anotherTokenQuant` on withdraw, or contributing an unfavorable ratio on inject — i.e., unauthorized extraction of value/theft of funds from a specific victim's on-chain operation, without requiring any privileged role from the attacker (a plain `ExchangeTransactionContract` swap is enough).

### Likelihood Explanation
Moderate-to-high: it requires only that an attacker observe a pending `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction (visible in mempool prior to block inclusion) and race an ordinary swap transaction ahead of it and another one after — both broadcastable by any TRX/TRC10 holder with no special permissions. There is no explicit reordering-resistance mechanism (no `expected`/min-out bound) protecting Inject/Withdraw the way `ExchangeTransactionContract` is protected.

### Recommendation
Add a caller-supplied bound to `ExchangeInjectContract` and `ExchangeWithdrawContract` (e.g., a `minExpected`/`maxSlippage` field, mirroring `ExchangeTransactionContract.expected`), and enforce it in `ExchangeInjectActuator.execute`/`doValidate` and `ExchangeWithdrawActuator.execute`/`doValidate` against the ratio recomputed at execution time, rejecting the transaction if the recomputed `anotherTokenQuant` falls outside the caller's tolerance.

### Proof of Concept
1. Exchange creator broadcasts `ExchangeWithdrawContract` withdrawing `tokenQuant` of `firstTokenID`, expecting `anotherTokenQuant` computed from current `firstTokenBalance`/`secondTokenBalance`.
2. Before it is packed into a block, an unprivileged attacker broadcasts an `ExchangeTransactionContract` swap that shifts `firstTokenBalance`/`secondTokenBalance` (via `ExchangeCapsule.transaction`), then arranges (via fee/ordering) for it to be included ahead of the creator's withdraw in the same or an earlier block.
3. The `ExchangeWithdrawActuator.execute` recomputes `anotherTokenQuant` from the now-skewed balances [5](#0-4) , giving the creator a worse rate than expected at submission time, with no on-chain check preventing it.
4. The attacker submits a second `ExchangeTransactionContract` swap immediately after, reverting the pool ratio and capturing the value difference — since neither `ExchangeInjectContract` nor `ExchangeWithdrawContract` carries any minimum-output guard field [3](#0-2) , this cannot be prevented on-chain.

### Citations

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L17-29)
```text
message ExchangeInjectContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}

message ExchangeWithdrawContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}
```

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L31-37)
```text
message ExchangeTransactionContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
  int64 expected = 5;
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-176)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L77-89)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-182)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-167)
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L41-45)
```java
  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```
