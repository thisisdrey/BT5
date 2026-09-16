I found the actual analog. `Wallet.checkPublicAmount` retrieves a `scalingFactor` from an arbitrary/untrusted shielded TRC20 contract address (a broadcastable value the caller controls via `shieldedTRC20ContractAddress` in `PrivateShieldedTRC20Parameters`) and uses it to convert the user-supplied public `fromAmount`/`toAmount` into the internal shielded `scaledFromAmount`/`scaledToAmount` used to mint/burn shielded value — mirroring the oracle bug pattern of blindly trusting an externally-sourced scale/decimals value without any bound or sanity check against the actual token's real decimals.

### Title
Unvalidated attacker/contract-controlled `scalingFactor` in shielded TRC-20 mint/burn allows scaled-amount desynchronization from real token decimals - (File: framework/src/main/java/org/tron/core/Wallet.java)

### Summary
`Wallet.checkPublicAmount` (called from `createShieldedContractParameters` / `createShieldedContractParametersWithoutAsk`, both reachable via public gRPC/HTTP API calls) fetches the `scalingFactor()` value directly from the target shielded TRC-20 contract at the address supplied by the caller, and uses it to convert the raw public `fromAmount`/`toAmount` into the "scaled" shielded amount that is later compared/asserted against the shielded note `value` fields to decide MINT/TRANSFER/BURN semantics. [1](#0-0) 

### Finding Description
Similar to the CurveVolatileOracle bug — where a value sourced from an external/adapter source was combined using an assumed decimal base rather than the actual one — `checkPublicAmount` takes the `scalingFactor` returned by an arbitrary contract (only sanity-checked to be `> 0`) and uses it to divide the caller-supplied `fromAmount`/`toAmount` to obtain the internal scaled amount: [2](#0-1) 

The contract address is fully attacker-controlled (any deployed contract implementing `scalingFactor()`), and the returned value is trusted with only a `> 0` check — there is no verification that it corresponds to the real TRC-20 token's decimals (e.g., matching `10^(18-tokenDecimals)`), nor any upper bound. `getShieldedContractScalingFactor` simply performs a constant call to whatever address is passed: [3](#0-2) 

Since the "scaled" amount computed here is directly compared to and merged with the actual private shielded note `value`s (`shieldedReceives.get(0).getNote().getValue()`, etc.) to determine the type and legitimacy of the transaction and drives the `TriggerSmartContract` call value passed to the actual token contract (via `setTransparentFromAmount`/`setTransparentToAmount`), a mismatch between the assumed decimal scale and reality causes the transparent amount debited/credited on the real ERC20-like token contract to diverge from the shielded value that is minted or burned.

### Impact Explanation
If a caller points `shieldedTRC20ContractAddress` at a malicious or misconfigured contract whose `scalingFactor()` does not match the actual scaling used elsewhere by the node (or if the real deployed shielded pool's scaling factor is misread/miscomputed under attacker-influenced call context), the amount recorded as minted/burned in the private pool can become desynchronized from the amount actually transferred on the public token side. This is analogous to the oracle inflating price by orders of magnitude — here it can inflate or deflate the shielded value asserted against the real transparent transfer, enabling unbacked shielded balance creation or an inconsistency between the public and private ledgers of the token, which is a fund-safety-relevant divergence.

### Likelihood Explanation
Reachable directly through the public shielded transaction creation RPCs (`createShieldedContractParameters` / `createShieldedContractParametersWithoutAsk`), which are exposed to any client once shielded transaction API is enabled, without requiring any special privilege — the attacker only needs to supply their own `shieldedTRC20ContractAddress` implementing `scalingFactor()`.

### Recommendation
Do not trust an arbitrary externally-supplied contract's `scalingFactor()` return value without additional invariant checks (e.g., cross-checking against a canonical, pre-registered scaling factor for known/whitelisted shielded pools, or validating consistency against the token's own `decimals()`), and bound the value to a sane range before using it to convert public amounts into internal scaled amounts used for shielded value accounting.

### Proof of Concept
1. Deploy a contract at address `X` implementing `scalingFactor()` returning an artificially small or large value (only constraint: `> 0` and evenly divides the caller's chosen `fromAmount`/`toAmount`).
2. Call the shielded gRPC/HTTP endpoint `createShieldedContractParameters` with `shieldedTRC20ContractAddress = X`, `fromAmount` a value crafted so that `fromAmount / scalingFactor` equals an attacker-desired shielded note value.
3. Observe that `checkPublicAmount` (Wallet.java lines 4223-4253) accepts and returns `scaledFromAmount` purely as a function of the attacker-controlled `scalingFactor`, with no cross-validation against the real token's decimals, allowing the minted shielded value to be decoupled from the intended 1:1 relationship with the public token amount transferred on-chain.

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L4220-4254)
```java
  /**
   * trigger contract to get the scalingFactor, and check the public amount,
   */
  private long[] checkPublicAmount(byte[] address, BigInteger fromAmount, BigInteger toAmount)
      throws ContractExeException, ContractValidateException {
    checkBigIntegerRange(fromAmount);
    checkBigIntegerRange(toAmount);

    BigInteger scalingFactor;
    try {
      byte[] scalingFactorBytes = getShieldedContractScalingFactor(address);
      scalingFactor = ByteUtil.bytesToBigInteger(scalingFactorBytes);
    } catch (ContractExeException e) {
      throw new ContractExeException("Get shielded contract scalingFactor failed");
    }
    if (scalingFactor.compareTo(BigInteger.ZERO) <= 0) {
      throw new ContractValidateException("scalingFactor must be positive");
    }

    // fromAmount and toAmount must be a multiple of scalingFactor
    if (!(fromAmount.mod(scalingFactor).equals(BigInteger.ZERO)
        && toAmount.mod(scalingFactor).equals(BigInteger.ZERO))) {
      throw new ContractValidateException("fromAmount or toAmount invalid");
    }

    long[] ret = new long[2];
    try {
      ret[0] = fromAmount.divide(scalingFactor).longValueExact();
      ret[1] = toAmount.divide(scalingFactor).longValueExact();
    } catch (ArithmeticException e) {
      throw new ContractValidateException("fromAmount or toAmount invalid");
    }

    return ret;
  }
```
