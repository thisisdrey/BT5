This maps directly onto the reported bug class. TRON's built-in Bancor-style token exchange (`ExchangeWithdrawContract`) removes liquidity from a pool and returns a proportional amount of the paired token, exactly like Curve `remove_liquidity_one_coin`, but with no way for the caller to specify a minimum acceptable output.

### Title
`ExchangeWithdrawActuator` removes token-pair liquidity with no slippage/minimum-output protection - (`actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
TRON's built-in Bancor-formula token exchange lets any account create a token pair pool (`ExchangeCreateContract`) and then inject, withdraw, or trade against it. Unlike `ExchangeTransactionContract`, which carries an `expected` field letting the caller specify a minimum output for trades, `ExchangeWithdrawContract` has no equivalent minimum-output/slippage field. Withdrawals compute the counterpart-token payout purely from the current on-chain pool ratio at execution time, exactly mirroring the reported Curve `remove_liquidity_one_coin(..., min_amount=0)` pattern.

### Finding Description
`ExchangeWithdrawContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — there is no `expected`/minimum field: [1](#0-0)  By contrast, `ExchangeTransactionContract` explicitly includes an `expected` "minimum number of tokens" field precisely to guard against price movement between contract creation and execution: [2](#0-1) 

In `ExchangeWithdrawActuator.execute`, the amount of the counterpart token (`anotherTokenQuant`) returned to the withdrawer is computed solely from the exchange pool's current `firstTokenBalance`/`secondTokenBalance` ratio at the moment the transaction executes, with no comparison against any caller-supplied floor: [3](#0-2)  The `doValidate` method only checks that the token exists in the exchange, that the exchange isn't drained, that `anotherTokenQuant > 0`, and a fixed-point rounding ("Not precise enough") sanity check — none of which bound how far the ratio can move due to price manipulation before withdrawal: [4](#0-3) 

Because any account can trade against the same pool via `ExchangeTransactionActuator` (which mutates `firstTokenBalance`/`secondTokenBalance` in the very same pool) immediately before and after the victim's withdrawal transaction lands in a block, an attacker can sandwich the withdrawal: skew the pool ratio unfavorably right before the withdraw executes, then reverse the skew immediately after, extracting the difference from the victim's payout: [5](#0-4) 

### Impact Explanation
A victim who submits `ExchangeWithdrawContract` to pull liquidity out of a pool they created/injected into can have their received counterpart-token amount reduced arbitrarily (bounded only by the attacker's available capital/flash-style same-block trades and the fixed transaction fee), resulting in direct, quantifiable theft of TRC10/TRX value at withdrawal time. This is a fund-loss issue for any account using the on-chain token exchange feature, reachable purely through ordinary signed transactions (`ExchangeTransactionContract` + `ExchangeWithdrawContract`) with no privileged access required.

### Likelihood Explanation
Any unprivileged account can broadcast `ExchangeTransactionContract` transactions against a target exchange pool and observe pending `ExchangeWithdrawContract` transactions in the mempool to construct a sandwich (front-run / back-run) around them, exactly analogous to the sandwich pattern described in the original report for Curve pools. The likelihood scales with pool liquidity depth and mempool visibility, both of which are attacker-favorable on public chains.

### Recommendation
Add an `expected`/minimum-output field to `ExchangeWithdrawContract` (mirroring the field already present on `ExchangeTransactionContract`), and enforce in `ExchangeWithdrawActuator.doValidate`/`execute` that the computed `anotherTokenQuant` is not less than the caller-specified minimum, causing the transaction to fail validation/execution otherwise.

### Proof of Concept
1. Attacker observes a pending `ExchangeWithdrawContract` transaction (owner withdrawing `tokenQuant` of `firstTokenID` from `exchange_id`) in the mempool.
2. Attacker front-runs with an `ExchangeTransactionContract` selling a large amount of `secondTokenID` into the same `exchange_id`, shifting `firstTokenBalance`/`secondTokenBalance` so that the ratio used in `ExchangeWithdrawActuator.execute` (lines 74-89) yields a much smaller `anotherTokenQuant` for the victim's withdrawal.
3. Victim's `ExchangeWithdrawContract` executes at the skewed ratio, receiving far less of `secondTokenID` than expected, since no minimum-output check exists (`actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java` lines 205-243).
4. Attacker back-runs with another `ExchangeTransactionContract` reversing the initial trade, restoring the pool ratio and capturing the difference extracted from the victim.

### Citations

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

**File:** Tron protobuf protocol document.md (L1422-1442)
```markdown
     - message `ExchangeTransactionContract`
    
       `owner_address`: address of owner.
    
       `exchange_id`: token pair id.
    
       `token_id`: token id to sell.
    
       `quant`: token amount to sell.
    
       `expected`: expected minimum number of tokens.
    
      ```java
      message ExchangeTransactionContract {
          bytes owner_address = 1;
          int64 exchange_id = 2;
          bytes token_id = 3;
          int64 quant = 4;
          int64 expected = 5;
      }
      ```
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-90)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L205-243)
```java
    if (tokenQuant <= 0) {
      throw new ContractValidateException("withdraw token quant must greater than zero");
    }

    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-75)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }
```
