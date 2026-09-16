## Title
Missing slippage protection in `ExchangeWithdrawContract` allows MEV sandwich attacks against exchange liquidity withdrawals - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeWithdrawContract` lets an exchange pair creator withdraw `tokenQuant` of one token, receiving a proportional `anotherTokenQuant` of the paired token computed from the *current* pool ratio. Unlike `ExchangeTransactionContract`, which carries an `expected` (minimum-received) field enforced in `ExchangeTransactionActuator.doValidate()`, `ExchangeWithdrawContract` has no such field, so there is no way for the caller to bound the amount of the paired token they will actually receive.

### Finding Description
`ExchangeTransactionContract` was hardened against price movement by including an `expected` field, checked in `ExchangeTransactionActuator`'s validation: [1](#0-0) 

`ExchangeWithdrawContract`, however, only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — no minimum/expected amount for the other side of the pair: [2](#0-1) 

In `ExchangeWithdrawActuator`, the amount of the paired token (`anotherTokenQuant`) the withdrawer will receive is derived purely from the exchange's *live* `firstTokenBalance`/`secondTokenBalance` ratio at validate/execute time: [3](#0-2) [4](#0-3) 

Because `anotherTokenQuant` is only recomputed from whatever pool state exists at the moment the transaction is validated/executed inside a block, and the contract carries no user-supplied lower bound on it, this is exactly the class of bug described in the external report: a transaction that converts a fixed input amount into a variable-priced output with no minimum-received guard, whose price can move between the time the user signs/broadcasts the transaction and the time it lands on-chain.

### Impact Explanation
An attacker observing a pending `ExchangeWithdrawContract` transaction in the mempool can broadcast their own `ExchangeTransactionContract` transactions (which are ordinary user-signed transactions with no special privilege) to shift the pool's `firstTokenBalance`/`secondTokenBalance` ratio just before the withdrawal executes, then reverse the trade afterward (classic sandwich attack). This lets the attacker extract value from the withdrawing exchange creator by causing them to receive far less of the paired token than the ratio implied when they built/signed their transaction — a concrete unauthorized transfer of value between accounts via the exchange pool, i.e., theft of funds from the withdrawing party to the attacker.

### Likelihood Explanation
Any unprivileged account can construct and broadcast an `ExchangeTransactionContract` before a target's `ExchangeWithdrawContract` is packed into a block, and TRON's transaction ordering/mempool visibility (as with any blockchain, especially with block producers ordering transactions) makes such front-running/sandwiching practical. No special permissions, keys, or node access are required — only signing and broadcasting standard transactions.

### Recommendation
Add an `expected`/minimum-amount field to `ExchangeWithdrawContract` (mirroring `ExchangeTransactionContract.expected`), and enforce it in `ExchangeWithdrawActuator.doValidate()`/`execute()` by rejecting the transaction if the computed `anotherTokenQuant` falls below the caller-specified minimum, consistent with the existing protection already present for `ExchangeTransactionContract`.

### Proof of Conceptual Scenario
1. Exchange pair `X`/`Y` has balances such that withdrawing `Q` of `X` currently yields `R` of `Y`.
2. Victim signs and broadcasts an `ExchangeWithdrawContract` withdrawing `Q` of `X`, expecting `R` of `Y`.
3. Attacker sees this pending transaction and broadcasts an `ExchangeTransactionContract` that shifts the pool ratio unfavorably for the victim before the withdrawal is packed.
4. The victim's withdrawal executes against the now-skewed ratio in `ExchangeWithdrawActuator.execute()` (lines 74-89), yielding `R' < R` of `Y`, with no `expected` field to reject the trade.
5. Attacker reverses their trade afterward, capturing the difference extracted from the victim.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** Tron protobuf protocol document.md (L1403-1420)
```markdown
     - message `ExchangeWithdrawContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to withdraw.
    
       `quant`: token amount to withdraw.
    
      ```java
      message ExchangeWithdrawContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
      }
      ```
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L63-89)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeWithdrawContract.getTokenId().toByteArray();
      long tokenQuant = exchangeWithdrawContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-247)
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

    } else {
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigSecondTokenBalance).longValueExact();
```
