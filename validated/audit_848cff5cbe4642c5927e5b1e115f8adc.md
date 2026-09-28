### Title
Operator.execute forwards the user's authenticated identity to arbitrary untrusted targets, allowing them to act as the victim across the whole protocol - (File: contracts/Operator.sol)

### Summary
The cpp-httplib bug forwards stored auth credentials to any host the client is redirected to. The Metronome analog is `Operator.execute`: it stores the caller's address in the `MSG_SENDER` transient slot for the duration of the entire batch and then performs arbitrary `functionCallWithValue` calls to attacker-influenceable targets. During any of those calls — including nested callbacks — `SynthContext._msgSender()` resolves to the victim, so every target reached inside the batch is implicitly granted the victim's full authority over `Pool`, `DepositToken`, `DebtToken`, and `SyntheticToken`. There is no target allowlist or per-call scoping, identical to forwarding an `Authorization` header to an untrusted origin.

### Finding Description
`Operator.execute` stores `msg.sender` in `MSG_SENDER_STORAGE` via `setMsgSender` and then executes arbitrary calls: [1](#0-0) [2](#0-1) 

Every protocol contract resolves the caller through `SynthContext._msgSender()`, which returns the stored victim identity whenever `msg.sender == operator`: [3](#0-2) 

A malicious target included in (or reached during) the batch — e.g. a phishing "airdrop"/router contract, or a malicious ERC-20/777-style token invoked via a swap-like call — can, while `MSG_SENDER` is still stored, call back into:

- `DepositToken.withdraw` / `transferFrom` to pull the victim's unlocked collateral shares
- `Pool.swap` / `DebtToken` operations to swap assets or manipulate the victim's position
- `SyntheticToken.transferFrom`-style flows anywhere `allowance`/`balanceOf` of the victim is used

The `nonReentrant` guard only prevents re-entering `execute` itself; it does not stop the arbitrary target from calling any other contract while the credential slot is live — exactly like httplib forwarding the `Authorization` header to the redirect target.

### Impact Explanation
Direct theft of user funds: any victim who batches a call to an attacker-controlled contract (phishing signature, malicious frontend-injected call, or a token with transfer callbacks) gives that contract full authority to withdraw their unlocked collateral and move their synthetic assets within the same transaction. The invariant broken is identity/authority scoping — credentials are forwarded across a trust boundary the user never intended.

### Likelihood Explanation
Requires the victim to include a call that reaches attacker-controlled code in an `Operator.execute` batch — the same precondition as the CVE (a malicious/compromised endpoint receives the forwarded credential). Operator batched execution is the intended UX for this protocol, and malicious contracts are routinely smuggled into batches via phishing frontends or malicious tokens, making the scenario realistic for an unprivileged attacker.

### Recommendation
Scope the forwarded identity to specific targets: pass an allowlist of permitted target contracts per call, or have protocol contracts additionally require `msg.sender == operator` combined with a user-supplied signature/intent rather than a blanket transient slot. At minimum, document that any target in an `execute` batch receives full account authority, and consider clearing `MSG_SENDER` before calls to non-whitelisted targets or restricting `execute` targets to protocol contracts registered in `PoolRegistry`.

### Proof of Concept
Foundry-style sketch:

```solidity
// Attacker contract included in victim's batch
contract MaliciousTarget {
    function run(IPool pool, IDepositToken dt) external {
        // During Operator.execute, SynthContext._msgSender() == victim
        uint256 unlocked = dt.unlockedBalanceOf(operator.getActualMsgSender());
        dt.withdraw(unlocked); // withdraws victim's collateral to msg flow
    }
}

// Victim is tricked into:
// operator.execute([Call(pool.deposit...), Call(malicious.run, 0, ...)])
// MaliciousTarget.run calls DepositToken.withdraw; Pool resolves
// _msgSender() -> victim via MSG_SENDER slot; victim's collateral leaves.
```

The fork PoC would: impersonate a user with deposits, call `Operator.execute` with a second `Call` to the malicious contract, and assert the attacker's EOA/contract received the victim's withdrawn underlying.

### Citations

**File:** contracts/Operator.sol (L20-24)
```text
    modifier setMsgSender() {
        MSG_SENDER_STORAGE.asAddress().tstore(msg.sender);
        _;
        MSG_SENDER_STORAGE.asAddress().tstore(address(0));
    }
```

**File:** contracts/Operator.sol (L34-55)
```text
    function execute(
        Call[] calldata calls_
    ) external payable override nonReentrant setMsgSender returns (bytes[] memory _returnData) {
        uint256 _length = calls_.length;
        _returnData = new bytes[](_length);

        uint256 _sumOfValues;
        Call calldata _call;
        for (uint256 i; i < _length; ) {
            _call = calls_[i];
            uint256 _value = _call.value;
            unchecked {
                _sumOfValues += _value;
            }
            _returnData[i] = _call.target.functionCallWithValue(_call.callData, _value);
            unchecked {
                ++i;
            }
        }

        require(msg.value == _sumOfValues, "value-mismatch");
    }
```

**File:** contracts/utils/SynthContext.sol (L14-24)
```text
    function _msgSender() internal view virtual override returns (address) {
        IPoolRegistry _poolRegistry = poolRegistry();
        if (address(_poolRegistry) != address(0)) {
            IOperator _operator = _poolRegistry.operator();
            if (msg.sender == address(_operator)) {
                return _operator.getActualMsgSender();
            }
        }

        return msg.sender;
    }
```
