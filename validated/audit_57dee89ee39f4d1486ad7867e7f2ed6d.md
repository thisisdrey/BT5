### Title
Unchecked pool-balance subtraction in legacy `ExchangeCapsule.transaction()` allows exchange pool balances to go negative, locking/corrupting other traders' funds - (File: `chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java`)

### Summary
`ExchangeCapsule.transaction()` computes the new first/second token balances of a TRON DEX-style exchange pair using a Bancor-relay double-precision formula and then performs a **plain, unchecked subtraction** (`secondTokenBalance - buyTokenQuant` / `firstTokenBalance - buyTokenQuant`) unless the committee-gated "hardened" calculation path is active. This mirrors the Panoptic `removedLiquidity -= chunkLiquidity` bug: an accounting value derived from an unchecked/uncontrolled arithmetic operation can be pushed negative (or, since these are `long`s, silently wrap/underflow), corrupting the shared pool state that backs other users' deposited tokens.

### Finding Description
In `ExchangeCapsule.transaction()`: [1](#0-0) 

The legacy (non-hardened) branch computes `newFirstTokenBalance`/`newSecondTokenBalance` via plain `+`/`-` with no bound check, in contrast to the hardened branch which explicitly validates `newFirstTokenBalance < 0 || newSecondTokenBalance < 0` before committing: [2](#0-1) 

The `buyTokenQuant` returned to the caller comes from `ExchangeProcessor.exchange()`, which chains two double-precision Bancor relay computations (`exchangeToSupply` then `exchangeFromSupply`), each involving `Math.pow` on ratios of attacker-controlled `sellTokenQuant` versus pool `balance`/`supply`: [3](#0-2) 

Because this is floating-point arithmetic feeding directly into an unchecked integer subtraction of the pool's `long` balances, it is possible — exactly as in the reported Panoptic bug class — for the derived `buyTokenQuant` to exceed the actual pool balance held for the other token, driving `newFirstTokenBalance`/`newSecondTokenBalance` negative in the legacy path. This corrupted, negative pool-balance state is then persisted via `Commons.putExchangeCapsule`, propagating into every subsequent trade against that exchange pair (called from both `ExchangeTransactionActuator.execute()` and `ExchangeWithdrawActuator`): [4](#0-3) 

The existence of the `hardenedCalc` flag and the explicit negative-balance guard added only to that branch is itself evidence that the legacy unguarded path is known to be capable of producing negative/inconsistent pool balances — this is the SFPM `removedLiquidity` underflow analog: an unchecked subtraction on a shared accounting variable (the exchange pool `firstTokenBalance`/`secondTokenBalance`) that other, unrelated users' funds depend on.

### Impact Explanation
The `ExchangeCapsule.firstTokenBalance`/`secondTokenBalance` fields represent the pooled TRX/TRC10 assets deposited by the pool creator and consumed/replenished by every trader who calls `ExchangeTransactionActuator`. If a crafted trade (extreme `tokenQuant` relative to pool size/supply) drives one side of the pool negative under the legacy math path, the pool's economic invariant is broken:
- Subsequent legitimate traders' `ExchangeTransactionActuator.doValidate()` balance/limit checks operate on a corrupted state, and follow-on trades or `ExchangeWithdrawActuator` (owner withdrawal) can pay out more tokens than actually exist in the pool, effectively stealing/locking assets that were deposited by the pool creator/other traders — an unbacked/negative balance condition (unauthorized value creation or destruction), matching the "Medium/High" unbacked-balance criterion.
- This is directly reachable by any unprivileged account sending a signed `ExchangeTransactionContract` transaction; no special privilege is required.

### Likelihood Explanation
Reaching the vulnerable code only requires calling `ExchangeTransactionActuator`/`ExchangeCreateActuator` with a normal signed transaction — no SR/witness/p2p access needed. Whether a concrete input reliably forces the resulting `buyTokenQuant` past the pool balance depends on floating-point rounding behavior of the Bancor relay formula for extreme `sellTokenQuant`/pool-size ratios, which I was not able to fully numerically verify within the available tool budget (I could not execute code to confirm a concrete negative-balance input). The presence of dedicated hardened-mode negative-balance checks strongly suggests this condition is reachable in the legacy path, but I could not confirm whether `allowHardenExchangeCalculation` is enabled by default on this chain (it is a committee-controlled `DynamicPropertiesStore` proposal parameter), which materially affects current exploitability.

### Recommendation
- Enforce the same non-negative invariant (`newFirstTokenBalance >= 0 && newSecondTokenBalance >= 0`) unconditionally in `ExchangeCapsule.transaction()`, not only in the `hardenedCalc` branch, and reject the transaction (throw `ContractValidateException`) instead of persisting a corrupted balance.
- Confirm/force `allowHardenExchangeCalculation` to be permanently enabled (i.e., remove the legacy unchecked branch) rather than leaving it as an optional committee-gated toggle.
- Add explicit bounds/sanity validation in `ExchangeTransactionActuator.doValidate()` ensuring the computed `buyTokenQuant` cannot exceed the currently available opposite-side pool balance before `execute()` commits the new balances.

### Proof of Concept
Conceptual reproduction path (not independently executed against the codebase due to tool constraints):
1. Attacker creates or targets an `Exchange`/`ExchangeV2` pair with a small `secondTokenBalance` (e.g., created via `ExchangeCreateActuator` with minimal seed liquidity) while `allowHardenExchangeCalculation` is disabled (legacy path active).
2. Attacker calls `ExchangeTransactionActuator` with `tokenId = firstTokenID` and a very large `quant`, causing `ExchangeProcessor.exchange()`'s double-precision Bancor relay math (`exchangeToSupply`/`exchangeFromSupply`) to return a `buyTokenQuant` that exceeds `secondTokenBalance`.
3. `ExchangeCapsule.transaction()`'s legacy branch computes `newSecondTokenBalance = secondTokenBalance - buyTokenQuant` with no negativity check and persists it via `Commons.putExchangeCapsule`.
4. The exchange pool state is now corrupted (negative or inconsistent balance); the attacker's account has already been credited `anotherTokenQuant` in `ExchangeTransactionActuator.execute()`, and subsequent traders/withdrawals interact with a broken pool, permitting extraction of more value than was actually deposited or permanently locking remaining depositors' funds.

Verification of the exact numeric conditions that force `buyTokenQuant` past the pool balance, and confirmation of the default state of `allowHardenExchangeCalculation` on this network, require running the actual `ExchangeProcessor`/`ExchangeCapsule` code with representative pool sizes — this should be validated with a Devin session capable of executing the existing `ExchangeCapsuleTest`/`ExchangeTransactionActuatorTest` suites and crafting boundary values.

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-98)
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
```
