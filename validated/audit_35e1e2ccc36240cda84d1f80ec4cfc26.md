Based on my investigation, java-tron's on-chain analog to the EGD Finance bug class (a "too simple" spot-price feed used to compute payouts, exploitable via manipulation) is the `ExchangeInject`/`ExchangeWithdraw` liquidity operations, which price the counter-token purely off the exchange pool's current spot ratio and are reachable from an anonymous account (subject only to the creator check), while the actual AMM swap path (`ExchangeTransactionActuator`) is explicitly protected against this with a caller-supplied `expected` minimum.

### Title
Unprotected spot-ratio pricing in ExchangeInject/ExchangeWithdraw enables sandwich-style value extraction from TRX/TRC10 exchange pools - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
`ExchangeInjectActuator` and `ExchangeWithdrawActuator` compute the counter-token amount for a liquidity operation directly from the exchange's current `firstTokenBalance`/`secondTokenBalance` ratio, with no caller-specified minimum/maximum bound (no analog of the `expected` field used by the swap actuator). This is exactly the bug class SlowMist flagged for EGD Finance: a price is derived from a mutable, single-transaction-manipulable on-chain balance ratio and used directly to mint/settle value, with no oracle hardening or slippage protection.

### Finding Description
`ExchangeCapsule` stores an internal Bancor-style AMM pool with `firstTokenBalance`/`secondTokenBalance` [1](#0-0) . Three actuators operate on it: `ExchangeTransactionActuator` (swap), `ExchangeInjectActuator` (add liquidity), and `ExchangeWithdrawActuator` (remove liquidity).

The swap path correctly requires the caller to supply a minimum acceptable output (`tokenExpected`), and rejects the trade if the AMM-computed output is below it: [2](#0-1) .

By contrast, `ExchangeInjectActuator.execute` computes `anotherTokenQuant` purely from the *current* pool ratio at execution time (`secondTokenBalance * tokenQuant / firstTokenBalance`), with no bound supplied by the caller: [3](#0-2) . The same pattern (ratio-derived quantity, no caller-supplied bound) recurs in `ExchangeWithdrawActuator.execute`: [4](#0-3) .

Because a swap (`ExchangeTransactionContract`) can be broadcast by any account and immediately shifts `firstTokenBalance`/`secondTokenBalance` (and hence the spot ratio) via `ExchangeCapsule.transaction`/`ExchangeProcessor.exchange` [5](#0-4) , the ratio consumed by a subsequent inject/withdraw in the very next transaction of the same block reflects that manipulated state.

The mitigating factor is that `ExchangeInjectContract`/`ExchangeWithdrawContract` both require the caller to be the exchange creator (`account[...] is not creator`) [6](#0-5) , so only the pool's creator account, not an arbitrary attacker, can trigger inject/withdraw. This significantly narrows exploitability relative to a fully unprivileged EGD-style attack, since the creator would essentially be self-sandwiching (front-running/back-running their own inject/withdraw with swaps) rather than an outside attacker draining an arbitrary victim's pool.

### Impact Explanation
If an exchange creator (or an account colluding with/controlled by the creator) swaps against their own pool to skew the ratio, then injects or withdraws at the skewed ratio, then reverses the swap, they can extract value from the pool's other liquidity contributed via prior injects — effectively minting an unbacked amount of the counter-token relative to what a fair spot price would allow. This matches "unbacked balance"/"theft of funds" impact, but is bounded to funds already held within a given TRX/TRC10 exchange pool, and requires the actor to already be the pool's creator address.

### Likelihood Explanation
Likelihood is Medium: the actuators are reachable by any signed transaction (`ExchangeTransactionContract`, `ExchangeInjectContract`, `ExchangeWithdrawContract`) and require no special network privilege (no SR/witness role), matching the "single signed transaction" reachability bar. However, the exploit is restricted to the pool's own creator account (not an arbitrary unprivileged attacker against a third party), which is a meaningfully weaker threat model than EGD Finance's fully permissionless price-manipulation attack.

### Recommendation
Add caller-supplied slippage bounds to `ExchangeInjectContract`/`ExchangeWithdrawContract` (analogous to `ExchangeTransactionContract.expected`), and validate the computed `anotherTokenQuant` against that bound before mutating balances in `ExchangeInjectActuator`/`ExchangeWithdrawActuator`. Consider also using a time-weighted or otherwise hardened price reference for these operations rather than the raw spot balance ratio.

### Proof of Concept
Not independently reproducible from static analysis alone — reproducing the extractable-value amount would require a testnet/framework harness driving a sequence of `ExchangeTransactionContract` (swap) → `ExchangeInjectContract`/`ExchangeWithdrawContract` (by the creator address) → reverse swap, and measuring net token gain versus fair-price baseline. I was not able to execute this in the current investigation and note this as an unverified but structurally supported analog based on the code paths cited above.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L106-112)
```java
  public long getFirstTokenBalance() {
    return this.exchange.getFirstTokenBalance();
  }

  public long getSecondTokenBalance() {
    return this.exchange.getSecondTokenBalance();
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
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
