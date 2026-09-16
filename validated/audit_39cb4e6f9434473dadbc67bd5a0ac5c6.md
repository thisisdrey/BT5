I have sufficient evidence to conclude this analysis.

### Title
TRC10 Bancor-relay Exchange pool prices can be manipulated by a single transaction due to absence of price-impact/oracle protection - (File: chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java)

### Summary
Java-tron's built-in TRC10 `Exchange` feature is an on-chain AMM (Bancor-relay style) where the spot exchange rate between two tokens is derived purely from the pool's two on-chain reserve balances at the moment of the transaction, with no external price oracle, no TWAP, and no minimum-liquidity enforcement. Any user can call `ExchangeCreateContract` to spin up a pool with an arbitrarily small reserve (as low as the smallest valid balances allowed by validation) and then use `ExchangeTransactionContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract` to move the reserve ratio arbitrarily within a single transaction.

### Finding Description
The reachable actuator chain is:
- `ExchangeCreateActuator.doValidate()` only rejects balances `<= 0` and enforces an upper bound `getExchangeBalanceLimit()`; there is no minimum-liquidity floor. [1](#0-0) 
- The instantaneous swap price is computed exclusively from the two live pool reserves (`firstTokenBalance`, `secondTokenBalance`) inside `ExchangeCapsule.transaction()`, which delegates to `ExchangeProcessor.exchange()` / `SafeExchangeProcessor.exchange()` — a Bancor-relay formula with no time-weighting, no circuit breaker, and no dependence on any external price feed. [2](#0-1) [3](#0-2) 
- `ExchangeTransactionActuator.execute()` calls this pricing function directly against current on-chain reserves in the very same transaction that alters them, so the resulting price is entirely attacker-controlled within one atomic operation (no flash-loan resistance, no minimum-holding period). [4](#0-3) 
- All four operations (`ExchangeCreate`, `ExchangeInject`, `ExchangeWithdraw`, `ExchangeTransaction`) are exposed as unauthenticated, unprivileged gRPC/HTTP endpoints reachable by any broadcaster. [5](#0-4) 

This mirrors the reported bug class exactly: a thinly-capitalized pool whose price is a pure function of its own reserves can be pushed to an extreme ratio by a single large trade, then read/relied upon by anything that treats the pool's balance ratio as a price oracle (e.g., another actuator, a proposal, or off-chain integrations that call `GetExchangeById`/reserve getters expecting a fair market price).

### Impact Explanation
Because pool creation has no minimum liquidity requirement and the AMM curve reacts immediately to reserve changes with no time-weighting, an attacker can create a shallow pool (e.g., TRX vs. a TRC10 token) or target an existing shallow one, and within a single transaction skew `firstTokenBalance`/`secondTokenBalance` to make the implied exchange rate arbitrary (e.g., driving one token's price toward the minimum representable unit or an extreme multiple). Any protocol or user that treats `ExchangeCapsule` reserves as a fair-value oracle for the paired token would be misled, enabling theft or unbacked value extraction through follow-on operations executed in the same or an adjacent transaction. This matches "Medium" severity: it requires capital (or flash-loan-equivalent large balances of TRX/TRC10) and depends on downstream consumers trusting the exchange price, but the underlying computation path itself provides no resistance whatsoever (no TWAP, no minimum liquidity, no oracle deviation check).

### Likelihood Explanation
Likelihood is Medium: no privileged role is required — `ExchangeCreateContract`, `ExchangeInjectContract`, `ExchangeWithdrawContract`, and `ExchangeTransactionContract` are all standard signed transactions any account can broadcast. The main precondition is sufficient token/TRX balance to move a shallow pool's ratio, which is realistic for small pools (this is precisely the scenario the referenced report describes for WBGL). The actual exploitation cost/likelihood further depends on whether any consumer of the pool price lacks its own TWAP/oracle-deviation guard, which is outside java-tron's control, but the chain-side pricing mechanism supplies no protection on its own.

### Recommendation
- Enforce a minimum reserve/liquidity threshold in `ExchangeCreateActuator.doValidate()` in addition to the existing upper `balanceLimit` check.
- Consider adding a maximum price-impact-per-transaction cap or a time-weighted price mechanism at the `ExchangeCapsule`/`ExchangeProcessor` level for consumers that need manipulation resistance.
- Document clearly (and enforce where feasible) that `ExchangeCapsule` reserve ratios are not manipulation-resistant oracles, so on-chain and off-chain consumers must not rely on them as sole price references without their own safeguards (e.g., minimum liquidity checks, deviation limits, multi-block TWAP).

### Proof of Concept
1. Attacker calls `ExchangeCreate` (or targets an existing exchange with small `firstTokenBalance`/`secondTokenBalance`, which passes validation as long as both are `> 0` and below `balanceLimit`). [1](#0-0) 
2. Attacker broadcasts `ExchangeTransactionContract` selling a large amount of one side of the pair into the shallow pool; `ExchangeCapsule.transaction()` recomputes `buyTokenQuant` purely from the current (attacker-influenced) `firstTokenBalance`/`secondTokenBalance`, moving the effective price to an extreme within the same transaction. [2](#0-1) 
3. Any downstream logic reading the pool's reserve ratio as a price (on-chain or off-chain) during or immediately after this transaction observes the manipulated price, with no TWAP or oracle-deviation safeguard present in the java-tron exchange code path to prevent this. [3](#0-2)

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L41-45)
```java
  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-69)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```

**File:** protocol/src/main/protos/api/api.proto (L178-188)
```text
  rpc ExchangeCreate (ExchangeCreateContract) returns (TransactionExtention) {
  }

  rpc ExchangeInject (ExchangeInjectContract) returns (TransactionExtention) {
  }

  rpc ExchangeWithdraw (ExchangeWithdrawContract) returns (TransactionExtention) {
  }

  rpc ExchangeTransaction (ExchangeTransactionContract) returns (TransactionExtention) {
  }
```
