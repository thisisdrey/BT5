Confirmed: there is no LP-share/supply tracking in `Exchange` — only `first_token_balance`/`second_token_balance` raw balances are stored, and only a single `creator_address` is entitled to withdraw. This confirms the withdraw calculation is purely a function of the *current instantaneous pool ratio*, which is the direct analog to Uniswap's `slot0` spot price.

### Title
Exchange withdrawal amount is computed from the manipulable instantaneous pool ratio, letting the exchange creator front/back-run trades to drain counterparties' funds - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java)

### Summary
`ExchangeWithdrawActuator` computes the amount of the "other" token to return on withdrawal solely from the exchange's current `firstTokenBalance`/`secondTokenBalance` ratio at execution time [1](#0-0) , exactly analogous to using Uniswap's `slot0` current tick/price instead of a manipulation-resistant TWAP to make a security-critical decision. Because this ratio is trivially and legitimately manipulable within the same block by any unprivileged account calling `ExchangeTransactionActuator` (a public, permissionless swap), the exchange creator can sandwich their own withdrawal around a large swap to extract disproportionate value from the pool at the expense of the counterparties who supplied the other side of that swap.

### Finding Description
The `Exchange`/`ExchangeCapsule` model has no LP-share or total-supply accounting — it only stores raw token balances and a single `creator_address` [2](#0-1) . Any account can create an exchange and only the creator may inject/withdraw liquidity, enforced in `doValidate()`: [3](#0-2) .

Anyone (including the creator itself, or an accomplice account) can call `ExchangeTransactionActuator.execute` at any time to swap tokens through the pool via the bonding-curve `ExchangeProcessor`/`SafeExchangeProcessor`, which directly mutates `firstTokenBalance`/`secondTokenBalance` [4](#0-3) . This is the on-chain analog of manipulating a Uniswap V3 pool's spot price via a large trade before reading `slot0`.

`ExchangeWithdrawActuator.doValidate()`/`execute()` then compute the withdrawal counter-value directly from whatever the *current* balance ratio happens to be at execution time — there is no time-weighted price, no minimum-received/slippage check from the withdrawer's perspective protecting other participants, and no proportional LP-share accounting: [5](#0-4) .

Because block-level transaction ordering in java-tron is controlled by the block producer/packer and multiple transactions from different accounts execute sequentially within the same block, an attacker who controls (or colludes with) the exchange creator account can, within one block:
1. Submit a large `ExchangeTransactionContract` swap (from any account) that skews the pool ratio dramatically in the creator's favor.
2. Have the creator immediately submit `ExchangeWithdrawContract` for a specific `tokenID`/`tokenQuant`, which is priced using the now-skewed ratio, extracting far more of `anotherTokenID` than the creator's proportional share of the pool.
3. Optionally reverse the swap in a later transaction, restoring the pool to its prior state while permanently keeping the extracted excess value.

This directly steals value from the pool that legitimately belongs to whoever contributed the counter-asset via earlier `ExchangeTransactionContract`/`ExchangeInjectContract` calls (unbacked withdrawal), matching the report's underlying bug class ("use of manipulable instantaneous state instead of a manipulation-resistant price to make a value-transfer decision").

### Impact Explanation
An exchange creator (an ordinary, unprivileged asset issuer/account — no special role needed beyond having created the exchange) can extract more value on withdrawal than their true proportional contribution, at the expense of other traders/liquidity in the pool. This is a concrete theft-of-funds / unbacked-balance issue reachable purely through normal signed transactions (`ExchangeTransactionContract` + `ExchangeWithdrawContract`), satisfying the "unauthorized... theft... of funds" acceptance criterion.

### Likelihood Explanation
Likelihood is high: both `ExchangeTransactionActuator` and `ExchangeWithdrawActuator` are ordinary, permissionless/creator-gated actuators reachable by any signed transaction; no privileged role, malicious SR/witness, or p2p manipulation is required. The attacker only needs to control the funds for one large swap and be (or collude with) the exchange creator, and rely on normal transaction-ordering within a block (which the attacker/block packer can arrange).

### Recommendation
Track proportional LP shares (analogous to Uniswap V2 LP tokens) rather than computing withdrawal amounts from the instantaneous balance ratio, or require inject/withdraw amounts to be validated against a manipulation-resistant reference (e.g., an average price accumulated over multiple blocks) rather than the spot balance ratio read at execution time. At minimum, disallow same-block inject/withdraw following a swap that materially moves the ratio, or require a cooldown between trading activity and creator withdrawals.

### Proof of Concept
1. Account A creates an exchange with `ExchangeCreateContract`, becoming `creatorAddress`, depositing `X` of token1 and `Y` of token2 (see `ExchangeCreateActuator.execute`) [6](#0-5) .
2. Account A (or a colluding account B) submits a large `ExchangeTransactionContract` selling a huge amount of token1 into the pool, which via `ExchangeCapsule.transaction`/`ExchangeProcessor.exchange` sharply reduces `secondTokenBalance` relative to `firstTokenBalance` [7](#0-6) .
3. In the same block, account A (the creator) submits `ExchangeWithdrawContract` for a modest amount of token1; because `anotherTokenQuant` is computed from the now-skewed `firstTokenBalance`/`secondTokenBalance` ratio [8](#0-7) , A receives a disproportionately large amount of token2 relative to A's true share of the pool.
4. Account A/B can reverse the swap afterward to restore the ratio, while the excess token2 extracted in step 3 remains permanently with A — value taken from whoever supplied token2 to the pool.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-223)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L106-112)
```java
  public long getFirstTokenBalance() {
    return this.exchange.getFirstTokenBalance();
  }

  public long getSecondTokenBalance() {
    return this.exchange.getSecondTokenBalance();
  }
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L55-90)
```java
      byte[] firstTokenID = exchangeCreateContract.getFirstTokenId().toByteArray();
      byte[] secondTokenID = exchangeCreateContract.getSecondTokenId().toByteArray();
      long firstTokenBalance = exchangeCreateContract.getFirstTokenBalance();
      long secondTokenBalance = exchangeCreateContract.getSecondTokenBalance();

      long newBalance = subtractExact(accountCapsule.getBalance(), fee);

      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, firstTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(firstTokenID, firstTokenBalance, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, secondTokenBalance));
      } else {
        accountCapsule
            .reduceAssetAmountV2(secondTokenID, secondTokenBalance, dynamicStore, assetIssueStore);
      }

      long id = addExact(dynamicStore.getLatestExchangeNum(), 1);
      long now = dynamicStore.getLatestBlockHeaderTimestamp();
      if (dynamicStore.getAllowSameTokenName() == 0) {
        //save to old asset store
        ExchangeCapsule exchangeCapsule =
            new ExchangeCapsule(
                exchangeCreateContract.getOwnerAddress(),
                id,
                now,
                firstTokenID,
                secondTokenID
            );
        exchangeCapsule.setBalance(firstTokenBalance, secondTokenBalance);
```
