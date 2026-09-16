### Title
Exchange pool permanently freezes remaining token balance once one side's balance reaches zero, with no actuator able to recover it - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java, ExchangeWithdrawActuator.java, ExchangeTransactionActuator.java)

### Summary
This is the same bug class as the reported `StreamingFeeModule` issue: a state variable can legitimately reach a boundary value (zero) through normal contract calls, but every subsequent transaction path that would otherwise let a user act on that state unconditionally reverts once the boundary is hit — permanently freezing funds with no recovery path, even for the account (the exchange creator) that would be expected to be able to fix it.

### Finding Description
Every one of the three `Exchange*Actuator`s (`ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeTransactionActuator`) validates the token pair balances before performing any operation, and unconditionally rejects the transaction if either side of the pool is zero: [1](#0-0) [2](#0-1) [3](#0-2) 

All three checks throw the identical `"Token balance in exchange is equal with 0, the exchange has been closed"` error and there is no other actuator that can set a nonzero balance directly — `ExchangeInjectActuator` also requires the caller to be the exchange creator, so even the creator cannot inject fresh liquidity once one side is zero: [4](#0-3) 

`ExchangeCapsule.transaction`, invoked by `ExchangeTransactionActuator`, computes the new balances via a bonding-curve pricing model (`ExchangeProcessor`/`SafeExchangeProcessor`) driven purely by attacker-controlled `sellTokenQuant`, and stores whatever balances result without any floor above zero other than a `<0` check in hardened mode: [5](#0-4) 

Because the pricing math is a floating-point / BigDecimal approximation of an asymptotic curve, a sufficiently large sell order (or accumulated rounding through repeated small trades near the curve's tail) can drive one side of the pool down to exactly `0`. Once that happens:
- `ExchangeTransactionActuator` can never execute another trade on this pair (line 194-197 above always fires first).
- `ExchangeWithdrawActuator` can never let a holder withdraw the *other* token's remaining balance out of the pool (line 209-212 above fires first) — this is the direct funds-freezing consequence, analogous to `StreamingFeeModule.accrueFee` reverting and blocking any fee-rate change forever.
- `ExchangeInjectActuator` can never top the pool back up (line 200-203, plus the creator-only restriction above).

This mirrors the reported bug exactly: a legitimately reachable zero state becomes a permanent dead-end because every code path that should let the state be corrected instead treats zero as a terminal "closed" condition and reverts unconditionally.

### Impact Explanation
Any non-zero balance remaining on the other side of the exchange pair becomes permanently unwithdrawable — this is a direct, non-recoverable freezing of user/creator funds locked inside the `Exchange`/`ExchangeV2` capsule. No governance, superrepresentative, or admin transaction type exists to force-set a nonzero exchange balance or bypass the check, so the funds are stuck for the lifetime of the chain.

### Likelihood Explanation
This is reachable by any account through the ordinary, unprivileged `ExchangeTransactionContract` broadcast path with attacker-controlled `quant`/`expected` fields — no special privileges, precompiles, or SR cooperation are required. Reaching exact zero requires hitting a specific numeric edge in the pricing curve, which lowers likelihood somewhat, but it is deterministic given known pool reserves and is a pure function of attacker-supplied `tokenQuant`, making it computable/targetable off-chain before submission.

### Recommendation
Do not treat `balance == 0` as an unconditional terminal "closed" state for all three actuators. At minimum:
- Allow `ExchangeInjectActuator` (restricted to the creator) to re-seed a pool whose balance has reached zero, mirroring the PR#118 fix pattern of allowing state correction instead of reverting.
- In `ExchangeCapsule.transaction`, disallow trades that would drive either reserve to exactly `0` (extend the existing hardened `< 0` check in `ExchangeCapsule.java` lines 160-162 to also reject `== 0`), preventing the dead-end state from being reached at all.

### Proof of Concept
1. Create an exchange pair via `ExchangeCreateActuator` with reserves `(A, B)`.
2. Submit an `ExchangeTransactionContract` selling token A with a `quant` sized (using the known bonding-curve formula in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply`) such that the computed `buyTokenQuant` equals exactly `secondTokenBalance` (token B reserve), driving `newSecondTokenBalance` to `0` in `ExchangeCapsule.transaction`.
3. Observe the transaction succeeds and is committed (`ExchangeTransactionActuator.execute`), leaving `firstTokenBalance > 0` and `secondTokenBalance == 0`.
4. Attempt an `ExchangeWithdrawContract` to withdraw the remaining `firstTokenBalance` → `doValidate` throws `"Token balance in exchange is equal with 0, the exchange has been closed"` and the transaction always fails.
5. Attempt an `ExchangeInjectContract` from the creator address to restore `secondTokenBalance` → `doValidate` throws the same error, confirming there is no path to ever unfreeze the remaining `firstTokenBalance`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L200-203)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L209-212)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L194-197)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
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
