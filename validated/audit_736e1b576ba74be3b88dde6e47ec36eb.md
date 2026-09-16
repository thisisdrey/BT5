### Title
Missing slippage/minimum-output protection in `ExchangeWithdrawContract` allows front-running of exchange (Bancor-style AMM) liquidity withdrawals - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java])

### Summary
`ExchangeWithdrawContract`, unlike the sibling `ExchangeTransactionContract`, has no `expected`/minimum-output field. The `ExchangeWithdrawActuator` computes the paired-token amount to return to the exchange creator entirely from the pool's live balances at execution time, with no way for the caller to bound the acceptable output. This exposes the withdrawing party to slippage/front-running exactly as described in the referenced report, mirroring `Funding.sol`'s `requestWithdraw`/`executeWithdraw` lacking a slippage parameter.

### Finding Description
The protobuf definition of `ExchangeWithdrawContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — no expected/minimum amount field: [1](#0-0) 

By contrast, `ExchangeTransactionContract` (the trade function) explicitly includes an `expected` field used as slippage protection, and `ExchangeTransactionActuator.doValidate()` enforces `anotherTokenQuant < tokenExpected` fails validation: [2](#0-1) 

`ExchangeWithdrawActuator`, however, derives the paired-token payout purely from the exchange's current on-chain balances at the moment of execution — there is no field or check bounding how much the caller is willing to accept: [3](#0-2) 

The validation path likewise only rechecks balance sufficiency and rounding precision ("Not precise enough"), never a caller-supplied minimum: [4](#0-3) 

Because any unprivileged account can trade against the pool via `ExchangeTransactionContract` (only balance/expected checks apply, no special permission required), an attacker can submit a trade transaction that shifts `firstTokenBalance`/`secondTokenBalance` immediately before the creator's queued `ExchangeWithdrawContract` transaction executes in the same block (or via mempool ordering/fee bidding). Since the withdraw computes `anotherTokenQuant = otherBalance * quant / sameBalance` against the *post-trade* balances, the creator receives a paired-token amount different (and potentially much smaller in value) than what the pool ratio implied when they built and signed their withdraw transaction.

### Impact Explanation
The exchange creator withdrawing liquidity has no mechanism to protect against this manipulation — every `ExchangeWithdrawContract` execution is fully exposed to whatever the pool ratio happens to be at the moment of execution, which any other network participant can shift with a preceding `ExchangeTransactionContract` call. This can materially reduce the value the withdrawing creator receives, transferring value to the attacker who sandwiches the withdrawal (trade before, potentially reverse-trade after) — a concrete loss-of-funds impact consistent with the exchange/market-order-handling attack surface.

### Likelihood Explanation
Likelihood is high: `ExchangeTransactionContract` calls are cheap, unpermissioned, and can be crafted to move the pool ratio in the attacker's favor; transaction ordering within a block/mempool is influenceable by any broadcaster. No special privilege beyond being the exchange's creator is required to be a victim, and no privilege at all is required to be the attacker.

### Recommendation
Add a minimum-output field (analogous to `expected` in `ExchangeTransactionContract`) to `ExchangeWithdrawContract`, e.g. `min_another_token_quant`, and enforce it in `ExchangeWithdrawActuator.doValidate()`/`execute()` by rejecting the transaction if the computed `anotherTokenQuant` falls below the caller-specified minimum, mirroring the existing check in `ExchangeTransactionActuator`.

### Proof of Concept
1. Attacker observes a pending `ExchangeWithdrawContract` from exchange creator C requesting withdrawal of `tokenQuant` of `firstTokenID`.
2. Attacker broadcasts an `ExchangeTransactionContract` selling a large amount of `firstTokenID` into the same exchange, shifting `firstTokenBalance` up and `secondTokenBalance` down (or vice versa), and ensures it lands before C's transaction in block ordering (fee bump / relay timing).
3. When C's `ExchangeWithdrawActuator.execute()` runs, `anotherTokenQuant = bigSecondTokenBalance * bigTokenQuant / bigFirstTokenBalance` is computed against the now-skewed balances, yielding a materially smaller `anotherTokenQuant` than C expected when signing the transaction, as seen at [5](#0-4) .
4. Attacker may optionally reverse the trade afterward to restore the pool and pocket the difference extracted from C, with no on-chain check preventing this because `ExchangeWithdrawContract` carries no minimum-output guard.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-243)
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
