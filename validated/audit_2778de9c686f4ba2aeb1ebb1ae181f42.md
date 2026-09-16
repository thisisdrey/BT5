### Title
Front-running griefing of `ExchangeTransactionContract` via the fixed `ExchangeBalanceLimit` check causes legitimate trader transactions to revert - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java])

### Summary
`ExchangeTransactionActuator.doValidate()` rejects a trade whenever the post-trade balance of the traded token in the bancor-style exchange would exceed a global, fixed cap (`dynamicStore.getExchangeBalanceLimit()`). Because this check is computed against the exchange's *current* token balances at validation time, any unprivileged account can front-run a pending, legitimate `ExchangeTransactionContract` with its own small trade in the same direction to push the pool balance close to the limit, causing the victim's transaction to revert with `"token balance must less than " + balanceLimit"`.

### Finding Description
In `doValidate()`: [1](#0-0) 
the code reads the live `firstTokenBalance`/`secondTokenBalance` from the `ExchangeCapsule`, adds the trader's `tokenQuant`, and reverts if the sum exceeds `balanceLimit`. These balances are mutated by every successful `ExchangeTransactionActuator.execute()` call via `ExchangeCapsule.transaction()`: [2](#0-1) 
Since `ExchangeTransactionContract` is a permissionless, broadcastable transaction type reachable by any signed account, an attacker observing the mempool can submit a trade in the same token/direction as a pending victim transaction with higher energy/bandwidth priority (or via miner collusion) so that it lands first. This raises the pool's balance of that token, and the victim's originally-valid `tokenQuant` now pushes the sum over `balanceLimit`, causing `ContractValidateException("token balance must less than " + balanceLimit)` and reverting/failing the trader's transaction — exactly the "attacker spends a tiny amount to grieve/DoS a trader's larger, otherwise-valid operation" pattern described in the source report, where the check point is a strict inequality on a value the attacker can nudge over the threshold with a small state change.

This differs from and is stronger than ordinary slippage/sandwich risk (the `tokenExpected` check at line 219, which the trader can tune) because `balanceLimit` is a fixed protocol constant the trader cannot compensate for by adjusting slippage tolerance — any successful front-run that crosses the cap unconditionally reverts the victim regardless of the trader's specified expected amount.

### Impact Explanation
An attacker can reliably cause a targeted trader's legitimate `ExchangeTransactionContract` to fail whenever the pool's token balance is near `ExchangeBalanceLimit`, wasting the trader's bandwidth/energy fee and blocking their ability to execute large, otherwise valid, exchange trades. This is a griefing/denial-of-service impact against normal exchange operation on-chain, reachable by any unprivileged account issuing ordinary transactions — no special privileges, malicious SR/witness status, or network-level attack required.

### Likelihood Explanation
Likelihood is moderate: it requires the attacker to observe a pending trade (via mempool/broadcast monitoring) and land a transaction ahead of it in the same block or an earlier block, which is a standard, well-understood transaction-ordering capability on TRON. It is most impactful near pools whose balance is already close to `ExchangeBalanceLimit`, which narrows applicability somewhat but does not eliminate exploitability for actively traded exchange pairs.

### Recommendation
Consider changing the fixed absolute cap into a check that degrades gracefully instead of hard-reverting, e.g., cap `tokenQuant`/the resulting trade size to the remaining headroom under `balanceLimit` rather than rejecting the whole transaction, or make the limit check advisory (skip/clip) similarly to the "solution one/two" proposed in the analogous splits-swapper report — clip the executed amount to what still fits under the limit and proportionally scale the counter-token amount, instead of throwing `ContractValidateException`.

### Proof of Concept
1. Exchange pool for tokens A/B has `firstTokenBalance` close to `ExchangeBalanceLimit` (e.g., `balanceLimit - 100`).
2. Trader broadcasts `ExchangeTransactionContract` selling token A with `tokenQuant = 50` (valid at broadcast time, since `balanceLimit - 100 + 50 < balanceLimit`).
3. Attacker observes the pending transaction and front-runs with their own `ExchangeTransactionContract` selling `tokenQuant = 60` of token A, which succeeds and increases the pool's first token balance to `balanceLimit - 40`.
4. The victim's transaction is then processed; `doValidate()` recomputes `tokenBalance = (balanceLimit - 40) + 50 = balanceLimit + 10 > balanceLimit`, and the transaction reverts with `"token balance must less than " + balanceLimit"` — see the check at [1](#0-0) 
5. The trader's fee/bandwidth is consumed with no trade executed, and the attack can be repeated to prevent the trader from ever using the exchange while the pool balance sits near the cap.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L199-205)
```java
    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
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
