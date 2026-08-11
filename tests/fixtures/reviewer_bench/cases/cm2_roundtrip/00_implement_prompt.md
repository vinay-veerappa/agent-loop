# TICKET CM2: Copier slice 3a: LoadFromDisk must not silently drop the fields SaveToDisk writes
## Defect
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
## Additional context you must respect
CopierRelationship is declared at TradeCopierEngine.cs:25 and CopierGroup at :70; both carry the full field set, and CopierGroup.ToRelationships() at :87 already copies every one of them, including deep copies of the two dictionaries with StringComparer.OrdinalIgnoreCase preserved. That method is a working example of the complete list.

The acceptance tests are in RiskGuardAddOnTests.cs under the 'CM2' header and are already red. They use `new TradeCopierEngine()` rather than the singleton, `UpsertRelationship` / `UpsertGroup` to populate, and `GetRelationships()` / `GetGroup(name)` to read back.

Relevant history: P1-23 and P0-9 DELETED two config fields rather than leave them settable-but-unread, on the rule that config must not lie. This ticket takes the other branch of the same rule -- these fields have a real consumer in slice 1's sizing branch, so they are made to work rather than removed.
## Regions to rewrite
### REGION id="R1"  file=scripts/ninjatrader/addons/TradeCopierEngine.cs  lines 688-800
Purpose: The whole method, lines 688-799. It contains all three construction sites -- structured relationships, groups, and the flat legacy dictionary -- which is why this is one region and not three: they must end up agreeing, and nested or adjacent spans covering one method is what O47 now rejects.
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
Return one block per region id above, in the same order. No other output.