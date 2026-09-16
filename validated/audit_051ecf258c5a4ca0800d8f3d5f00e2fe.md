This confirms the analog. `ExchangeTransactionContract` correctly implements a slippage floor (`expected` field, checked via `tokenExpected` in `ExchangeTransactionActuator.doValidate` at [1](#0-0)  ), analogous to Uniswap's `amount0Min`/`amount1Min`. `ExchangeWithdrawContract`, however, has no such minimum field at all, and `ExchangeWithdrawActuator` computes the paired-token payout purely from the pool's live reserve ratio at execution time with no user-supplied floor [2](#0-1) .

### Title
Missing slippage/minimum-output control in `ExchangeWithdrawActuator` allows sandwich-manipulation of AMM-pool withdrawal payout - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeWithdrawContract`/`ExchangeWithdrawActuator` let an exchange's creator withdraw `tokenQuant` of one side of a bancor-style TRC10/TRX pool and receive a proportional `anotherTokenQuant` of the paired token, computed strictly from the pool's *current* on-chain reserves at execution time. Unlike `ExchangeTransactionContract`, which carries an `expected` minimum-output field enforced in `ExchangeTransactionActuator.doValidate` (`"token required must greater than expected"`), `ExchangeWithdrawContract` has no analogous minimum/slippage field.

### Finding Description
`ExchangeWithdrawActuator.execute` and `doValidate` derive `anotherTokenQuant` solely from `firstTokenBalance`/`secondTokenBalance` read from the `ExchangeCapsule` at the moment the transaction executes: [3](#0-2) 
The protobuf contract only carries `exchange_id`, `token_id`, and `quant` — no expected/minimum value for the other token, so the creator has no way to bound how much of the paired asset they will actually receive: [4](#0-3) 
This is the direct analog of the reported Uniswap V3 `claim`/`burn` issue: `pool.burn` (here, the reserve-ratio computation) is executed without any caller-enforced floor, so any transaction that changes the pool ratio between when the withdraw was crafted/broadcast and when it is included can change the payout. Because the exchange pool is a public, permissionless AMM, any account can submit `ExchangeTransactionContract` trades against the same `exchange_id` to shift `firstTokenBalance`/`secondTokenBalance` immediately before the creator's `ExchangeWithdrawContract` is packed into a block, front-running/sandwiching the withdrawal and skewing the ratio the creator is paid at. Notably, `ExchangeTransactionActuator` treats exactly this risk as important enough to add an `expected` slippage guard for ordinary traders, but the corresponding protection was omitted for withdrawals.

### Impact Explanation
The exchange creator (an unprivileged transaction broadcaster who created the token pair) can be forced to withdraw at a manipulated ratio, receiving materially less of the paired token than intended — a direct value-loss/impermanent-loss scenario for the account performing the withdrawal, matching the Medium severity of the referenced report (value impacted via realized slippage during withdrawal, no user-controlled floor to reject an unfavorable execution).

### Likelihood Explanation
Exploitation only requires submitting ordinary, permissionless `ExchangeTransactionContract` transactions timed around the victim's `ExchangeWithdrawContract` broadcast — no special privileges, and TRON's block-production/mempool ordering (or public visibility of pending broadcasts) makes such front-running/sandwiching feasible for any network participant, mirroring the same MEV/sandwich mechanics described in the original report.

### Recommendation
Add an `expected`/minimum output field to `ExchangeWithdrawContract` (mirroring `ExchangeTransactionContract.expected`) and enforce it in `ExchangeWithdrawActuator.doValidate`, rejecting the withdrawal with a `ContractValidateException` (e.g., `"token required must greater than expected"`) when the computed `anotherTokenQuant` falls below the caller-specified floor.

### Proof of Concept
1. Exchange pool `id=E` holds `firstTokenBalance=100_000_000`, `secondTokenBalance=200_000_000`.
2. Creator broadcasts `ExchangeWithdrawContract{exchange_id=E, token_id=first, quant=10_000_000}`, expecting ≈`20_000_000` of `second` based on the ratio observed when constructing the transaction.
3. Before this transaction is included, an attacker broadcasts `ExchangeTransactionContract` trades against pool `E` that shift the ratio (e.g., selling large amounts of `second` into the pool), reducing `secondTokenBalance` relative to `firstTokenBalance`.
4. When the creator's withdraw executes, `ExchangeWithdrawActuator.execute` (lines 77-89) recomputes `anotherTokenQuant` from the now-skewed reserves, paying out far less than `20_000_000` of `second`, with no validation step to reject the unfavorable outcome — because `ExchangeWithdrawContract` carries no `expected` field to check against.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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
