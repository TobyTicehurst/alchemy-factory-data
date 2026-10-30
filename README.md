# Alchemy Factory Data

The data tables of [Alchemy Factory](https://store.steampowered.com/app/3669570/) as JSON. Most data is stored under `data/DataTables/`.


`data/` is from Steam build 25991404 (Windows, Unreal Engine 5.7.4), read with mappings dumped by UE4SS 1181 (`raw/25991404.usmap`).

## Updating `data/` After a Game Update

You need the game installed through Steam, [UE4SS](https://github.com/UE4SS-RE/RE-UE4SS) in it (the Alchemy Factory Vortex extension automatically installs it), Python 3.9 or later, and [retoc](https://github.com/trumank/retoc) and [repak](https://github.com/trumank/repak) on the PATH.

1. **Dump the mappings.** Start the game, begin a new Standard Mode save (the tutorial loads the most classes), and once it has loaded press **Ctrl + Numpad 6**. UE4SS writes `AlchemyFactory-<engine version>.usmap` to `AlchemyFactory/Binaries/Win64/ue4ss/` in the game's folder (right click the game in steam, Manage > Browse local files).
2. **Extract the tables:**

   ```
   python scripts/extract_tables.py --usmap <path-to-usmap>
   ```

   It takes a few seconds. It reads the game from its usual Steam folder (`--game` for another) and runs `retoc` and `repak` from the PATH (`--retoc` and `--repak` for elsewhere). It rewrites `data/`, and prints the game's Steam build.

The script checks the mappings two ways. The `.usmap` doesn't record which version of the game it's from, so one older than the game's files (where the layouts are) is refused. And each table must decode to exactly its end, so a table whose layout doesn't match the mappings is skipped with a warning; if any are, dump the mappings again.

### What's in `data/`

- **`data/<path under Content>.json`:** each DataTable, e.g. `data/DataTables/DT_Enemies.json`, as `{"row_struct": ..., "rows": {row name: {property: value}}}` (plus `"parent_tables"` for a composite table), and each curve as its properties. The game began as a belt tower defence prototype, so its code calls items "Enemies": `DT_Enemies` is the items and `DT_EnemyCrafting` the recipes.
- **`data/schemas.json`:** the layouts used, i.e. the tables' row structs and every struct and enum inside them.

Enums are written as their names, references to other objects as full object paths (`/Game/Arts/UI/Ingredients/T_UI_PortalSigil.T_UI_PortalSigil`), and texts in English. A property a row doesn't store has the default from the game's code, which the mappings don't include, so it's `null`.

The script finds the tables by name first (paths containing `DT_`, `ST_`, `DataTable`, `Curve` or `CV_`), then keeps those whose class is a DataTable, curve or string table. A table named otherwise would be missed.

## Dumping the Other Data

UE4SS can dump three more things from the running game. They're kept out of git (see `.gitignore`), as they're large and only some mods need them. Begin a brand new save as above, then press each one's keys. Each is written to `AlchemyFactory/Binaries/Win64/ue4ss/`, and `ue4ss/UE4SS.log` says when it's done. The keys are set in `ue4ss/Mods/Keybinds/Scripts/main.lua`, so UE4SS's `Keybinds` mod must be enabled.

| Keys | Dump | Written as |
|---|---|---|
| Ctrl + Numpad 6 | Mappings | `AlchemyFactory-<engine version>.usmap` |
| Ctrl + Numpad 5 | Reflection data | `AlchemyFactory-<engine version>.jmap` |
| Ctrl + Numpad 9 | Headers for Unreal's header tool | `UHTHeaderDump/` |
| Ctrl + H | C++ SDK | `CXXHeaderDump/` |

The header dumps take a minute or two. Before closing the game, check that `UHTHeaderDump/BeltTD/Public/` has `.h` files: an unfinished dump has only `.cpp` files.

## What Each Dump Holds

All four come from the reflection data Unreal keeps in the running game. The game's own C++ module is `BeltTD`.

### Mappings (`.usmap`)

A compact binary file of every struct's and class's property names in order, with their types, and every enum's names and values, Blueprint-generated types included. It holds no offsets, code or values.

The game's assets are cooked with unversioned properties: they store values without names or types, so reading them needs these layouts. `scripts/extract_tables.py` uses them, as do [FModel](https://fmodel.app), CUE4Parse and similar tools.

### Reflection Data (`.jmap`)

A JSON file of the whole reflection data (about 37,000 objects), in the [jmap](https://github.com/trumank/jmap) format. Beyond the mappings, it has each property's memory offset, size and flags, each class's super chain, each function's parameters, flags and native function address, and the classes' vtables. It's for working with the game in memory or in its executable, not for reading assets. About 100 MB.

### Headers for Unreal's Header Tool (`UHTHeaderDump/`)

The game's C++ classes and structs written back out as source, without the code inside functions: one folder per module, with `Public/*.h` (the `UCLASS`, `USTRUCT`, `UPROPERTY` and `UFUNCTION` declarations), `Private/*.cpp` (constructors setting each property's default, and empty function bodies) and a `Build.cs`. It doesn't include Blueprints, which are assets in the game's paks.

An Unreal project can compile these into classes with the game's names and layouts, so assets made in it load in the game. Only `BeltTD` is needed for that: its `Build.cs` depends on modules that come with Unreal 5.7. The game also bundles four third-party plugins, dumped beside it: `TweenMaker`, `UniversalMPIndicator`, `AdvancedSessions` and `AdvancedSteamSessions`. The other folders are the engine's own modules.

### C++ SDK (`CXXHeaderDump/`)

One `.hpp` per module listing each class's members at their memory offsets, Blueprint classes included (for example `BP_BeltTDGameState.hpp`). It's for C++ mods that read or write the game's objects directly.
