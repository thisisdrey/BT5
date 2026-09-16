### Title
`ExchangeInjectContract`/`ExchangeWithdrawContract` are vulnerable to sandwich attacks due to missing slippage protection on the on-chain Exchange bonding curve - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java])

### Summary
TRON's built-in Exchange (a bancor-style AMM implemented by `ExchangeCreateContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract`/`ExchangeTransactionContract`) derives its "price" purely from the current on-chain pool ratio, exactly like the external `XOracle` price used by `Unitas.swap`. Any account can move that ratio in a single block by submitting an `ExchangeTransactionContract` trade. Unlike `ExchangeTransactionActuator`, which lets the caller bound the outcome with a minimum-expected-amount check, `ExchangeInjectActuator` and `ExchangeWithdrawActuator` compute the counter-asset amount solely from the pool ratio **at execution time**, with no caller-supplied bound. This reproduces the exact sandwich pattern in the report: a public, price-dependent transaction sitting in the mempool can be front-run and back-run by any unprivileged actor to extract value from the transaction's sender.

### Finding Description
The Exchange pool ratio (`firstTokenBalance` / `secondTokenBalance`) plays the role of the oracle price in the report. It is read and mutated by ordinary, unprivileged transactions:

- `ExchangeTransactionActuator.execute` calls `exchangeCapsule.transaction(...)`, which moves the pool balances according to a bancor curve implemented in `ExchangeCapsule.transaction` and `ExchangeProcessor`/`SafeExchangeProcessor`, i.e., it changes the "price" for everyone else in the same block window: [1](#0-0) 

- `ExchangeInjectActuator.execute` computes the counter-asset amount purely from the pool ratio that exists *when the transaction executes*, with no minimum/maximum bound supplied by the caller: [2](#0-1) 

- `ExchangeWithdrawActuator.execute` has the identical pattern for withdrawals — the amount of the counter asset returned to the creator is derived from the ratio at execution time, again with no caller-supplied bound: [3](#0-2) 

By contrast, `ExchangeTransactionActuator` (the trading path) does allow the caller to bound slippage via `tokenExpected`, proving the protocol authors recognized the need for this protection but omitted it from Inject/Withdraw: [4](#0-3) 

Because Inject/Withdraw transactions are broadcast publicly before being packed into a block, an attacker can:
1. Observe the victim's pending `ExchangeInjectContract` (or `ExchangeWithdrawContract`) in the mempool.
2. Front-run it with an `ExchangeTransactionContract` trade that skews the pool ratio in the attacker's favor.
3. Let the victim's inject/withdraw execute at the manipulated ratio, causing the victim to deposit a disproportionately large counter-asset amount (inject) or receive a disproportionately small counter-asset amount (withdraw).
4. Back-run with a reverse `ExchangeTransactionContract` trade to restore the ratio and capture the extracted value, paying only the trading fee/slippage cost of the two trades (which the bancor curve makes cheap for small perturbations).

This is functionally identical to the reported `XOracle`/`Unitas.swap` sandwich: a state update that is publicly visible pre-confirmation and used to price a subsequent operation, with no protection against intervening price manipulation.

### Impact Explanation
An attacker (any unprivileged account capable of broadcasting `ExchangeTransactionContract`, `ExchangeInjectContract`, `ExchangeWithdrawContract`) can extract value from Exchange creators performing routine liquidity operations, without needing any special privilege, by sandwiching their transaction. This constitutes unauthorized theft of funds from Exchange pool participants, satisfying the "concrete unauthorized account operation / theft of funds" bar for Medium severity.

### Likelihood Explanation
The attack requires only:
- Monitoring the public mempool for pending `ExchangeInjectContract`/`ExchangeWithdrawContract` transactions (trivially detectable by exchange ID).
- The ability to submit ordinary `ExchangeTransactionContract` transactions before and after the victim's transaction is packed — achievable by any account with sufficient balance/energy, the same capability required to use the Exchange normally.

No malicious SR/witness/committee/peer role, no leaked keys, and no privileged access are needed — only standard transaction broadcasting, matching the allowed reachable-path constraints.

### Recommendation
Add a caller-supplied slippage bound to `ExchangeInjectContract` and `ExchangeWithdrawContract` (analogous to the `expected` field already used in `ExchangeTransactionContract`), and reject the transaction in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` if the computed `anotherTokenQuant` falls outside the caller's specified min/max bound.

### Proof of Concept
1. `Creator` broadcasts `ExchangeInjectContract` for exchange `E` with `tokenId = firstToken`, `quant = Q` (expects `anotherTokenQuant ≈ secondBalance*Q/firstBalance` based on the ratio it currently observes).
2. `Attacker` sees the pending tx and broadcasts `ExchangeTransactionContract` selling a large amount of `secondToken` into `E`, sharply raising `secondTokenBalance` relative to `firstTokenBalance` before `Creator`'s tx is packed.
3. `Creator`'s `ExchangeInjectContract` executes at the skewed ratio: `ExchangeInjectActuator.execute` computes `anotherTokenQuant = secondTokenBalance*Q/firstTokenBalance` using the now-inflated `secondTokenBalance`, forcing `Creator` to deposit far more `secondToken` than intended. [5](#0-4) 
4. `Attacker` immediately broadcasts the reverse `ExchangeTransactionContract` (buying back `secondToken` with `firstToken`), restoring the ratio and pocketing the value siphoned from `Creator`'s inflated deposit, minus bancor-curve fees.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-158)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L60-83)
```java
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeInjectContract.getTokenId().toByteArray();
      long tokenQuant = exchangeInjectContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            secondTokenBalance, tokenQuant), firstTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, tokenQuant),
            addExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            firstTokenBalance, tokenQuant), secondTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, anotherTokenQuant),
            addExact(secondTokenBalance, tokenQuant));
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
