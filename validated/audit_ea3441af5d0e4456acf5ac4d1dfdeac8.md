### Title
Front-runnable Exchange balance-ratio checks allow persistent DoS of `ExchangeWithdrawContract` (and precision-based reverts in `ExchangeTransactionContract`) - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeWithdrawActuator` computes the amount of the paired token to return to the exchange creator by reading the *current* pool balances (`firstTokenBalance`/`secondTokenBalance`) of the shared `ExchangeCapsule` and deriving a ratio-based `anotherTokenQuant`, then asserting the result is within a fixed 0.0001 precision tolerance and that the pool holds enough of both tokens. Because these same pool balances are mutated by any account issuing an unrelated `ExchangeTransactionContract` trade (`ExchangeTransactionActuator`), an attacker can front-run the creator's withdraw transaction with a trivial trade that shifts the balance ratio just enough to make the withdraw's live-computed precision/sufficiency checks fail, causing `ContractValidateException` every time the withdraw is attempted — the same "balance snapshot used for a sanity check that any unprivileged actor can shift before the privileged/legitimate call executes" root cause described in the reported `notifyRewardAmount()` DoS.

### Finding Description
`ExchangeWithdrawActuator.doValidate()` reads `firstTokenBalance`/`secondTokenBalance` straight from the shared, globally-mutable `ExchangeCapsule` at validation time: [1](#0-0) 

It then derives `anotherTokenQuant` from these live balances and enforces sufficiency and precision checks against them: [2](#0-1) 

The same `ExchangeCapsule` balances are freely mutated by any unprivileged account through `ExchangeTransactionActuator.execute()`, which calls `exchangeCapsule.transaction(...)` and persists the updated pool balances via `Commons.putExchangeCapsule`: [3](#0-2) 

Because `ExchangeTransactionContract` is a permissionless, single-signed-transaction operation reachable by any account with the traded token/TRX (`doValidate()` only checks the caller's own balance, not any special role — `ExchangeWithdrawActuator.java:181-183` restricts only the *withdraw* to the creator, not the trade side), an attacker can submit a minimal trade before every block in which the creator's withdraw is expected to land. This shifts `firstTokenBalance`/`secondTokenBalance` by a small amount, which is enough to push the ratio-derived `anotherTokenQuant` outside the ±0.0001 rounding tolerance or below the "exchange balance is not enough" threshold, causing `ContractValidateException` and rejection of the creator's `ExchangeWithdrawContract` on every attempt — a repeatable denial of service, exactly analogous to the reported pattern where a user's `claimReward()` transaction, front-run just before `notifyRewardAmount()`, permanently breaks the privileged balance-dependent operation.

### Impact Explanation
An exchange creator can be indefinitely prevented from withdrawing their liquidity from an on-chain `Exchange`/`ExchangeV2` pool by any third party willing to submit cheap trade transactions ahead of each withdraw attempt. Because the withdraw is the only mechanism to remove liquidity from the pool, sustained front-running amounts to freezing the creator's funds inside the pool, satisfying the "permanent freezing of funds" bar for High severity — reachable purely via ordinary, permissionless `ExchangeTransactionContract` transactions (an order-placer/asset-issuer-reachable path), not requiring any privileged role, malicious SR/witness, or network-level attack.

### Likelihood Explanation
The precondition is trivial: the attacker only needs enough of either exchanged token/TRX to submit a minimal `ExchangeTransactionContract` trade in (or immediately before) the block containing the target withdraw, which is realistically achievable by any address given java-tron's public mempool and block-production timing (similar front-running feasibility assumption used in the original report). No special timing precision beyond ordinary transaction ordering/front-running is required, and the attack can be repeated cheaply for as long as the attacker wishes to keep the creator's funds locked.

### Recommendation
Avoid deriving withdraw eligibility/precision solely from a live, externally-manipulable balance snapshot taken at validate/execute time. Options include: decoupling the “Not precise enough” check from the instantaneous pool ratio (e.g., allow the creator to specify acceptable slippage bounds explicitly rather than a fixed global tolerance), using a time-weighted or block-committed balance for the ratio calculation, or applying the withdrawal atomically against the state as of the start of the transaction batch with an explicit minimum-out parameter supplied by the creator so genuine market movement (not just adversarial front-running) doesn't need to hit the exact original ratio.

### Proof of Concept
1. Exchange creator A owns exchange `id` with pool balances `(firstTokenBalance, secondTokenBalance)`.
2. A broadcasts `ExchangeWithdrawContract{exchangeId, tokenId=firstTokenID, quant=Q}`.
3. Attacker B, holding a small amount of `secondTokenID` (or TRX), broadcasts a minimal `ExchangeTransactionContract` trade against the same exchange with higher gas priority so it lands first.
4. B's trade updates `exchangeCapsule` balances via `ExchangeTransactionActuator.execute()` (`ExchangeTransactionActuator.java:81-91`).
5. A's withdraw is then validated in `ExchangeWithdrawActuator.doValidate()` against the now-shifted balances; the recomputed `anotherTokenQuant` no longer satisfies the ±0.0001 precision check (or the sufficiency check), and `ContractValidateException("Not precise enough")` / `"exchange balance is not enough"` is thrown, failing A's transaction.
6. B repeats step 3 for every subsequent block in which A retries the withdraw, permanently denying A's ability to exit the pool.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L185-192)
```java
    byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
    byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
    long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
    long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();

```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L218-243)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-96)
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

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);
```
