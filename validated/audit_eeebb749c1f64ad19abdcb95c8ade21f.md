### Title
Sandwich Attack Vulnerability in Bancor-style `ExchangeTransactionActuator` due to Ineffective Slippage Parameter - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java`)

### Summary
The on-chain TRX/TRC10 exchange (`ExchangeTransactionContract` / `ExchangeTransactionActuator`) uses a bancor-style bonding-curve AMM (`ExchangeCapsule.transaction()` → `ExchangeProcessor`/`SafeExchangeProcessor`) and relies solely on a user-supplied `expected` field as slippage protection, exactly analogous to the `baseMinAmount`/`quoteMinAmount` parameters flagged in the external `GSPFunding.sol::sellShares()` report. The only guard is `tokenExpected > 0` and a post-hoc check `anotherTokenQuant >= tokenExpected`, both of which are trivially satisfiable and provide no real defense against a sandwich attack.

### Finding Description
`ExchangeTransactionActuator.doValidate()` requires only that `tokenExpected > 0` and that the AMM-computed output is `>= tokenExpected`: [1](#0-0) 

Because `expected` can legally be set to `1` (the minimal allowed positive value), any transaction will pass validation as long as the attacker leaves the victim with at least 1 unit of the destination token — an essentially meaningless bound. There is no deadline field, no maximum price-impact cap, and no TWAP/oracle reference check anywhere in the actuator or in `ExchangeCapsule`/`ExchangeProcessor`: [2](#0-1) [3](#0-2) 

The pool state (`firstTokenBalance`/`secondTokenBalance`) is mutated in-place by every trade and persisted via `Commons.putExchangeCapsule`, so an attacker who observes a pending `ExchangeTransactionContract` in the mempool can:
1. Front-run with a large sell of the same `tokenID` to push the bonding-curve price against the victim.
2. Let the victim's transaction execute — it still satisfies `anotherTokenQuant >= tokenExpected` because `expected` was set low (as in normal wallet UX, e.g. `1` as seen throughout the test suite, or 0-adjacent values), so it does not revert.
3. Back-run with a reverse trade to capture the price difference, extracting value directly from the victim's execution.

This exactly matches the reported bug class: a slippage/min-amount parameter that is user-settable to an arbitrarily low value and therefore fails to prevent adverse price manipulation around the victim's transaction.

### Impact Explanation
Any unprivileged account can craft and broadcast an `ExchangeTransactionContract` (reachable via `wallet/exchangetransaction`) with a low `expected` value, as most wallet integrations do to avoid unnecessary reverts. An attacker monitoring the mempool can sandwich such transactions on the on-chain TRX/TRC10 exchange pools, extracting value from ordinary users — a direct, concrete theft of funds via unauthorized value transfer from victim to attacker, achieved purely through transaction ordering around a normal signed transaction. This satisfies the "concrete unauthorized account operation / theft of funds" bar for Medium/High severity.

### Likelihood Explanation
Likelihood is high: the vulnerable code path is a first-class production feature (`ExchangeTransactionContract`) reachable by any account with sufficient balance/asset via `wallet/exchangetransaction`, no special permission is required, and typical `expected` values used by clients (as also reflected in the actuator's own test suite, e.g. passing `1` as `expected`) leave essentially the whole bonding-curve slippage exposed to manipulation. No p2p, malicious-SR, or privileged capability is needed — sandwiching only requires transaction visibility and standard block-producer ordering behavior.

### Recommendation
- Add a mandatory `deadline`/expiration parameter to `ExchangeTransactionContract` to bound execution window.
- Consider adding a maximum allowable price-impact percentage check in `ExchangeTransactionActuator.doValidate()` relative to the pool's pre-trade price, rather than relying purely on a client-supplied absolute `expected` amount.
- Document/enforce that `expected` be computed from a fresh on-chain price query immediately before broadcast, and consider providing an interface for tighter slippage bounds (e.g., basis-point tolerance) instead of an absolute token amount that is easy to under-specify.

### Proof of Concept
1. Attacker observes a pending `ExchangeTransactionContract` from victim: sell `Q` of `tokenID` on `exchange_id=E`, `expected=1`.
2. Attacker submits `ExchangeTransactionContract` selling a large amount `Q_a` of the same `tokenID` on the same `exchange_id`, ordered before the victim's transaction (e.g., higher fee/priority or same-block ordering advantage), shifting `firstTokenBalance`/`secondTokenBalance` per `ExchangeCapsule.transaction()`'s bonding curve math.
3. Victim's transaction executes against the now-worse price; since `anotherTokenQuant` still exceeds `expected=1`, `ExchangeTransactionActuator.doValidate()` passes at line 219-221 and the trade completes at a degraded rate.
4. Attacker immediately submits a reverse `ExchangeTransactionContract` selling the token just bought back, capturing the price differential created by steps 2–3, net of the AMM curve fee, at the victim's expense.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L186-221)
```java
    if (tokenQuant <= 0) {
      throw new ContractValidateException("token quant must greater than zero");
    }

    if (tokenExpected <= 0) {
      throw new ContractValidateException("token expected must greater than zero");
    }

    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    long tokenBalance = (Arrays.equals(tokenID, firstTokenID) ? firstTokenBalance
        : secondTokenBalance);
    tokenBalance = addExact(tokenBalance, tokenQuant);
    if (tokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }

    if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(tokenQuant, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(tokenID, tokenQuant, dynamicStore)) {
        throw new ContractValidateException("token balance is not enough");
      }
    }

    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-169)
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
