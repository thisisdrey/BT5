### Title
Unsafe `BigInteger.longValue()` fallback in `MarketUtils.multiplyAndDivide()` can silently wrap/truncate maker/taker trade quantities - (File: chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java)

### Summary
`MarketUtils.multiplyAndDivide(a, b, c, disableMath)` first attempts a fast-path `long` multiply+divide and, on `ArithmeticException` (i.e., when `a*b` overflows a `long`), falls back to `BigInteger` arithmetic. However the `BigInteger` result of `a.multiply(b).divide(c)` is converted back with `.longValue()` instead of `.longValueExact()`. `BigInteger.longValue()` does not throw on overflow — it silently truncates to the low 64 bits, which can produce an arbitrary, possibly negative, `long`. This is the same bug class as the reported `int256` cast issue: overflow is checked for the multiplication step, but the final narrowing cast back to the smaller type is never validated against its range, so out-of-range results silently wrap instead of failing safely.

### Finding Description [1](#0-0) 

```java
public static long multiplyAndDivide(long a, long b, long c, boolean disableMath) {
    try {
      long tmp = multiplyExact(a, b, disableMath);
      return floorDiv(tmp, c, disableMath);
    } catch (ArithmeticException ex) {
      // do nothing here, because we will use BigInteger to compute again
    }

    BigInteger aBig = BigInteger.valueOf(a);
    BigInteger bBig = BigInteger.valueOf(b);
    BigInteger cBig = BigInteger.valueOf(c);

    return aBig.multiply(bBig).divide(cBig).longValue();
}
```

The `long` fast path is protected (via `multiplyExact`, which throws on overflow of `a*b`). But the fallback path only guards against overflow of `a*b` (that's why it entered the catch block) — it does not check whether the *final* quotient `a*b/c` fits in a `long` before calling `.longValue()`. When `c` is small relative to `a*b` (e.g., `c == 1` or `c` much smaller than `b`), the resulting quotient can still exceed `Long.MAX_VALUE`, and `.longValue()` silently returns the truncated low-64-bit value (which can be negative or a small positive garbage number) instead of throwing.

This function is called from `MarketSellAssetActuator.matchSingleOrder()` — the core order-matching logic executed while processing a `MarketSellAssetContract`, which is a transaction type any account can broadcast (an "order placer"): [2](#0-1) [3](#0-2) [4](#0-3) 

`takerSellRemainQuantity`, `makerSellQuantity`, and `makerBuyQuantity` are all attacker-controlled `long` values coming directly from order capsules created by users placing market orders (`sellTokenQuantity`/`buyTokenQuantity` on a `MarketSellAssetContract`), so a malicious actor can construct maker and taker orders whose product overflows a `long` (forcing the `BigInteger` fallback) while the true quotient still exceeds `Long.MAX_VALUE`, causing `.longValue()` to wrap to an incorrect (possibly negative) result.

### Impact Explanation
The wrapped/truncated `long` returned from `multiplyAndDivide` becomes `takerBuyTokenQuantityRemain` / `makerBuyTokenQuantityReceive`, values that directly drive:
- how much of a token/asset is credited to the taker/maker (`accountCapsule.addAssetAmountV2` / balance adjustments elsewhere in the matching flow),
- how order remainders are computed and subtracted (`subtractExact`, `setSellTokenQuantityRemain`).

If the wrapped value becomes negative or an unexpectedly small/large amount, this can lead to:
- Crediting/debiting incorrect (attacker-favorable) token amounts during exchange order matching — theft of funds or creation of an unbacked asset balance.
- Corruption of the maker/taker order remainder bookkeeping, which can desynchronize on-chain order-book state (affecting subsequent block application / consensus-critical state transitions in `Manager`).

This satisfies the "unauthorized account operation / theft or permanent freezing of funds / unbacked balance" bar because it is directly reachable from a single signed `MarketSellAssetContract` transaction, executed inside the actuator's `execute()` path that is part of normal transaction processing/block application.

### Likelihood Explanation
Exploitability requires the attacker to control both a maker order (previously placed) and match it with a taker order whose sell/buy quantities are chosen so that `a*b` overflows `long` (forcing the catch branch) while `a*b/c` still exceeds `Long.MAX_VALUE`. Since `sellTokenQuantity`/`buyTokenQuantity` on market orders are ordinary `long` fields set by the order placer (bounded only by asset/TRX balances or, for asset amounts, by however large the asset's total supply/`long` value is), an attacker with a large asset supply or by issuing a token with a large total supply could construct order quantities large enough to trigger this condition. This makes the likelihood plausible but requires deliberately engineered extreme quantities, similar in spirit to the reported issue, which also required carefully chosen boundary values.

### Recommendation
Replace `.longValue()` with `.longValueExact()` in the `BigInteger` fallback of `MarketUtils.multiplyAndDivide`, and handle the resulting `ArithmeticException` explicitly (e.g., reject the trade/reduce match quantity, or throw an actuator-level validation/execution error) rather than allowing execution to proceed with a silently wrapped value:

```java
return aBig.multiply(bBig).divide(cBig).longValueExact();
```

and propagate any `ArithmeticException` to the caller so `MarketSellAssetActuator` can fail the transaction safely instead of using a corrupted quantity.

### Proof of Concept
Conceptual PoC (values chosen to overflow `long` at the multiply step and remain out of `long` range after division):
1. Attacker places (or arranges) a maker order with `makerSellQuantity` and `makerBuyQuantity` set to large `long` values (bounded by asset supply which can be minted up to `Long.MAX_VALUE`).
2. Attacker places a taker `MarketSellAssetContract` with `takerSellRemainQuantity` chosen such that:
   - `takerSellRemainQuantity * makerSellQuantity` overflows a signed 64-bit `long` (triggers `ArithmeticException` in the fast path, entering the `BigInteger` fallback), and
   - `(takerSellRemainQuantity * makerSellQuantity) / makerBuyQuantity` still exceeds `Long.MAX_VALUE` (e.g., `makerBuyQuantity == 1`).
3. `BigInteger.longValue()` on the true (very large) quotient truncates to the low 64 bits, returning a wrapped/possibly negative `long` as `takerBuyTokenQuantityRemain`.
4. This corrupted value is then used to compute `makerBuyTokenQuantityReceive`, `takerSellTokenLeft`, and account asset adjustments in `matchSingleOrder`, resulting in incorrect crediting/debiting of trade amounts.

Note: I was unable to fully verify the exact upper bound / validation constraints imposed on `sellTokenQuantity`/`buyTokenQuantity` at `MarketSellAssetActuator.validate()` (the specific quantity-limit checks were not found in the indexed portion of that file), so the precise minimum asset supply/quantities required to trigger the overflow-and-truncate condition in production could not be confirmed with certainty from the available code. This should be validated further (e.g., by inspecting `MarketSellAssetActuator.validate()` in full and any `MAX` limits on `sellTokenQuantity`/`buyTokenQuantity`) before treating exploitability as fully confirmed.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L264-277)
```java
  public static long multiplyAndDivide(long a, long b, long c, boolean disableMath) {
    try {
      long tmp = multiplyExact(a, b, disableMath);
      return floorDiv(tmp, c, disableMath);
    } catch (ArithmeticException ex) {
      // do nothing here, because we will use BigInteger to compute again
    }

    BigInteger aBig = BigInteger.valueOf(a);
    BigInteger bBig = BigInteger.valueOf(b);
    BigInteger cBig = BigInteger.valueOf(c);

    return aBig.multiply(bBig).divide(cBig).longValue();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L402-404)
```java
    long takerBuyTokenQuantityRemain = MarketUtils
        .multiplyAndDivide(takerSellRemainQuantity, makerSellQuantity, makerBuyQuantity,
            this.disableJavaLangMath());
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L426-428)
```java
      makerBuyTokenQuantityReceive = MarketUtils
          .multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity,
              this.disableJavaLangMath());
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L461-463)
```java
      makerBuyTokenQuantityReceive = MarketUtils
          .multiplyAndDivide(makerSellRemainQuantity, makerBuyQuantity, makerSellQuantity,
              this.disableJavaLangMath());
```
