## Title
ExchangeInjectContract / ExchangeWithdrawContract are missing slippage protection, enabling sandwich-frontrunning of liquidity operations — (`actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeTransactionContract` (a bancor-style swap on TRON's built-in `Exchange`) has a caller-supplied `expected` field that is validated against the computed output before execution, giving the sender slippage protection. [1](#0-0) [2](#0-1) 

By contrast, `ExchangeInjectContract` and `ExchangeWithdrawContract` — the contracts used to add/remove liquidity from the same on-chain AMM pool — have **no equivalent minimum/maximum bound**. The counterpart-token amount (`anotherTokenQuant`) is derived purely from the pool's *current* balances at execution time, with no way for the sender to bound the acceptable ratio. This is the direct analog of the reported `_swapTokens`/`_addLiquidity` issue where a missing minimum-output check allows the pool ratio to be manipulated between transaction submission and execution.

### Finding Description
In `ExchangeInjectActuator.doValidate()`, the amount of the paired token required is computed strictly from the exchange's live balances: [3](#0-2) 
There is no field in `ExchangeInjectContract` (`owner_address`, `exchange_id`, `token_id`, `quant`) that lets the caller specify a bound on `anotherTokenQuant`. [4](#0-3) 

Similarly, `ExchangeWithdrawActuator.doValidate()` computes `anotherTokenQuant` from the live pool ratio and only checks "precision"/sufficiency, never a caller-specified minimum-received bound: [5](#0-4) 
`ExchangeWithdrawContract` also lacks an `expected` field. [6](#0-5) 

Because any account can execute `ExchangeTransactionContract` against the same exchange (the swap only requires token balance, not creator privilege), an attacker can:
1. Observe a pending `ExchangeInjectContract` (or `ExchangeWithdrawContract`) from the exchange creator in the mempool/block being built.
2. Front-run it with a large `ExchangeTransactionContract` swap that skews `firstTokenBalance`/`secondTokenBalance` via the bancor-style curve in `ExchangeCapsule.transaction`. [7](#0-6) 
3. Let the victim's inject/withdraw execute against the manipulated ratio, since `anotherTokenQuant` is recomputed fresh from whatever the pool balances are at execution time.
4. Back-run with a reverse swap to restore the ratio and capture the value extracted from the victim's mis-priced liquidity operation.

### Impact Explanation
An exchange creator submitting `ExchangeInjectContract` can be forced to deposit a disproportionate amount of one token relative to the manipulated pool ratio, effectively transferring value to the attacker who executes the sandwich swaps — an unauthorized extraction of funds analogous to the reported harvest-frontrunning bug. The same applies in reverse for `ExchangeWithdrawContract`, where the creator can receive less of the paired token than the pool's "fair" (un-manipulated) ratio would imply. Since `Exchange` balances hold real TRX/TRC10 value, this results in concrete loss of funds for the account performing the injection/withdrawal.

### Likelihood Explanation
Both actuators are reachable via ordinary signed transactions from any account (the exchange creator for inject/withdraw, any account for swaps), require no special node privileges, and the exploit only needs the attacker to observe pending transactions and submit surrounding transactions in the same or adjacent blocks — a standard sandwich/front-run pattern already demonstrated as exploitable against this exact contract family in `ExchangeTransactionActuator` (which is why that one contract alone received an `expected` slippage guard).

### Recommendation
Add a caller-specified bound to `ExchangeInjectContract` and `ExchangeWithdrawContract` (e.g., `expected_another_token_quant` for inject, `expected` minimum for withdraw), and validate the computed `anotherTokenQuant` against it in `doValidate()`/`execute()`, mirroring the existing protection in `ExchangeTransactionActuator`.

### Proof of Concept
1. Exchange creator broadcasts `ExchangeInjectContract{exchangeId, tokenId=A, quant=X}` expecting `anotherTokenQuant` for token B based on the current ratio.
2. Attacker sees this in the mempool and broadcasts (with higher fee/priority) a large `ExchangeTransactionContract` swap that shifts the A:B pool ratio unfavorably for the pending inject.
3. Block is produced with attacker's swap ordered before the creator's inject; `ExchangeInjectActuator.execute()` recomputes `anotherTokenQuant` from the skewed balances (`actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java:71-83`), causing the creator to deposit token B at an unfavorable ratio.
4. Attacker submits a reverse `ExchangeTransactionContract` swap restoring the ratio and pocketing the difference extracted from the creator's injection.

### Citations

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L17-22)
```text
message ExchangeInjectContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}
```

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L24-29)
```text
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-227)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenID = secondTokenID;
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divide(bigFirstTokenBalance).longValueExact();
      newTokenBalance = addExact(firstTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(secondTokenBalance, anotherTokenQuant);
    } else {
      anotherTokenID = firstTokenID;
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divide(bigSecondTokenBalance).longValueExact();
      newTokenBalance = addExact(secondTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(firstTokenBalance, anotherTokenQuant);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-244)
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
