# TICKET CM2: Copier slice 3a: LoadFromDisk must not silently drop the fields SaveToDisk writes

## Defect the patch claims to fix
The copier config round trip is lossy in one direction only. SaveToDisk (TradeCopierEngine.cs:802) serialises each relationship and group whole, with JsonConvert.SerializeObject, so EVERY property reaches the file. LoadFromDisk (TradeCopierEngine.cs:688) hand-parses a remembered subset of property names at THREE separate construction sites: structured Relationships (around line 713), Groups (around line 749), and the flat legacy dictionary (around line 776). None of the three reads SizingMode, Mode, PerTickerRatios, CustomSymbolMappings or StealthMode. Only the relationship site reads MaxSlippageTicks; the group site does not. The consequence is not that these fields cannot be configured -- it is worse. They are written to disk, they are visible in the file, they look set, and loading returns them to their defaults with no error. That is P2-41's shape, where the config reports what you asked for and applies something else. SizingMode is one of the dropped fields, so CopierSizingMode.PerTickerMatrix -- the whole of slice 1, already shipped and green -- cannot be selected by any means except editing C# and recompiling.

## Required change
After the fix, a relationship or group saved by SaveToDisk and reloaded by LoadFromDisk must come back with SizingMode, Mode, PerTickerRatios, CustomSymbolMappings, StealthMode and MaxSlippageTicks intact, at ALL THREE construction sites, and a relationship that round trips through disk must size a fill from its ratio table.

Six constraints, each of which a plausible implementation gets wrong:

1. DO NOT set ObjectCreationHandling.Replace on the serializer. P1-39 is explicit about this: it discards the property initialisers' StringComparer.OrdinalIgnoreCase and makes every instrument lookup case-sensitive, so a root arriving as 'mes' matches no rule and, under slice 1, refuses the entry. A test pins the comparer.

2. Keep the camelCase ALIASES. 'leaderAccount', 'followerAccount', 'quantityRatio', 'maxPositionSize' and the rest are different NAMES, not different cases of the PascalCase ones. Json.NET matches property names case-insensitively and will not map them for you. A test pins all three of leaderAccount, quantityRatio and maxPositionSize.

3. An unrecognised ENUM value must NOT throw. LoadFromDisk clears _relationships and _groups before parsing and wraps the whole body in one try/catch, so anything that throws leaves the engine holding NO configuration -- one typo in copier_config.json would silently disarm every relationship. A green-at-baseline test pins this and the fix must not break it.

   BUT tolerating an unrecognised enum name is NOT the same as tolerating every deserialisation error, and widening it that far is a naked-risk regression. A blanket Json.NET Error handler that swallows everything leaves a type-mismatched field at the CLR default instead of its property initialiser's value: MaxPositionSize becomes 0 rather than 100, and QuantityRatio becomes 0.0 rather than 1.0. A zero cap or a zero ratio sizes every fill at nothing, so the leader trades and the follower does not. A malformed NUMBER must fail closed -- refusing the entry is fine, keeping the intended default is fine, silently producing zero is not. A second green-at-baseline test pins this. This is not hypothetical: a review panel found exactly this in a candidate that had already passed every mechanical gate.

4. The flat legacy form -- a bare object with no Relationships/Groups wrapper, whose KEY is the leader account name -- must keep working, and must carry the new fields too.

5. Fields absent from the file must still get their existing defaults. ArmedForLive in particular defaults to FALSE and must never be defaulted to true by a deserialisation path.

6. Three sites, one behaviour. If the three construction sites keep three separate lists of remembered field names, the next field added will be dropped by whichever site the author forgets -- which is the defect being fixed, not a fix for it. Prefer one shared parse that all three use.

## Mechanical gates already passed
static: 1 block(s) well-formed; compile: build succeeded; test: no regressions; 830 passed, 0 failed, 10 expected failure(s) now green; all 10 acceptance test(s) green; lock-scope: no risk calls under _stateLock

## SETTLED DECISIONS - AUTHORITATIVE, DO NOT RE-LITIGATE
The arbiter has already decided these. They SUPERSEDE the ticket text wherever they conflict. Do NOT raise a finding that contradicts one, and do not report directive-compliant code as a spec violation.

- CoveredQuantity is the SUM over every live protective stop on the position, and both it and RecognizedStopOrder are DERIVED from PositionGuardFsm's stop list -- neither is assignable (P1-36, closed 2026-08-07).
- NT8 raises ExecutionUpdate BEFORE PositionUpdate. Code that reads account.Positions from an execution handler reads a position that does not exist yet on an entry fill (P0-49, closed 2026-08-07).
- The copier FAILS CLOSED ON ENTRIES, NEVER ON EXITS.
- Pending copies and recognised stops are keyed by Order OBJECT REFERENCE, never by Order.OrderId. NT8's OrderId is neither unique nor stable.
- The mirrored bracket stop carries the leader's SIGNED offset applied to the FOLLOWER's own fill. Never Math.Abs, never the leader's stop PRICE.
- Simulation accounts are identified by account.Provider == Provider.Simulator, never by a name prefix (P1-20, closed).
- Refusal log messages in PerTickerMatrix must identify the leader symbol, not the mapped target, when the mapping is cross-instrument.
- PerTickerMatrix mode must refuse entries when CustomSymbolMappings maps the leader root to a different root; cross-instrument sizing is deferred to slice 2.

## Implementer notes
- Fixed the flat-legacy leader-key regression by tracking which JSON properties were actually present during `Populate` and applying key-derived fallbacks only when the corresponding property was absent. This prevents a non-null property-initializer default (e.g., `LeaderAccountName = ""`) from blocking the fallback to the dictionary key.
- The shared `Populate` helper now returns a `HashSet<string>` of property names that were successfully resolved from the JSON (after alias mapping). `ParseRelationship` and `ParseGroup` use this set to decide whether to apply defaults for `Id`, `LeaderAccountName`, `FollowerAccountName`, `GroupName`, and `FollowerAccounts`.
- All other behavior from the previous patch is preserved: one shared parse path for all three construction sites, camelCase aliases, `StringComparer.OrdinalIgnoreCase` on the two dictionaries, enum tolerance for unknown names, and numeric/bool defaults retained on malformed values instead of silently becoming zero.
- No new config keys or persisted fields were added.


## REGION "R1" (scripts/ninjatrader/addons/TradeCopierEngine.cs)
### BEFORE
```csharp
        public void LoadFromDisk(string filePath)
        {
            if (string.IsNullOrEmpty(filePath) || !File.Exists(filePath)) return;
            try
            {
                string json = File.ReadAllText(filePath);
                var jRoot = JObject.Parse(json);

                lock (_lock)
                {
                    _relationships.Clear();
                    _groups.Clear();

                    var relsObj = jRoot["Relationships"] as JObject ?? jRoot["relationships"] as JObject;
                    var grpsObj = jRoot["Groups"] as JObject ?? jRoot["groups"] as JObject;
                    bool hasStructuredSections = relsObj != null || grpsObj != null;

                    if (hasStructuredSections)
                    {
                        if (relsObj != null)
                        {
                            foreach (var kv in relsObj)
                            {
                                if (kv.Value is JObject jObj)
                                {
                                    var rel = new CopierRelationship
                                    {
                                        Id = jObj["Id"]?.ToString() ?? Guid.NewGuid().ToString(),
                                        LeaderAccountName = jObj["LeaderAccountName"]?.ToString() ?? jObj["leaderAccount"]?.ToString() ?? (kv.Key.Contains("_") ? kv.Key.Split('_')[0] : kv.Key),
                                        FollowerAccountName = jObj["FollowerAccountName"]?.ToString() ?? jObj["followerAccount"]?.ToString() ?? (kv.Key.Contains("_") ? kv.Key.Split('_')[1] : "SimCopy2"),
                                        IsEnabled = jObj["IsEnabled"] != null ? (bool)jObj["IsEnabled"] : (jObj["isEnabled"] != null ? (bool)jObj["isEnabled"] : true),
                                        ArmedForLive = jObj["ArmedForLive"] != null ? (bool)jObj["ArmedForLive"] : (jObj["armedForLive"] != null ? (bool)jObj["armedForLive"] : false),
                                        QuantityRatio = jObj["QuantityRatio"] != null ? (double)jObj["QuantityRatio"] : (jObj["quantityRatio"] != null ? (double)jObj["quantityRatio"] : 1.0),
                                        FixedLotMode = jObj["FixedLotMode"] != null ? (bool)jObj["FixedLotMode"] : (jObj["fixedLotMode"] != null ? (bool)jObj["fixedLotMode"] : false),
                                        FixedLotSize = jObj["FixedLotSize"] != null ? (int)jObj["FixedLotSize"] : (jObj["fixedLotSize"] != null ? (int)jObj["fixedLotSize"] : 1),
                                        AutoSymbolConversion = jObj["AutoSymbolConversion"] != null ? (bool)jObj["AutoSymbolConversion"] : (jObj["autoSymbolConversion"] != null ? (bool)jObj["autoSymbolConversion"] : true),
                                        MaxPositionSize = jObj["MaxPositionSize"] != null ? (int)jObj["MaxPositionSize"] : (jObj["maxPositionSize"] != null ? (int)jObj["maxPositionSize"] : 100),
                                        DailyLossLimit = jObj["DailyLossLimit"] != null ? (double)jObj["DailyLossLimit"] : (jObj["dailyLossLimit"] != null ? (double)jObj["dailyLossLimit"] : 1000.0),
                                        IsQuarantined = jObj["IsQuarantined"] != null ? (bool)jObj["IsQuarantined"] : (jObj["isQuarantined"] != null ? (bool)jObj["isQuarantined"] : false),
                                        MaxSlippageTicks = jObj["MaxSlippageTicks"] != null ? (double)jObj["MaxSlippageTicks"] : (jObj["maxSlippageTicks"] != null ? (double)jObj["maxSlippageTicks"] : 0.0)
                                    };
                                    _relationships.Add(rel);
                                }
                            }
                        }

                        if (grpsObj != null)
                        {
                            foreach (var kv in grpsObj)
                            {
                                if (kv.Value is JObject jObj)
                                {
                                    var followers = new List<string>();
                                    var followersToken = jObj["FollowerAccounts"] ?? jObj["followerAccounts"];
                                    if (followersToken != null)
                                    {
                                        var parsed = JsonConvert.DeserializeObject<List<string>>(followersToken.ToString());
                                        if (parsed != null) followers = parsed;
                                    }

                                    var grp = new CopierGroup
                                    {
                                        Id = jObj["Id"]?.ToString() ?? Guid.NewGuid().ToString(),
                                        GroupName = jObj["GroupName"]?.ToString() ?? kv.Key,
                                        LeaderAccountName = jObj["LeaderAccountName"]?.ToString() ?? jObj["leaderAccount"]?.ToString() ?? "Sim101",
                                        IsEnabled = jObj["IsEnabled"] != null ? (bool)jObj["IsEnabled"] : (jObj["isEnabled"] != null ? (bool)jObj["isEnabled"] : true),
                                        ArmedForLive = jObj["ArmedForLive"] != null ? (bool)jObj["ArmedForLive"] : (jObj["armedForLive"] != null ? (bool)jObj["armedForLive"] : false),
                                        QuantityRatio = jObj["QuantityRatio"] != null ? (double)jObj["QuantityRatio"] : (jObj["quantityRatio"] != null ? (double)jObj["quantityRatio"] : 1.0),
                                        FixedLotMode = jObj["FixedLotMode"] != null ? (bool)jObj["FixedLotMode"] : (jObj["fixedLotMode"] != null ? (bool)jObj["fixedLotMode"] : false),
                                        FixedLotSize = jObj["FixedLotSize"] != null ? (int)jObj["FixedLotSize"] : (jObj["fixedLotSize"] != null ? (int)jObj["fixedLotSize"] : 1),
                                        AutoSymbolConversion = jObj["AutoSymbolConversion"] != null ? (bool)jObj["AutoSymbolConversion"] : (jObj["autoSymbolConversion"] != null ? (bool)jObj["autoSymbolConversion"] : true),
                                        MaxPositionSize = jObj["MaxPositionSize"] != null ? (int)jObj["MaxPositionSize"] : (jObj["maxPositionSize"] != null ? (int)jObj["maxPositionSize"] : 100),
                                        DailyLossLimit = jObj["DailyLossLimit"] != null ? (double)jObj["DailyLossLimit"] : (jObj["dailyLossLimit"] != null ? (double)jObj["dailyLossLimit"] : 1000.0),
                                        FollowerAccounts = followers
                                    };
                                    _groups.Add(grp);
                                }
                            }
                        }
                    }
                    else
                    {
                        var dict = JsonConvert.DeserializeObject<Dictionary<string, JObject>>(json);
                        if (dict != null)
                        {
                            foreach (var kv in dict)
                            {
                                var jObj = kv.Value;
                                var rel = new CopierRelationship
                                {
                                    LeaderAccountName = jObj["LeaderAccountName"]?.ToString() ?? jObj["leaderAccount"]?.ToString() ?? kv.Key,
                                    FollowerAccountName = jObj["FollowerAccountName"]?.ToString() ?? jObj["followerAccount"]?.ToString() ?? "SimCopy2",
                                    IsEnabled = jObj["IsEnabled"] != null ? (bool)jObj["IsEnabled"] : (jObj["isEnabled"] != null ? (bool)jObj["isEnabled"] : true),
                                    ArmedForLive = jObj["ArmedForLive"] != null ? (bool)jObj["ArmedForLive"] : (jObj["armedForLive"] != null ? (bool)jObj["armedForLive"] : false),
                                    QuantityRatio = jObj["QuantityRatio"] != null ? (double)jObj["QuantityRatio"] : (jObj["quantityRatio"] != null ? (double)jObj["quantityRatio"] : 1.0),
                                    FixedLotMode = jObj["FixedLotMode"] != null ? (bool)jObj["FixedLotMode"] : (jObj["fixedLotMode"] != null ? (bool)jObj["fixedLotMode"] : false),
                                    FixedLotSize = jObj["FixedLotSize"] != null ? (int)jObj["FixedLotSize"] : (jObj["fixedLotSize"] != null ? (int)jObj["fixedLotSize"] : 1),
                                    AutoSymbolConversion = jObj["AutoSymbolConversion"] != null ? (bool)jObj["AutoSymbolConversion"] : (jObj["autoSymbolConversion"] != null ? (bool)jObj["autoSymbolConversion"] : true),
                                    MaxPositionSize = jObj["MaxPositionSize"] != null ? (int)jObj["MaxPositionSize"] : (jObj["maxPositionSize"] != null ? (int)jObj["maxPositionSize"] : 100),
                                    DailyLossLimit = jObj["DailyLossLimit"] != null ? (double)jObj["DailyLossLimit"] : (jObj["dailyLossLimit"] != null ? (double)jObj["dailyLossLimit"] : 1000.0),
                                    IsQuarantined = jObj["IsQuarantined"] != null ? (bool)jObj["IsQuarantined"] : (jObj["isQuarantined"] != null ? (bool)jObj["isQuarantined"] : false)
                                };
                                _relationships.Add(rel);
                            }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[LoadFromDisk EXCEPTION] {ex}");
            }
        }
```
### AFTER (proposed)
```csharp
        public void LoadFromDisk(string filePath)
        {
            if (string.IsNullOrEmpty(filePath) || !File.Exists(filePath)) return;
            try
            {
                string json = File.ReadAllText(filePath);
                var jRoot = JObject.Parse(json);

                var aliasMap = new Dictionary<string, string>(StringComparer.Ordinal)
                {
                    { "id", "Id" },
                    { "leaderAccount", "LeaderAccountName" },
                    { "followerAccount", "FollowerAccountName" },
                    { "isEnabled", "IsEnabled" },
                    { "armedForLive", "ArmedForLive" },
                    { "quantityRatio", "QuantityRatio" },
                    { "fixedLotMode", "FixedLotMode" },
                    { "fixedLotSize", "FixedLotSize" },
                    { "autoSymbolConversion", "AutoSymbolConversion" },
                    { "maxPositionSize", "MaxPositionSize" },
                    { "dailyLossLimit", "DailyLossLimit" },
                    { "isQuarantined", "IsQuarantined" },
                    { "maxSlippageTicks", "MaxSlippageTicks" },
                    { "sizingMode", "SizingMode" },
                    { "mode", "Mode" },
                    { "stealthMode", "StealthMode" },
                    { "perTickerRatios", "PerTickerRatios" },
                    { "customSymbolMappings", "CustomSymbolMappings" },
                    { "groupName", "GroupName" },
                    { "followerAccounts", "FollowerAccounts" }
                };

                bool TryConvertToken(JToken token, Type targetType, out object value)
                {
                    value = null;
                    if (token == null || token.Type == JTokenType.Null)
                        return false;

                    try
                    {
                        if (targetType == typeof(string))
                        {
                            value = token.Type == JTokenType.String ? token.Value<string>() : token.ToString();
                            return true;
                        }

                        if (targetType == typeof(bool))
                        {
                            if (token.Type == JTokenType.Boolean)
                            {
                                value = token.Value<bool>();
                                return true;
                            }
                            if (token.Type == JTokenType.String)
                            {
                                string s = token.Value<string>();
                                if (bool.TryParse(s, out bool b))
                                {
                                    value = b;
                                    return true;
                                }
                            }
                            return false;
                        }

                        if (targetType == typeof(int))
                        {
                            if (token.Type == JTokenType.Integer)
                            {
                                value = token.Value<int>();
                                return true;
                            }
                            if (token.Type == JTokenType.String)
                            {
                                string s = token.Value<string>();
                                if (int.TryParse(s, out int i))
                                {
                                    value = i;
                                    return true;
                                }
                            }
                            return false;
                        }

                        if (targetType == typeof(double))
                        {
                            if (token.Type == JTokenType.Integer || token.Type == JTokenType.Float)
                            {
                                value = token.Value<double>();
                                return true;
                            }
                            if (token.Type == JTokenType.String)
                            {
                                string s = token.Value<string>();
                                if (double.TryParse(s, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out double d))
                                {
                                    value = d;
                                    return true;
                                }
                            }
                            return false;
                        }

                        if (targetType.IsEnum)
                        {
                            if (token.Type == JTokenType.String)
                            {
                                string s = token.Value<string>();
                                if (Enum.TryParse(targetType, s, true, out object e) && Enum.IsDefined(targetType, e))
                                {
                                    value = e;
                                    return true;
                                }
                            }
                            else if (token.Type == JTokenType.Integer)
                            {
                                int i = token.Value<int>();
                                object e = Enum.ToObject(targetType, i);
                                if (Enum.IsDefined(targetType, e))
                                {
                                    value = e;
                                    return true;
                                }
                            }
                            return false;
                        }

                        if (targetType == typeof(Dictionary<string, double>))
                        {
                            if (token.Type != JTokenType.Object)
                                return false;
                            var dict = new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase);
                            foreach (JProperty p in ((JObject)token).Properties())
                            {
                                object dv;
                                if (TryConvertToken(p.Value, typeof(double), out dv))
                                    dict[p.Name] = (double)dv;
                            }
                            value = dict;
                            return true;
                        }

                        if (targetType == typeof(Dictionary<string, string>))
                        {
                            if (token.Type != JTokenType.Object)
                                return false;
                            var dict = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                            foreach (JProperty p in ((JObject)token).Properties())
                            {
                                dict[p.Name] = p.Value.Type == JTokenType.String ? p.Value.Value<string>() : p.Value.ToString();
                            }
                            value = dict;
                            return true;
                        }

                        if (targetType == typeof(List<string>))
                        {
                            if (token.Type != JTokenType.Array)
                                return false;
                            var list = new List<string>();
                            foreach (JToken t in ((JArray)token))
                            {
                                list.Add(t.Type == JTokenType.String ? t.Value<string>() : t.ToString());
                            }
                            value = list;
                            return true;
                        }

                        try
                        {
                            value = token.ToObject(targetType);
                            return true;
                        }
                        catch
                        {
                            return false;
                        }
                    }
                    catch
                    {
                        return false;
                    }
                }

                HashSet<string> Populate(JObject jObj, object target)
                {
                    var present = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                    Type type = target.GetType();
                    foreach (JProperty jp in jObj.Properties())
                    {
                        string propName;
                        if (!aliasMap.TryGetValue(jp.Name, out propName))
                            propName = jp.Name;

                        System.Reflection.PropertyInfo prop = type.GetProperty(propName,
                            System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.IgnoreCase);
                        if (prop == null || prop.GetSetMethod() == null)
                            continue;

                        object value;
                        if (TryConvertToken(jp.Value, prop.PropertyType, out value))
                        {
                            prop.SetValue(target, value, null);
                            present.Add(prop.Name);
                        }
                    }
                    return present;
                }

                CopierRelationship ParseRelationship(JObject jObj, string key)
                {
                    var rel = new CopierRelationship();
                    HashSet<string> present = Populate(jObj, rel);
                    if (!present.Contains("Id"))
                        rel.Id = Guid.NewGuid().ToString();
                    if (!present.Contains("LeaderAccountName"))
                        rel.LeaderAccountName = key.Contains("_") ? key.Split('_')[0] : key;
                    if (!present.Contains("FollowerAccountName"))
                        rel.FollowerAccountName = key.Contains("_") ? key.Split('_')[1] : "SimCopy2";
                    return rel;
                }

                CopierGroup ParseGroup(JObject jObj, string key)
                {
                    var grp = new CopierGroup();
                    HashSet<string> present = Populate(jObj, grp);
                    if (!present.Contains("Id"))
                        grp.Id = Guid.NewGuid().ToString();
                    if (!present.Contains("GroupName"))
                        grp.GroupName = key;
                    if (!present.Contains("LeaderAccountName"))
                        grp.LeaderAccountName = "Sim101";
                    if (!present.Contains("FollowerAccounts"))
                        grp.FollowerAccounts = new List<string>();
                    return grp;
                }

                lock (_lock)
                {
                    _relationships.Clear();
                    _groups.Clear();

                    var relsObj = jRoot["Relationships"] as JObject ?? jRoot["relationships"] as JObject;
                    var grpsObj = jRoot["Groups"] as JObject ?? jRoot["groups"] as JObject;
                    bool hasStructuredSections = relsObj != null || grpsObj != null;

                    if (hasStructuredSections)
                    {
                        if (relsObj != null)
                        {
                            foreach (var kv in relsObj)
                            {
                                JObject jObj = kv.Value as JObject;
                                if (jObj != null)
                                    _relationships.Add(ParseRelationship(jObj, kv.Key));
                            }
                        }

                        if (grpsObj != null)
                        {
                            foreach (var kv in grpsObj)
                            {
                                JObject jObj = kv.Value as JObject;
                                if (jObj != null)
                                    _groups.Add(ParseGroup(jObj, kv.Key));
                            }
                        }
                    }
                    else
                    {
                        var dict = JsonConvert.DeserializeObject<Dictionary<string, JObject>>(json);
                        if (dict != null)
                        {
                            foreach (var kv in dict)
                            {
                                _relationships.Add(ParseRelationship(kv.Value, kv.Key));
                            }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[LoadFromDisk EXCEPTION] {ex}");
            }
        }
```

## LEARNING FEEDBACK (from prior tickets)

### Known false positives (arbiter REJECTED these - do NOT re-raise):
- REJECTED: R1: The flat-legacy path now iterates `jRoot.Properties()` directly instead of using `JsonConvert.DeserializeObject<Dict
- REJECTED: R1: The `NormalizeAliases` method iterates `source.Properties()` twice. The first loop copies non-alias properties; the 
- REJECTED: R2: When `rel` is non-null but `rel.SizingMode` is not `PerTickerMatrix`, the auto-table guard condition is:
- REJECTED: R1: The `if (rawCopyQty < 1 && isExit) { rawCopyQty = 1; }` block in the matrix branch is redundant with the existing su
- REJECTED: R1: The no-rule exit path has the same defect. When `hasRatio` is false and `isExit` is true:

### Known real defects (arbiter UPHELD these - keep flagging if you see them):
- UPHELD: R1: The `Error` handler in `loadSettings` swallows ALL deserialisation errors silently: `Error = (sender, args) => { arg
- UPHELD: R1: `ApplyRelationshipDefaults` is called AFTER `JsonConvert.DeserializeObject`, but it checks `HasField(normalized, ...
- UPHELD: R1: `NormalizeAliases` drops every alias property from the output when the canonical name is already present, but it als
- UPHELD: R1/R2: The patch allows cross-instrument sizing in PerTickerMatrix mode when `CustomSymbolMappings` maps the leader root
- UPHELD: R1: When `CustomSymbolMappings` maps to a different root and there is no `PerTickerRatios` entry for the mapped symbol, 