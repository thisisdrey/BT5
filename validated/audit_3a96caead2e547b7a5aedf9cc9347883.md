### Title
Sandwich Attack on ExchangeInject Due to Missing Slippage Protection - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java])

### Summary
`ExchangeInjectContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — there is no user-supplied bound on the counter-token amount or on the pool ratio at the time of execution. [1](#0-0)  When `ExchangeInjectActuator` executes, the required "another token" amount is computed purely from whatever `firstTokenBalance`/`secondTokenBalance` ratio exists in the pool at that instant, with no min/max check supplied by the caller. [2](#0-1) 

### Finding Description
The `Exchange*` actuators implement a Bancor-relay AMM (`ExchangeCreateActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeTransactionActuator`) reachable directly from a signed transaction. Unlike `ExchangeTransactionContract`, which has an `expected` field enforced in `doValidate()` (`anotherTokenQuant < tokenExpected` reverts), [3](#0-2)  the `ExchangeInjectContract`/`ExchangeWithdrawContract` types provide no equivalent bound. The inject actuator's `doValidate()` and `execute()` both simply recompute `anotherTokenQuant` from the pool's *current* `firstTokenBalance`/`secondTokenBalance` ratio and require the caller to have exactly that amount of the other asset — there is no lower/upper ratio bound the LP can specify to protect against a manipulated ratio. [4](#0-3) 

This maps directly onto the reported bug class: an attacker who sees a victim's `ExchangeInjectContract` in the mempool can front-run it with an `ExchangeTransactionContract` swap that shifts `firstTokenBalance`/`secondTokenBalance` far from the "fair" market ratio, forcing the victim's `execute()`-time computed `anotherTokenQuant` to be based on the skewed ratio (donating value into the pool at a bad rate), then back-run with another swap (and/or an `ExchangeWithdrawContract`) to reclaim the skewed value, since the attacker is effectively the counterparty absorbing the mis-priced liquidity contribution. Because `transaction()` in `ExchangeCapsule` mutates the actual (not virtual) reserves directly with the Bancor-style `ExchangeProcessor`, [5](#0-4)  the ratio can be pushed arbitrarily within a single block by an attacker with sufficient capital and then restored, with the victim's inject sandwiched in between and having no way to abort or bound the exchange rate it is exposed to.

### Impact Explanation
A victim liquidity provider using `ExchangeInjectContract` can be forced to inject their token at an attacker-manipulated ratio, resulting in the victim contributing more value than intended and the attacker capturing the difference through the surrounding swap/withdraw transactions — a concrete transfer of victim funds to the attacker. This is a real (not merely theoretical) unauthorized value transfer reachable by any account issuing standard signed transactions (`ExchangeTransactionContract` + `ExchangeInjectContract` + `ExchangeWithdrawContract`), no privileged role required.

### Likelihood Explanation
Requires the attacker to observe the victim's `ExchangeInjectContract` in the mempool and place surrounding transactions (front-run swap, then back-run swap/withdraw) within the same or adjacent blocks — a standard sandwich pattern. It also requires sufficient capital relative to pool depth to move the ratio meaningfully, similar to the prerequisites in the original report (small/new exchange pool, ability to swap enough to skew the ratio). This is achievable by any active TRON account without special privileges.

### Recommendation
Add explicit slippage-bound fields (e.g., min/max acceptable counter-token amount, or min/max acceptable ratio) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, and enforce them in `ExchangeInjectActuator.doValidate()`/`execute()` (and the withdraw equivalent) the same way `tokenExpected` is enforced in `ExchangeTransactionActuator`, so that a caller-specified bound on the effective exchange ratio must hold at execution time or the transaction reverts.

### Proof of Concept
1. Attacker observes a victim's pending `ExchangeInjectContract` for exchange pool `P` (first/second token balance `B1:B2`).
2. Attacker submits an `ExchangeTransactionContract` swap that shifts `P`'s reserves to a skewed ratio `B1':B2'` (using `ExchangeCapsule.transaction`, which is unbounded by any external oracle). [5](#0-4) 
3. Victim's `ExchangeInjectContract` executes next: `ExchangeInjectActuator.execute()` computes `anotherTokenQuant` from the skewed `B1':B2'` ratio and debits the victim's account accordingly, with no ability for the victim to have bounded this ratio in their contract. [6](#0-5) 
4. Attacker submits a reverse `ExchangeTransactionContract` swap (and/or `ExchangeWithdrawContract`) to restore the ratio and extract the value effectively donated by the victim's mis-priced injection.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L60-83)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeInjectContract.getTokenId().toByteArray();
      long tokenQuant = exchangeInjectContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L179-231)
```java
    byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
    byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
    long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
    long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();

    byte[] anotherTokenID;
    long anotherTokenQuant;

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }

    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
      throw new ContractValidateException("token id is not in exchange");
    }

    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }

    if (tokenQuant <= 0) {
      throw new ContractValidateException("injected token quant must greater than zero");
    }

    BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
    BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
    BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
    long newTokenBalance;
    long newAnotherTokenBalance;

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

    if (anotherTokenQuant <= 0) {
      throw new ContractValidateException("the calculated token quant  must be greater than 0");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-220)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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
