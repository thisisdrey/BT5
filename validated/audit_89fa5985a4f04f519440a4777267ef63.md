### Title
Lack of slippage protection in `ExchangeWithdrawActuator` allows front-running to force unfavorable withdrawal ratios - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeWithdrawContract` lets an exchange creator withdraw liquidity from a bancor-style `Exchange` pool, receiving a paired-token amount (`anotherTokenQuant`) computed on-the-fly from the current pool ratio (`firstTokenBalance` / `secondTokenBalance`) at execution time. Unlike `ExchangeTransactionContract`, which carries an `expected` field enforced in `ExchangeTransactionActuator.doValidate()`, `ExchangeWithdrawContract` has no minimum-output/slippage parameter at all, so a withdrawer has no way to bound the amount of the paired token they will actually receive.

### Finding Description
In `ExchangeWithdrawActuator.execute()`, `anotherTokenQuant` is derived purely from the pool balances read at execution time: [1](#0-0) 

The withdraw contract's protobuf definition only carries `owner_address`, `exchange_id`, `token_id`, and `quant` — no `expected`/minimum field, contrary to `ExchangeTransactionContract` which explicitly has `expected` for this purpose: [2](#0-1) 

By contrast, `ExchangeTransactionActuator.doValidate()` does enforce a slippage bound by comparing the computed swap output against the caller-supplied `tokenExpected`: [3](#0-2) 

`ExchangeWithdrawActuator.doValidate()` only checks a *precision* tolerance (`allowHarden`/legacy remainder checks against ~0.01%), not a caller-specified minimum acceptable output; it does not know or check what the withdrawer expects to receive: [4](#0-3) 

Because the pool ratio can be moved arbitrarily between transaction construction/broadcast and block inclusion by anyone submitting an `ExchangeTransactionContract` against the same `exchange_id` (a permissionless, unprivileged operation reachable by any signed transaction), an attacker can front-run a pending `ExchangeWithdrawContract` to skew `firstTokenBalance`/`secondTokenBalance` just before it executes.

### Impact Explanation
The exchange creator submitting `ExchangeWithdrawContract` has no mechanism to cap their downside: an attacker can sandwich the withdrawal by trading against the exchange immediately before it is packed into a block, sharply reducing the `anotherTokenQuant` (and correspondingly the value) received relative to what the withdrawer expected when they signed the transaction. This is a direct, unauthorized value transfer from the withdrawer to the attacker via ratio manipulation — a concrete "theft of funds"-class impact reachable purely by broadcasting standard, permissionless transactions (`ExchangeTransactionContract` to move the ratio, and observing the pending `ExchangeWithdrawContract`).

### Likelihood Explanation
`Exchange`/`ExchangeV2` pools are a core, currently-supported java-tron feature reachable by any account (asset issuers who create exchanges routinely need to withdraw liquidity), and manipulating the ratio only requires ordinary `ExchangeTransactionContract` broadcasts, which any account can send. Given java-tron's transaction-ordering flexibility (mempool visibility, no strict FIFO guarantee), front-running/sandwiching a visible pending withdraw transaction is practically achievable.

### Recommendation
Add a minimum-expected-output field to `ExchangeWithdrawContract` (mirroring `ExchangeTransactionContract.expected`) and enforce it in `ExchangeWithdrawActuator.doValidate()`/`execute()`, rejecting the transaction if the computed `anotherTokenQuant` falls below the caller-specified minimum.

### Proof of Concept
1. Attacker observes a pending `ExchangeWithdrawContract` for `exchange_id = X`, `token_id = A`, `quant = Q` in the mempool.
2. Attacker broadcasts an `ExchangeTransactionContract` against the same exchange `X` that sells a large amount of token `B` into the pool, sharply changing the `firstTokenBalance`/`secondTokenBalance` ratio (processed via `exchangeCapsule.transaction(...)` in `ExchangeTransactionActuator.execute()`).
3. Once the attacker's transaction lands first, the victim's `ExchangeWithdrawContract` executes with the skewed ratio: `anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance)` (or the symmetric branch) in `ExchangeWithdrawActuator.execute()`, yielding a far smaller `anotherTokenQuant` than the victim expected when signing.
4. There is no `expected`/minimum field on `ExchangeWithdrawContract` to cause the withdraw to revert, so the victim's under-value withdrawal succeeds and the attacker profits from the ratio distortion (can be reversed in a follow-up trade), completing a classic sandwich attack with no protocol-level guard.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L77-89)
```java
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

**File:** Tron protobuf protocol document.md (L1403-1442)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
