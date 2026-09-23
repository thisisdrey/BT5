No vulnerability found for this question.

CVE-2018-14721 describes a Java-specific gadget chain: `jackson-databind`'s polymorphic deserialization allows an attacker-supplied `@class` type name to resolve to a listed Java class (from `axis2-jaxws`) that then performs SSRF as a side effect of instantiation. This bug class fundamentally depends on Java's runtime class-loading/reflection-based polymorphic deserialization pattern (attacker-controlled fully-qualified class name → arbitrary constructor/setter side effects), which has no structural analog in Go's standard library.

I checked the closest primitive matches in this repo:

- `encoding/gob`'s interface value transmission requires types to be explicitly pre-registered via `gob.Register`/`gob.RegisterName` by the *program*, not the attacker; an attacker cannot introduce arbitrary unregistered types into the decode path, and decoding an unregistered/unknown type name simply fails rather than instantiating attacker-chosen code. [1](#0-0) [2](#0-1) 
- `encoding/json` (v1 and v2) has no polymorphic type-name-driven instantiation at all — unmarshaling targets a concrete, statically-known Go type or a generic `interface{}`/`any`, and there is no mechanism for a JSON payload to specify an arbitrary Go type to instantiate. [3](#0-2) 

Because Go offers no reachable, unauthenticated path where attacker-supplied data selects an arbitrary type/class for deserialization-driven instantiation (the root cause of CVE-2018-14721), there is no legitimate analog to report here.

### Citations

**File:** src/encoding/gob/type.go (L830-853)
```go
// RegisterName is like [Register] but uses the provided name rather than the
// type's default.
func RegisterName(name string, value any) {
	if name == "" {
		// reserved for nil
		panic("attempt to register empty name")
	}

	ut := userType(reflect.TypeOf(value))

	// Check for incompatible duplicates. The name must refer to the
	// same user type, and vice versa.

	// Store the name and type provided by the user....
	if t, dup := nameToConcreteType.LoadOrStore(name, reflect.TypeOf(value)); dup && t != ut.user {
		panic(fmt.Sprintf("gob: registering duplicate types for %q: %s != %s", name, t, ut.user))
	}

	// but the flattened type in the type table, since that's what decode needs.
	if n, dup := concreteTypeToName.LoadOrStore(ut.base, name); dup && n != name {
		nameToConcreteType.Delete(name)
		panic(fmt.Sprintf("gob: registering duplicate names for %s: %q != %q", ut.user, n, name))
	}
}
```

**File:** src/encoding/gob/doc.go (L167-176)
```go
Interface types are not checked for compatibility; all interface types are
treated, for transmission, as members of a single "interface" type, analogous to
int or []byte - in effect they're all treated as interface{}. Interface values
are transmitted as a string identifying the concrete type being sent (a name
that must be pre-defined by calling [Register]), followed by a byte count of the
length of the following data (so the value can be skipped if it cannot be
stored), followed by the usual encoding of concrete (dynamic) value stored in
the interface value. (A nil interface value is identified by the empty string
and transmits no value.) Upon receipt, the decoder verifies that the unpacked
concrete item satisfies the interface of the receiving variable.
```

**File:** src/encoding/json/v2/arshal_default.go (L1285-1300)
```go
	fncs.unmarshal = func(dec *jsontext.Decoder, va addressableValue, uo *jsonopts.Struct) error {
		xd := export.Decoder(dec)
		if uo.Flags.Has(jsonflags.TagFlags) {
			if uo.Flags.Get(jsonflags.StringTag) && !uo.Flags.Get(jsonflags.ReportErrorsWithLegacySemantics) {
				return newUnmarshalErrorBeforeWithSkipping(dec, t, errInvalidStringTag)
			}
			if uo.Flags.Has(jsonflags.FormatTag) {
				return newInvalidFormatError(dec, t)
			}
		}
		tok, err := dec.ReadToken()
		if err != nil {
			return err
		}
		k := tok.Kind()
		switch k {
```
