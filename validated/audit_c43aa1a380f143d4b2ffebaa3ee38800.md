### Title
`ValidateMultiSign` precompiled contract lacks chain-domain separation, enabling cross-chain signature replay - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` TVM precompile (selector `validatemultisign(address,uint256,bytes32,bytes[])`) is the on-chain building block that smart contracts use to authorize actions from off-chain signatures (e.g., gasless meta-transactions, multisig wallet actions, order/permit-style approvals). The digest that signers must sign is computed purely from the account address, a permission id, and caller-supplied `data` — with no chain identifier, genesis hash, or other domain separator. Any signature valid on one java-tron based network is therefore valid on every other java-tron network sharing the same address/permission state, mirroring the reported `claimERC20Prize`/`claimETHPrize` cross-chain-replay bug class (Merkle-proof/signature reuse across chain deployments).

### Finding Description
`ValidateMultiSign.execute` builds the signed digest as: [1](#0-0) 

```
byte[] address = words[0].toTronAddress();
int permissionId = words[1].intValueSafe();
byte[] data = words[2].getData();
byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
byte[] hash = Sha256Hash.hash(..., combine);
```

The hash contains no chain-specific value (no chainId, no genesis block hash, no calling-contract address). It then recovers signer addresses and checks accumulated permission weight against the account's on-chain `Permission`: [2](#0-1) 

The same weight/threshold logic is reused by `TransactionCapsule.checkWeight`/`getWeight`, which similarly never mixes in a chain identifier: [3](#0-2) [4](#0-3) 

Because address derivation, permission structure, and this precompile's hashing scheme are identical across every java-tron-based network (mainnet, testnets, or any independently deployed java-tron chain/fork), a user who signs a `validatemultisign` payload for one network's contract produces a signature that verifies identically on another network's contract using the same address/permissionId/data — exactly the cross-chain replay pattern flagged in the reference report for Merkle-proof-gated claim functions.

### Impact Explanation
Contracts built on TVM that rely on `ValidateMultiSign` for authorization (gasless relays, multisig-controlled vaults, exchange/order-signing systems, permit-style approvals) can be tricked into executing an action authorized by a signature that was actually produced for a different chain/deployment. If the account and its active permission (keys/threshold) happen to match across the two chains — which is common since address derivation is chain-agnostic in TRON — an attacker who observes a signed authorization on one network can replay it on another, causing unauthorized execution of privileged actions (fund transfers, approvals) without the signer's consent for that specific chain. This is an unauthorized account operation / potential theft of funds scenario.

### Likelihood Explanation
Exploitability depends on the same account address existing with an equivalent permission on two java-tron-based deployments and the victim (or an off-chain relay) reusing/leaking a signed `validatemultisign` payload across those deployments — a realistic scenario for projects that deploy the same dApp/contract bytecode to multiple TRON-compatible networks (mainnet + private/enterprise fork, or successive testnet resets) and rely on user-held keys for cross-network operations.

### Recommendation
Add domain separation to the digest computed in `ValidateMultiSign` (and any other on-chain signature-verification path used for authorization) by mixing in a chain-specific value — e.g., the network's genesis block hash or a configurable chain id from `DynamicPropertiesStore` — before hashing, similar to EIP-155/EIP-712 domain separators. Document this as a required practice for dApp authors and consider exposing the chain id to TVM contracts so they can independently domain-separate their own signed messages.

### Proof of Concept
1. Deploy identical contract bytecode (using `validatemultisign`) on two java-tron networks that share address derivation (e.g., mainnet and a private fork/testnet).
2. Create an account with the same address and an identical `Permission` (keys/threshold) on both networks (trivial if the fork is a genesis copy or if the same key/permission setup is replicated).
3. Off-chain, sign `sha256(address || permissionId || data)` once.
4. Submit the signature to the contract on network A — action executes.
5. Submit the same signature/data to the contract on network B — `ValidateMultiSign` recomputes the identical hash (no chain id involved) and recovers the same signer, so the action executes there too, without the signer intending or approving it for network B.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1065)
```java
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1111)
```java
      AccountCapsule account = this.getDeposit().getAccount(address);
      if (account != null) {
        try {
          Permission permission = account.getPermissionById(permissionId);
          if (permission != null) {
            //calculate weight
            long totalWeight = 0L;
            List<byte[]> executedSignList = new ArrayList<>();
            for (byte[] sign : signatures) {
              byte[] recoveredAddr = recoverAddrBySign(sign, hash);

              sign = merge(recoveredAddr, sign);
              if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
                if (ByteArray.matrixContains(executedSignList, sign)) {
                  continue;
                }
                MUtil.checkCPUTime();
              }
              long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
              if (weight == 0) {
                //incorrect sign
                return Pair.of(true, DATA_FALSE);
              }
              totalWeight += weight;
              executedSignList.add(sign);
              executedSignList.add(recoveredAddr);
            }

            if (totalWeight >= permission.getThreshold()) {
              return Pair.of(true, dataOne());
            }
          }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L218-226)
```java
  public static long getWeight(Permission permission, byte[] address) {
    List<Key> list = permission.getKeysList();
    for (Key key : list) {
      if (key.getAddress().equals(ByteString.copyFrom(address))) {
        return key.getWeight();
      }
    }
    return 0;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L233-270)
```java
  public static long checkWeight(Permission permission, List<ByteString> sigs, byte[] hash,
      List<ByteString> approveList)
      throws SignatureException, PermissionException, SignatureFormatException {
    long currentWeight = 0;
    if (sigs.size() > permission.getKeysCount()) {
      throw new PermissionException(
          "Signature count is " + (sigs.size()) + " more than key counts of permission : "
              + permission.getKeysCount());
    }
    HashMap addMap = new HashMap();
    for (ByteString sig : sigs) {
      if (sig.size() < 65) {
        throw new SignatureFormatException(
            "Signature size is " + sig.size());
      }
      String base64 = TransactionCapsule.getBase64FromByteString(sig);
      byte[] address = SignUtils
          .signatureToAddress(hash, base64, CommonParameter.getInstance().isECKeyCryptoEngine());
      long weight = getWeight(permission, address);
      if (weight == 0) {
        throw new PermissionException(
            ByteArray.toHexString(hash) + " is signed by " + encode58Check(address)
                + " but it is not contained of permission.");
      }
      if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_7_1)) {
        base64 = encode58Check(address);
      }
      if (addMap.containsKey(base64)) {
        throw new PermissionException(encode58Check(address) + " has signed twice!");
      }
      addMap.put(base64, weight);
      if (approveList != null) {
        approveList.add(ByteString.copyFrom(address)); //out put approve list.
      }
      currentWeight += weight;
    }
    return currentWeight;
  }
```
