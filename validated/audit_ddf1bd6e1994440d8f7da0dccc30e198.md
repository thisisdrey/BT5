### Title
VM trace file write follows symlinks at predictable transaction-hash path - (File: actuator/src/main/java/org/tron/core/vm/VMUtils.java)

### Summary
`VMUtils.createProgramTraceFile`/`writeStringToFile`, invoked from `VMActuator` after every TVM execution when VM tracing is enabled, writes a program-trace JSON file at the predictable path `./vm_trace/<txHash>.json` and then opens it with a plain `FileOutputStream`, without ever checking whether the path is a symbolic link. [1](#0-0)  Since `File.exists()`/`isFile()`/`canWrite()` and `FileOutputStream` all follow symlinks transparently, this mirrors CVE-2026-40977's root cause (`ApplicationPidFileWriter` following symlinks at a predictable default path): a local attacker who can place a symlink at the pre-computed transaction-hash path can make the node overwrite an arbitrary file it has write access to, every time a transaction with that hash executes.

### Finding Description
`createProgramTraceFile(String txHash)` builds `file = new File(new File("./", "vm_trace"), txHash + ".json")`. [2](#0-1)  If the file already exists, the code only checks `file.isFile() && file.canWrite()` — both of which resolve through symlinks — before handing the same `File` object to `writeStringToFile`, which opens it via `new FileOutputStream(file)` and writes the serialized trace content, truncating whatever the symlink points to. [3](#0-2)  The filename component (`txHash`) is fully predictable in advance by anyone constructing/signing the transaction, since it is the transaction's own hash, which the caller can compute locally before broadcasting. This is directly analogous to the advisory's pattern: a fixed/predictable filesystem location + symlink-following write, differing only in that the vulnerable file name here is the transaction hash rather than a static "application.pid".

### Impact Explanation
An attacker with local filesystem write access to the node's `vm_trace` working directory can pre-plant a symlink named `<precomputed-txhash>.json` pointing at any file the java-tron process can write (configuration files, database files, key files, or other operational data). Broadcasting the corresponding transaction (with VM trace enabled) then causes the node to silently overwrite that target file's contents with trace JSON, potentially corrupting node configuration, on-disk state, or causing the process to crash or halt on the next restart/read of that file. This matches the "node crash or halt" impact category. Because the attacker fully controls when the write triggers (by choosing when to broadcast the transaction), this is a repeatable, deterministic file-corruption primitive, exactly as described in the original advisory ("corrupt one file on the host each time...").

### Likelihood Explanation
Exploitation requires: (1) VM tracing enabled via `VMConfig.vmTrace()`, which defaults to `false` and is documented as "For developer only" [4](#0-3) ; and (2) local filesystem write access to the process's working directory (`./vm_trace/`), matching the original advisory's own precondition (`PR:H`, local attacker with write access to the target directory). This mirrors the CVSS profile of the source advisory (`AV:L/AC:H/PR:H`) — it is not remotely triggerable by an anonymous API client with no local footprint, and it only manifests on nodes explicitly running with a developer/debug tracing flag turned on, which is atypical for production mainnet nodes.

### Recommendation
In `VMUtils.createProgramTraceFile`/`writeStringToFile`, resolve and validate the target path using `Files.readAttributes(path, ..., LinkOption.NOFOLLOW_LINKS)` (or `Files.newOutputStream` with `StandardOpenOption.CREATE_NEW`/without following links) before writing, refusing to write through a symlink, consistent with the symlink-hardening already applied to keystore writes in `WalletUtils.writeWalletFile`/`warnIfSymbolicLink` [5](#0-4)  and the keystore CLI's `NOFOLLOW` read pattern used in `KeystoreUpdate`. [6](#0-5)  Alternatively, write to a temp file in the same directory and atomically rename over the target only after confirming (via `NOFOLLOW_LINKS` attributes) that the destination is not a symlink.

### Proof of Concept
1. Enable VM tracing on a target node (`vm.trace=true` config, developer/debug mode).
2. Locally (attacker has write access to the node's working directory) construct a transaction whose resulting hash is known/predictable, e.g. by trying nonces/data variations offline until a desired hash prefix is obtained, or simply pre-compute the hash of the exact transaction to be broadcast.
3. Create a symlink `./vm_trace/<txHash>.json -> /path/to/target/file/writable/by/node/process`.
4. Broadcast the transaction; when `VMActuator` calls `VMUtils.saveProgramTraceFile(txHash, content)`, `createProgramTraceFile` finds the existing symlink, `isFile()`/`canWrite()` both return true (following the link), and `writeStringToFile` truncates and overwrites the symlink target with trace JSON. [7](#0-6)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/VMUtils.java (L52-97)
```java
  private static File createProgramTraceFile(String txHash) {
    File result = null;

    if (VMConfig.vmTrace()) {

      File file = new File(new File("./", "vm_trace"), txHash + ".json");

      if (file.exists()) {
        if (file.isFile() && file.canWrite()) {
          result = file;
        }
      } else {
        try {
          file.getParentFile().mkdirs();
          if (!file.createNewFile()) {
            logger.error("failed to create file {}", file.getPath());
          }
          result = file;
        } catch (IOException e) {
          // ignored
        }
      }
    }

    return result;
  }

  private static void writeStringToFile(File file, String data) {
    OutputStream out = null;
    try {
      out = new FileOutputStream(file);
      if (data != null) {
        out.write(data.getBytes("UTF-8"));
      }
    } catch (Exception e) {
      logger.error(format("Cannot write to file '%s': ", file.getAbsolutePath()), e);
    } finally {
      closeQuietly(out);
    }
  }

  public static void saveProgramTraceFile(String txHash, String content) {
    File file = createProgramTraceFile(txHash);
    if (file != null) {
      writeStringToFile(file, content);
    }
```

**File:** common/src/main/java/org/tron/core/vm/config/VMConfig.java (L6-14)
```java
/**
 * For developer only
 */
public class VMConfig {

  private static boolean vmTraceCompressed = false;

  @Setter
  private static boolean vmTrace = false;
```

**File:** crypto/src/main/java/org/tron/keystore/WalletUtils.java (L84-151)
```java
  public static void writeWalletFile(WalletFile walletFile, File destination)
      throws IOException {
    Path dir = destination.getAbsoluteFile().getParentFile().toPath();
    Files.createDirectories(dir);

    Path tmp;
    try {
      tmp = Files.createTempFile(dir, "keystore-", ".tmp",
          PosixFilePermissions.asFileAttribute(OWNER_ONLY));
    } catch (UnsupportedOperationException e) {
      // Windows / non-POSIX fallback — best-effort narrowing only (see JavaDoc)
      tmp = Files.createTempFile(dir, "keystore-", ".tmp");
      File tf = tmp.toFile();
      tf.setReadable(false, false);
      tf.setReadable(true, true);
      tf.setWritable(false, false);
      tf.setWritable(true, true);
    }

    try {
      objectMapper.writeValue(tmp.toFile(), walletFile);
      try {
        Files.move(tmp, destination.toPath(),
            StandardCopyOption.REPLACE_EXISTING,
            StandardCopyOption.ATOMIC_MOVE);
      } catch (AtomicMoveNotSupportedException e) {
        Files.move(tmp, destination.toPath(), StandardCopyOption.REPLACE_EXISTING);
      }
    } catch (Exception e) {
      try {
        Files.deleteIfExists(tmp);
      } catch (IOException suppress) {
        e.addSuppressed(suppress);
      }
      throw e;
    }
  }

  public static Credentials loadCredentials(String password, File source, boolean ecKey)
      throws IOException, CipherException {
    warnIfSymbolicLink(source);
    WalletFile walletFile = objectMapper.readValue(source, WalletFile.class);
    return Credentials.create(Wallet.decrypt(password, walletFile, ecKey));
  }

  /**
   * Emit a warning if {@code source} is a symbolic link. The keystore is still
   * read (following the symlink), preserving compatibility with legitimate
   * deployments that use symlinks to organize keystore files (e.g.
   * {@code /opt/tron/keystore/witness.json} -> {@code /mnt/encrypted/...},
   * container volume-mount paths). The warning gives operators a chance to
   * notice if a path they did not expect to be a symlink has become one — for
   * example if an attacker with config-injection ability has redirected the
   * SR startup keystore. This mirrors how Ethereum consensus clients (e.g.
   * Lighthouse) handle a configured {@code voting_keystore_path}.
   */
  private static void warnIfSymbolicLink(File source) {
    try {
      BasicFileAttributes attrs = Files.readAttributes(source.toPath(),
          BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
      if (attrs.isSymbolicLink()) {
        logger.warn("Keystore file is a symbolic link: {} — proceeding, "
            + "but verify the symlink target points where you expect.",
            source.getPath());
      }
    } catch (IOException ignored) {
      // If we can't stat, let the subsequent readValue surface the real error.
    }
```

**File:** plugins/src/main/java/common/org/tron/plugins/KeystoreUpdate.java (L146-163)
```java
      boolean ecKey = !sm2;
      // Re-read via NOFOLLOW byte channel to close the TOCTOU window between
      // findKeystoreByAddress and this read — an attacker with directory
      // write access could otherwise swap the file for a symlink in between.
      byte[] keystoreBytes = KeystoreCliUtils.readKeystoreFile(keystoreFile, err);
      if (keystoreBytes == null) {
        // readKeystoreFile already printed the specific reason
        return 1;
      }
      WalletFile walletFile = MAPPER.readValue(keystoreBytes, WalletFile.class);
      SignInterface keyPair = Wallet.decrypt(oldPassword, walletFile, ecKey);

      // createStandard already sets the correctly-derived address. Do NOT override
      // with walletFile.getAddress() — that would propagate a potentially spoofed
      // address from the JSON.
      WalletFile newWalletFile = Wallet.createStandard(newPassword, keyPair);
      // writeWalletFile does a secure temp-file + atomic rename internally.
      WalletUtils.writeWalletFile(newWalletFile, keystoreFile);
```
