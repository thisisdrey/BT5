### Title
Non-hardened Bancor Exchange math allows unchecked negative pool reserves via unprivileged `ExchangeTransactionContract` / `ExchangeInjectContract` calls - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
java-tron's on-chain TRC10↔TRX Bancor-style AMM (`Exchange`) is the closest in-repo analog to the Solend/Kamino "isolated pool price-oracle" bug class: each `Exchange` pool's `firstTokenBalance`/`secondTokenBalance` acts as the on-chain price reference for every trade against that pool, and any unprivileged account can create pools, inject/withdraw liquidity, and trade against them via broadcastable transactions.

### Finding Description
`ExchangeCapsule.transaction()` computes the counter-party amount using either the legacy floating-point `ExchangeProcessor` (double-based `Math.pow`) or, when the chain parameter `AllowHardenExchangeCalculation` is enabled, the `BigDecimal`-based `SafeExchangeProcessor`. Critically, the sanity check that the resulting pool reserves must stay non-negative is gated **only** on the hardened path: [1](#0-0) 

```
if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
  throw new ContractValidateException(...);
}
```
When `hardenedCalc` is false (the historical default before the `AllowHardenExchangeCalculation` proposal is activated), the double-precision `ExchangeProcessor.exchange()` computes `buyTokenQuant` via `Math.pow` on ratios of pool balances: [2](#0-1) 

Floating-point error in this formula (especially near pool-balance extremes, e.g. very small `secondTokenBalance` after repeated trades/injections, or very large `sellTokenQuant`) can produce a `buyTokenQuant` larger than the actual reserve, driving `newSecondTokenBalance`/`newFirstTokenBalance` negative without triggering any validation error, since the negative check is skipped for this path. Because `ExchangeTransactionActuator.execute()` unconditionally credits the computed `anotherTokenQuant` to the caller's real account balance via `addAssetAmountV2`/`setBalance` and persists the (now negative or inconsistent) pool state, the attacker can extract more value than the pool logically backs, mirroring the "isolated pool price/reserve manipulation" pattern from the Solend incident where an on-chain price mechanism was pushed out of its safe bounds to extract more value than legitimately backed. [3](#0-2) 

An attacker reaches this purely through unprivileged, broadcastable transactions: `ExchangeCreateContract` (to make a thin pool), `ExchangeInjectContract`/`ExchangeWithdrawContract` (to skew reserves toward extreme ratios), and `ExchangeTransactionContract` (to trigger the mispriced trade) — all validated/executed by `AbstractExchangeActuator`-derived actuators with no special permission beyond owning the tokens spent.

### Impact Explanation
If floating-point drift can be driven to push a pool balance negative or to yield an inflated `buyTokenQuant`, the attacker mints/receives TRC10 or TRX value beyond what the pool's tracked reserve represents, i.e., an unbacked balance/imbalance in the corresponding `Exchange` capsule that other liquidity providers rely on for `ExchangeWithdrawContract`. This is a "theft of funds"/"unbacked balance" class vulnerability, matching the report's "oracle attack causing bad debt in isolated pools" pattern applied to java-tron's own on-chain AMM.

### Likelihood Explanation
Requires: (1) a non-hardened environment (`AllowHardenExchangeCalculation` not yet activated on the target chain — this is a real, coexisting legacy code path since the hardened processor was added as a fix for a related class of issues, implying the un-hardened path is still reachable on any network/consensus configuration where the proposal hasn't been activated), (2) attacker control of pool state via self-created `Exchange` pools (unprivileged), and (3) construction of extreme ratios to maximize floating-point error. This is fully achievable by any transaction broadcaster with token balances, without SR/witness privilege, matching the "unprivileged API client" reachability bar. I was not able to fully verify the exact numerical bound at which `Math.pow`/double arithmetic actually flips a computed balance negative (this would require targeted numeric analysis/fuzzing of `ExchangeProcessor.exchange()`), so likelihood should be treated as moderate rather than confirmed pending that quantitative proof.

### Recommendation
- Apply the same non-negative reserve check performed for `hardenedCalc` unconditionally, regardless of `allowHarden()`/`AllowHardenExchangeCalculation` state.
- Consider deprecating/retiring the double-based `ExchangeProcessor` entirely in favor of `SafeExchangeProcessor` for all exchange actuators, or bound `sellTokenQuant`/reserve ratios to ranges empirically proven safe under double precision.
- Add an explicit assertion in `ExchangeTransactionActuator`/`ExchangeInjectActuator`/`ExchangeWithdrawActuator` that both `getFirstTokenBalance()` and `getSecondTokenBalance()` remain `> 0` after `Commons.putExchangeCapsule`, failing the transaction otherwise.

### Proof of Concept
Conceptual PoC (unverified against live numeric behavior, given no code-execution access in this session):
1. Broadcast `ExchangeCreateContract` to create a TRX/TRC10 pool with minimal `first_token_balance`/`second_token_balance` (e.g., 1 unit vs. very large ratio).
2. On a chain where `AllowHardenExchangeCalculation` proposal has not been activated (`hardenedCalc == false`), broadcast a sequence of `ExchangeInjectContract`/`ExchangeWithdrawContract` transactions to skew the pool to an extreme, thin-reserve ratio.
3. Broadcast `ExchangeTransactionContract` with a `quant` chosen to maximize floating-point rounding error in `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` (`Math.pow` on near-1 or near-0 bases), aiming to compute a `buyTokenQuant` exceeding the actual `secondTokenBalance`.
4. Observe that `ExchangeCapsule.transaction()` accepts the negative resulting balance (no exception thrown since `hardenedCalc` is false) and `ExchangeTransactionActuator.execute()` credits the inflated `anotherTokenQuant` to the attacker's real account balance/asset holdings.

Note: I could not execute or numerically simulate `Math.pow`-based drift within this session to concretely demonstrate a value flip to negative; this should be validated with a numerical PoC/fuzz test against `ExchangeProcessor` before treating this as fully confirmed.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-168)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-45)
```java
  private long exchangeToSupply(long balance, long quant) {
    logger.debug("balance: " + balance);
    long newBalance = balance + quant;
    logger.debug("balance + quant: " + newBalance);

    double issuedSupply = -supply * (1.0
        - Maths.pow(1.0 + (double) quant / newBalance, 0.0005, this.useStrictMath));
    logger.debug("issuedSupply: " + issuedSupply);
    long out = (long) issuedSupply;
    supply += out;

    return out;
  }

  private long exchangeFromSupply(long balance, long supplyQuant) {
    supply -= supplyQuant;

    double exchangeBalance = balance
        * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, this.useStrictMath) - 1.0);
    logger.debug("exchangeBalance: " + exchangeBalance);

    return (long) exchangeBalance;
  }

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-99)
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

      ret.setExchangeReceivedAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
```
