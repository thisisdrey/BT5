Confirmed: `ExchangeInjectContract` and `ExchangeWithdrawContract` have no `expected` field, unlike `ExchangeTransactionContract` which has `expected` at `exchange_contract.proto:36` [1](#0-0) , and the corresponding actuators never check a minimum-received bound against the pool's current ratio before mutating balances [2](#0-1) [3](#0-2) .

### Title
Missing slippage/minimum-output protection in `ExchangeInjectActuator` and `ExchangeWithdrawActuator` enables sandwich attacks on TRC10 bancor-exchange liquidity operations - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
The `ExchangeInjectContract` and `ExchangeWithdrawContract` messages, and their actuators, do not include any user-supplied "expected"/minimum-amount field to bound the counter-token amount returned or required by a proportional liquidity operation on the built-in bancor-style TRC10 exchange. This is the same bug class as the reported `RioLRTCoordinator.deposit` issue: an operation whose settlement amount is computed at execution time from a pool ratio that can move between submission and inclusion, with no slippage guard for the caller.

### Finding Description
`ExchangeTransactionContract` (the actual "trade" contract, analogous to a deposit/swap) was hardened against this exact class of bug: it carries an `expected` field [4](#0-3) , and `ExchangeTransactionActuator.doValidate()` enforces `anotherTokenQuant >= tokenExpected` before execution [5](#0-4) .

However, `ExchangeInjectContract` and `ExchangeWithdrawContract` were never given an equivalent field [6](#0-5) . Both actuators compute the required/returned counter-token amount from the exchange pool's live `firstTokenBalance`/`secondTokenBalance` ratio at execution time, with no caller-supplied bound to reject an unfavorable ratio:

- `ExchangeInjectActuator.doValidate()` derives `anotherTokenQuant` purely from the current pool ratio and the `tokenQuant` the caller sends, with no comparison against any expected minimum/maximum [7](#0-6) .
- `ExchangeWithdrawActuator.doValidate()` similarly derives `anotherTokenQuant` from the live ratio, and only checks a "Not precise enough" rounding-precision condition, never bounding against attacker-induced ratio shift [8](#0-7) .

Because any account can freely call `ExchangeTransactionContract` against the same exchange pool to shift `firstTokenBalance`/`secondTokenBalance` in either direction, an attacker who observes a pending `ExchangeInjectContract` or `ExchangeWithdrawContract` in the mempool can front-run it with a trade that skews the ratio, let the victim's inject/withdraw execute at the skewed ratio (extracting or diluting more/less of the counter-token than the creator intended), then back-run with a reverse trade to restore the ratio and capture the difference — a classic sandwich attack. This mirrors the reported bug class precisely: absence of a minimum/expected-output check on a ratio/oracle-derived settlement amount, leaving the caller with no defense against price manipulation while the transaction is pending.

### Impact Explanation
The exchange pool creator (the only account authorized to call inject/withdraw, per the `is not creator` check [9](#0-8) ) can be forced to inject liquidity at a manipulated ratio or withdraw less counter-token than the pool's unmanipulated state would yield, resulting in a direct, measurable transfer of value from the creator to the attacker executing the sandwich trades. This is a concrete theft-of-funds/unbacked-balance impact reachable purely through ordinary signed transactions (`ExchangeTransactionContract` + `ExchangeInjectContract`/`ExchangeWithdrawContract`) that any account can broadcast against a public TRC10 exchange.

### Likelihood Explanation
Likelihood is limited by the fact that only the exchange creator can trigger inject/withdraw, and the attacker needs to predict/observe the pending inject/withdraw transaction and control transaction ordering (e.g., via bribing or being a block producer, or simply being fast in a busy mempool) to sandwich it profitably. This constrains but does not eliminate exploitability, since TRC10 exchanges are permissionless to trade against and transaction ordering within a block is influenced by the block-producing SR, making the attack feasible without any privileged role beyond ordinary transaction broadcasting.

### Recommendation
Add an `expected`/minimum-output (and optionally maximum-input) field to `ExchangeInjectContract` and `ExchangeWithdrawContract`, mirroring the pattern already used in `ExchangeTransactionContract`, and enforce it in `ExchangeInjectActuator.doValidate()` / `ExchangeWithdrawActuator.doValidate()` by rejecting the transaction when the computed `anotherTokenQuant` falls outside the caller-specified bound.

### Proof of Concept
1. Exchange pool P has `firstTokenBalance = A`, `secondTokenBalance = B` (ratio B/A).
2. Creator broadcasts `ExchangeInjectContract` for pool P injecting `tokenQuant` of the first token, expecting to also lock up `~ B/A * tokenQuant` of the second token (per `ExchangeInjectActuator.execute` at `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java:73-83`).
3. Attacker observes this pending transaction and broadcasts an `ExchangeTransactionContract` trade against pool P that shifts the ratio unfavorably for the creator (e.g., inflating `secondTokenBalance` relative to `firstTokenBalance`), and ensures it is ordered before the creator's inject transaction.
4. The creator's `ExchangeInjectContract` executes against the now-skewed ratio, computing `anotherTokenQuant` (per lines 73-83) at a rate worse than the creator intended, since there is no `expected` bound to reject this outcome.
5. Attacker broadcasts a reverse `ExchangeTransactionContract` trade to restore the ratio, realizing a profit equal to the value difference extracted from the creator's inject.
6. The identical mechanism applies to `ExchangeWithdrawActuator`, where the creator can be made to withdraw less counter-token than the unmanipulated ratio would provide.

### Citations

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L17-37)
```text
message ExchangeInjectContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}

message ExchangeWithdrawContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}

message ExchangeTransactionContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
  int64 expected = 5;
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L184-220)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L190-247)
```java
    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();

    long anotherTokenQuant;

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }

    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
      throw new ContractValidateException("token is not in exchange");
    }

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

    } else {
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigSecondTokenBalance).longValueExact();
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
