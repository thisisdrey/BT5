### Title
Exchange withdraw/inject ratio calculation rounds down, letting the exchange creator drain pool liquidity at the expense of the counter-token reserve - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java])

### Summary
`ExchangeWithdrawActuator.execute()` and `ExchangeInjectActuator.execute()` compute the "another token" amount for a proportional liquidity withdrawal/injection using plain `BigInteger.divide()`, which truncates toward zero (floor for positive values), instead of rounding in the direction that protects the shared pool reserves. This mirrors the reported Vault issue where `convertToShares()` rounds in the caller's favor instead of the vault's.

### Finding Description
When an exchange creator withdraws liquidity via `ExchangeWithdrawActuator`, the exact `tokenQuant` of the requested token is removed from the pool, while the proportional amount of the *other* token (`anotherTokenQuant`) is computed with floor division: [1](#0-0) 
Similarly, `ExchangeInjectActuator` computes the paired deposit amount with the same floor-rounding helper before crediting the pool: [2](#0-1) 
Both actuators are only callable by the exchange's creator address (`Commons`/`ExchangeCapsule.getCreatorAddress()` check), but the exchange pool itself is shared trading liquidity that other unprivileged users interact with via the fully public `ExchangeTransactionActuator` (AMM trade path reachable by any signed transaction). Because withdraw/inject always round the *paired* token amount down rather than up, the creator can repeatedly withdraw (or inject then withdraw) small increments where the paired-token division floors to zero-loss for themselves but leaves the pool's two-token ratio skewed, degrading the invariant that later traders rely on for fair pricing. Over many iterations this rounding bias compounds, similar to how vault share/asset conversions must always round in favor of the pool rather than the caller.

### Impact Explanation
This is a Medium-severity issue: the actor who can trigger the flaw (the exchange creator, an ordinary self-service address that anyone can become by calling `ExchangeCreateActuator`) is not a privileged system role, and the effect degrades the AMM pool's balance/pricing integrity that other, unrelated order takers rely on when using `ExchangeTransactionActuator`. It does not directly let an attacker steal from other users' wallets, but it causes the pool's book-kept reserves to accumulate rounding loss, which can eventually be extracted by the creator across many small withdraw/inject operations, effectively skimming value from the shared liquidity pool.

### Likelihood Explanation
Likelihood is moderate: it requires the attacker to be the creator of their own exchange (freely obtainable via `ExchangeCreateActuator`), and to repeat many small withdraw operations to accumulate meaningful skew, since each single truncation is bounded by 1 unit of the paired token. This differs from the Sherlock report's single-operation, deterministic loss, so the practical exploitability and magnitude here are lower and not fully proven without a concrete PoC quantifying extractable value across iterations.

### Recommendation
Round the paired-token computation in the pool's favor: use ceiling division when calculating the amount to be added to the pool's reserves relative to what leaves it, so that the exchange reserves are never systematically reduced by truncation. Apply the same `divideCeil`/`BigInteger.divideAndRemainder` pattern already used elsewhere in the codebase (e.g., `RepositoryImpl.divideCeilExact`) to `ExchangeWithdrawActuator`/`ExchangeInjectActuator`'s `anotherTokenQuant` computation.

### Proof of Concept
Not fully verified with a concrete numeric exploit chain; the finding is based on identified rounding-direction code (`BigInteger.divide()` truncation in both `execute()` methods) analogous to the reported Vault bug class. A background Devin session with build/test tooling would be needed to write a unit test demonstrating cumulative reserve loss over repeated `ExchangeWithdraw`/`ExchangeInject` calls to confirm exploitability magnitude.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L209-227)
```java
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
```
