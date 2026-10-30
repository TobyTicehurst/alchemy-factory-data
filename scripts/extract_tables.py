"""Extract every DataTable and curve from the installed game into JSON, using the layouts in a .usmap.

    python scripts/extract_tables.py --usmap file [--game folder] [--output folder] [--retoc exe] [--repak exe]

Needs retoc and repak (https://github.com/trumank/retoc, https://github.com/trumank/repak), on the PATH or given
with --retoc and --repak. The defaults are the game's Steam folder and data/.

The mappings must be from the installed version of the game. The .usmap doesn't record which that is, so a .usmap
older than the game's exe or containers (where the layouts are) is refused. Decoding checks the layouts too: each
table must end exactly where the layouts say, so a table that doesn't fit them is skipped with a warning.

1. Unpack: retoc converts the game's IoStore containers to legacy .uasset/.uexp files, only for packages whose path
   has a table's, string table's or curve's usual name in it (DT_, ST_, DataTable, Curve, CV_). Each converted
   package's main object is then checked by class, so only DataTables, curves and string tables are kept. repak
   reads the English Game.locres from the .pak.
2. Decode: each DataTable and curve is read with the layouts from the .usmap, its texts in English, and written to
   <output>/<its path under Content>.json, e.g. data/DataTables/DT_Enemies.json:
   {"row_struct": "BeltTDEnemyConfig", "rows": {row name: {property: value}}}, plus "parent_tables" for a
   CompositeDataTable. A curve is written as its properties.
3. Schemas: the layouts that were used (the tables' classes, their row structs and everything inside them) are
   written to <output>/schemas.json.

Values: enums as their enumerator names, object references as full object paths
(/Game/Arts/UI/Ingredients/T_UI_PortalSigil.T_UI_PortalSigil), texts as English strings. A property the file doesn't
store has the struct's default value, which comes from the game's code and isn't in the .usmap, so it's null; one
the file marks as zero is written as zero.
"""

import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Alchemy Factory")
STEAM_APP = 3669570

# The game's content (engine and plugin assets are outside it)
CONTENT = "AlchemyFactory/Content/"
LOCRES = "AlchemyFactory/Content/Localization/Game/en/Game.locres"

# Path parts that pick the packages to convert; anything they let through is checked by class afterwards
PATH_FILTERS = ["DT_", "ST_", "DataTable", "Curve", "CV_"]

TABLE_CLASSES = ["DataTable", "CompositeDataTable"]
CURVE_CLASSES = ["CurveFloat", "CurveLinearColor"]
STRING_TABLE_CLASSES = ["StringTable"]

# Structs the engine serializes natively, as their fields in order with no property header, rather than as
# unversioned properties. Their fields come from the .usmap like any other struct.
NATIVE_STRUCTS = [
    "Color",
    "DeprecateSlateVector2D",
    "Guid",
    "IntPoint",
    "IntVector",
    "LinearColor",
    "Quat",
    "RichCurveKey",
    "Rotator",
    "Vector",
    "Vector2D",
    "Vector2f",
    "Vector4",
]


class Reader:
    """Little-endian reader with Unreal's string and name forms."""

    def __init__(self, data, names=()):
        self.data = data
        self.position = 0
        self.names = names

    def take(self, length):
        if self.position + length > len(self.data):
            raise ValueError(f"read of {length} bytes at {self.position:#x} runs past the end ({len(self.data):#x})")
        chunk = self.data[self.position : self.position + length]
        self.position += length
        return chunk

    def unpack(self, format):
        return struct.unpack("<" + format, self.take(struct.calcsize("<" + format)))[0]

    def u8(self):
        return self.unpack("B")

    def i8(self):
        return self.unpack("b")

    def u16(self):
        return self.unpack("H")

    def i16(self):
        return self.unpack("h")

    def u32(self):
        return self.unpack("I")

    def i32(self):
        return self.unpack("i")

    def u64(self):
        return self.unpack("Q")

    def i64(self):
        return self.unpack("q")

    def f32(self):
        return self.unpack("f")

    def f64(self):
        return self.unpack("d")

    def fstring(self):
        """FString: an i32 length (negative for UTF-16) including the terminating NUL."""
        length = self.i32()
        if length < 0:
            text = self.take(-2 * length).decode("utf-16-le", errors="replace")
        else:
            text = self.take(length).decode("utf-8", errors="replace")
        return text.rstrip("\0")

    def fname(self):
        """FName: an index into the package's name map plus an instance number (Name_3 is stored as 4)."""
        index, number = self.i32(), self.i32()
        if not 0 <= index < len(self.names):
            raise ValueError(f"name index {index} out of range at {self.position - 8:#x}")
        return f"{self.names[index]}_{number - 1}" if number else self.names[index]

    def unversioned_header(self):
        """The indices of the properties stored, each with whether it's zero."""
        fragments = []
        total = 0
        while True:
            packed = self.u16()
            skip, has_zeroes, is_last, count = packed & 0x7F, bool(packed & 0x80), bool(packed & 0x100), packed >> 9
            fragments.append((skip, has_zeroes, count))
            total += count
            if is_last:
                break
        zero_mask = [False] * total
        if any(has_zeroes for _, has_zeroes, _ in fragments):
            mask_bytes = 1 if total <= 8 else 2 if total <= 16 else (total + 31) // 32 * 4
            mask = self.take(mask_bytes)
            zero_mask = [bool(mask[bit // 8] >> (bit % 8) & 1) for bit in range(total)]
        present = []
        index = bit = 0
        for skip, _, count in fragments:
            index += skip
            for _ in range(count):
                present.append((index, zero_mask[bit]))
                index += 1
                bit += 1
        return present


# ---- Mappings ---------------------------------------------------------------------------------------------------

# .usmap property types by id; 16 is the old name of a soft object reference
USMAP_TYPES = [
    "Byte", "Bool", "Int", "Float", "Object", "Name", "Delegate", "Double", "Array", "Struct", "Str", "Text",
    "Interface", "MulticastDelegate", "WeakObject", "LazyObject", "SoftObject", "SoftObject", "UInt64", "UInt32",
    "UInt16", "Int64", "Int16", "Int8", "Map", "Set", "Enum", "FieldPath", "Optional", "Utf8Str", "AnsiStr",
]


def property_entry(name, type_name, array_dimension=1):
    return {"name": name, "type": type_name, "array_dimension": array_dimension}


def read_usmap(path):
    """{"structs": {name: {name, super, properties}}, "enums": {name: {value: enumerator}}} from a .usmap.

    Layout: u16 magic 0x30C4, u8 version, [i32 has versioning (+ versions)], u8 compression, u32 compressed size,
    u32 size, then names (u16 length + UTF-8), enums and structs. UE4SS adds extensions after the structs, which
    decoding doesn't need.
    """
    reader = Reader(path.read_bytes())
    if reader.u16() != 0x30C4:
        sys.exit(f"{path} is not a .usmap file")
    version = reader.u8()
    if version > 4:
        sys.exit(f"{path}: unsupported .usmap version {version}")
    if version >= 1 and reader.i32() != 0:
        reader.take(8)  # package file versions (UE4, UE5)
        reader.take(reader.i32() * 20)  # custom versions, each a GUID and a version
        reader.u32()  # net changelist
    compression = reader.u8()
    _compressed_size, size = reader.u32(), reader.u32()
    if compression != 0:
        sys.exit(f"{path} is compressed (method {compression}): dump it again without compression")
    body = Reader(reader.take(size))

    names = []
    for _ in range(body.u32()):
        length = body.u16() if version >= 2 else body.u8()
        names.append(body.take(length).decode("utf-8", errors="replace"))

    def name():
        return names[body.i32()]

    enums = {}
    for _ in range(body.u32()):
        enum_name = name()
        count = body.u16() if version >= 3 else body.u8()
        values = {}
        for i in range(count):
            value = body.i64() if version >= 4 else i
            values[str(value)] = name().rsplit("::", 1)[-1]
        enums[enum_name] = values

    def property_type():
        type_id = body.u8()
        if type_id >= len(USMAP_TYPES):
            raise ValueError(f"unknown .usmap property type {type_id}")
        entry = property_entry("", USMAP_TYPES[type_id])
        if entry["type"] == "Enum":
            underlying = property_type()["type"]
            entry["enum"] = name()
            entry["underlying"] = underlying
        elif entry["type"] == "Struct":
            entry["struct"] = name()
        elif entry["type"] in ("Array", "Set", "Optional"):
            entry["inner"] = property_type()
        elif entry["type"] == "Map":
            entry["key"] = property_type()
            entry["value"] = property_type()
        return entry

    structs = {}
    for _ in range(body.u32()):
        struct_name = name()
        super_index = body.i32()
        super_name = None if super_index == -1 else names[super_index]
        body.u16()  # property count, fixed-size array elements counted one each
        indexed = []
        for _ in range(body.u16()):
            index = body.u16()
            array_dimension = body.u8()
            property_name = name()
            entry = property_type()
            entry["name"], entry["array_dimension"] = property_name, array_dimension
            indexed.append((index, entry))
        structs[struct_name] = {"name": struct_name, "super": super_name, "properties": in_index_order(indexed)}
    return {"structs": structs, "enums": enums}


def in_index_order(indexed):
    """Properties by unversioned index. Each takes array_dimension indices; a gap (a property never serialized) is
    filled so later indices still line up."""
    properties = []
    next_index = 0
    for index, entry in sorted(indexed, key=lambda pair: pair[0]):
        while next_index < index:
            properties.append(property_entry(f"_unserialized{next_index}", "Unknown"))
            next_index += 1
        next_index = index + max(1, entry["array_dimension"])
        properties.append(entry)
    return properties


def all_properties(schemas, name):
    """Every property of a struct in unversioned index order: its own, then its super's. A fixed-size array takes
    one index per element, so each entry is (property, element)."""
    if name not in schemas["structs"]:
        raise ValueError(f"no layout for struct {name} in the .usmap")
    found = schemas["structs"][name]
    out = []
    for entry in found["properties"]:
        out.extend((entry, element) for element in range(max(1, entry["array_dimension"])))
    if found["super"]:
        out.extend(all_properties(schemas, found["super"]))
    return out


def schemas_subset(schemas, roots):
    """Only the given structs and everything they use (supers, nested structs, enums)."""
    out = {"structs": {}, "enums": {}}

    def visit(name):
        found = schemas["structs"].get(name)
        if found is None or name in out["structs"]:
            return
        out["structs"][name] = found
        pending = list(found["properties"])
        while pending:
            entry = pending.pop()
            if entry.get("enum") in schemas["enums"]:
                out["enums"][entry["enum"]] = schemas["enums"][entry["enum"]]
            if "struct" in entry:
                visit(entry["struct"])
            pending.extend(entry[key] for key in ("inner", "key", "value") if key in entry)
        if found["super"]:
            visit(found["super"])

    for root in roots:
        visit(root)
    return out


# ---- Packages ---------------------------------------------------------------------------------------------------


class Package:
    """The parts of a legacy .uasset header the decoders need: names, imports and exports, with object paths."""

    def __init__(self, uasset):
        reader = Reader(uasset)
        if reader.u32() != 0x9E2A83C1:
            raise ValueError("not a .uasset file")
        legacy_version = reader.i32()
        if legacy_version != -4:
            reader.i32()  # UE3 version
        file_version = reader.i32()
        reader.i32()  # UE5 version
        reader.i32()  # licensee version
        if file_version != 0:
            raise ValueError("a versioned package; this script reads the cooked, unversioned ones retoc writes")
        if legacy_version <= -9:
            reader.take(20)  # saved hash
            reader.i32()  # total header size
        reader.take(reader.i32() * 20)  # custom versions
        if legacy_version > -9:
            reader.i32()  # total header size
        package_name = reader.fstring()
        reader.u32()  # package flags
        name_count, name_offset = reader.i32(), reader.i32()
        reader.i32(), reader.i32()  # soft object paths
        reader.i32(), reader.i32()  # gatherable text data
        export_count, export_offset = reader.i32(), reader.i32()
        import_count, import_offset = reader.i32(), reader.i32()

        reader.position = name_offset
        self.names = []
        for _ in range(name_count):
            self.names.append(reader.fstring())
            reader.u32()  # hashes
        reader.names = self.names

        # Each import: class package, class name, outer, object name, whether optional (32 bytes)
        reader.position = import_offset
        self.imports, import_outers = [], []
        for _ in range(import_count):
            reader.fname(), reader.fname()
            import_outers.append(reader.i32())
            self.imports.append(reader.fname())
            reader.i32()

        # Each export: class, super, template, outer, object name, flags, serial size and offset, then flags and
        # dependency counts (96 bytes)
        reader.position = export_offset
        self.exports, export_outers, export_class_indices = [], [], []
        for _ in range(export_count):
            start = reader.position
            export_class_indices.append(reader.i32())
            reader.i32(), reader.i32()
            export_outers.append(reader.i32())
            self.exports.append(reader.fname())
            reader.position = start + 96

        def object_path(index):
            """An object's path is its chain of outers: an import's ends at a package import, an export's at this
            package."""
            chain = []
            current = index
            while current != 0 and len(chain) < 64:
                names, outers = (self.imports, import_outers) if current < 0 else (self.exports, export_outers)
                i = abs(current) - 1
                if i >= len(names):
                    break
                chain.append(names[i])
                if current > 0 and outers[i] == 0:
                    chain.append(package_name)
                current = outers[i]
            chain.reverse()
            return join_object_path(chain)

        self.import_paths = [object_path(-i) for i in range(1, import_count + 1)]
        self.export_paths = [object_path(i) for i in range(1, export_count + 1)]
        self.export_classes = [self.object(index) or "" for index in export_class_indices]

    def object(self, index):
        """The name of the object a package index refers to: negative for imports, positive for exports."""
        names = self.imports if index < 0 else self.exports
        return names[abs(index) - 1] if 0 < abs(index) <= len(names) else None

    def object_path(self, index):
        paths = self.import_paths if index < 0 else self.export_paths
        return paths[abs(index) - 1] if 0 < abs(index) <= len(paths) else None

    def main_class(self):
        if not self.export_classes:
            raise ValueError("the package has no exports")
        return self.export_classes[0]


def join_object_path(chain):
    """Unreal's object path form: /Game/Package.Asset:SubObject.Nested"""
    path = ""
    for i, part in enumerate(chain):
        if i == 1:
            path += "."
        elif i == 2:
            path += ":"
        elif i > 2:
            path += "."
        path += part
    return path


# ---- Texts ------------------------------------------------------------------------------------------------------


def read_locres(data):
    """{namespace: {key: string}} from a .locres file."""
    reader = Reader(data)
    reader.position = 16  # magic GUID
    version = reader.u8()
    strings_offset = reader.i64()
    if version >= 2:
        reader.u32()  # entry count
    namespaces = []
    for _ in range(reader.u32()):
        if version >= 2:
            reader.u32()  # hash
        namespace = reader.fstring()
        keys = []
        for _ in range(reader.u32()):
            if version >= 2:
                reader.u32()  # hash
            key = reader.fstring()
            reader.u32()  # source string hash
            keys.append((key, reader.i32()))
        namespaces.append((namespace, keys))
    reader.position = strings_offset
    strings = []
    for _ in range(reader.i32()):
        strings.append(reader.fstring())
        if version >= 3:
            reader.i32()  # reference count
    return {namespace: {key: strings[i] for key, i in keys if i < len(strings)} for namespace, keys in namespaces}


def read_string_table(uasset, uexp):
    """(namespace, {key: source string}) from a StringTable asset."""
    reader = Reader(uexp, Package(uasset).names)
    reader.unversioned_header()  # UStringTable sets no properties
    reader.u32()  # has GUID
    namespace = reader.fstring()
    entries = {}
    for _ in range(reader.i32()):
        key = reader.fstring()
        entries[key] = reader.fstring()
    return namespace, entries


class Localizer:
    """Turns texts into English: string tables hold the source (Chinese) strings, Game.locres the translations,
    both keyed by namespace and key."""

    def __init__(self, string_tables, locres):
        self.tables = {}
        for asset_path, uasset, uexp in string_tables:
            relative = asset_path.split("/Content/", 1)[-1]
            self.tables[f"/Game/{relative}.{relative.rsplit('/', 1)[-1]}"] = read_string_table(uasset, uexp)
        self.translations = read_locres(locres)

    def lookup(self, namespace, key):
        return self.translations.get(namespace, {}).get(key)

    def plain(self, string):
        return string

    def localized(self, namespace, key, source):
        if not key:
            return source
        translation = self.lookup(namespace, key)
        return source if translation is None else translation

    def table(self, table, key):
        if table not in self.tables:
            return key
        namespace, entries = self.tables[table]
        translation = self.lookup(namespace, key)
        if translation is not None:
            return translation
        return entries.get(key, key)


# ---- Decoding ---------------------------------------------------------------------------------------------------


def key_string(key):
    """JSON object keys must be strings: string keys as they are, anything else as its JSON text."""
    return key if isinstance(key, str) else json.dumps(key, separators=(",", ":"), ensure_ascii=False)


def store(out, entry, element, value):
    """Store value as the property's value, or as element `element` of it for a fixed-size array."""
    if entry["array_dimension"] > 1 and isinstance(out.get(entry["name"]), list):
        out[entry["name"]][element] = value
    else:
        out[entry["name"]] = value


def struct_of(schemas, name, fill):
    """Every property of a struct at the value fill gives it (fixed-size arrays as lists of it)."""
    out = {}
    for entry, _ in all_properties(schemas, name):
        if entry["name"] not in out:
            value = fill(entry)
            if entry["array_dimension"] > 1:
                value = [value] * entry["array_dimension"]
            out[entry["name"]] = value
    return out


def zero_struct(schemas, name):
    return struct_of(schemas, name, lambda entry: zero_value(schemas, entry))


def enum_name(schemas, entry, value):
    """An enum value's name, or the number if it has none (e.g. craft type 26, past Max)."""
    return schemas["enums"].get(entry.get("enum"), {}).get(str(value), value)


def zero_value(schemas, entry):
    """The value a property has when the header marks it as zero."""
    type_name = entry["type"]
    if type_name == "Struct":
        return [] if entry["struct"] == "GameplayTagContainer" else zero_struct(schemas, entry["struct"])
    if type_name == "Enum" or (type_name == "Byte" and "enum" in entry):
        return enum_name(schemas, entry, 0)
    if type_name in ("Byte", "Int8", "Int16", "Int", "Int64", "UInt16", "UInt32", "UInt64"):
        return 0
    if type_name in ("Float", "Double"):
        return 0.0
    return {"Bool": False, "Name": "None", "Str": "", "Text": "", "Array": [], "Set": [], "Map": {}}.get(type_name)


class Decoder:
    def __init__(self, data, package, schemas, localizer):
        self.reader = Reader(data, package.names)
        self.package = package
        self.schemas = schemas
        self.localizer = localizer

    def text(self):
        reader = self.reader
        reader.u32()  # flags
        history = reader.i8()
        if history == -1:
            return self.localizer.plain(reader.fstring() if reader.u32() else "")
        if history == 0:
            return self.localizer.localized(reader.fstring(), reader.fstring(), reader.fstring())
        if history == 11:
            return self.localizer.table(reader.fname(), reader.fstring())
        raise ValueError(f"unsupported text history {history} at {reader.position:#x}")

    def object(self, class_name):
        """An object's own data: its class's properties, then whether it has a GUID (and the GUID)."""
        properties = self.struct_value(class_name)
        if self.reader.u32():
            self.reader.take(16)
        return properties

    def struct_value(self, name):
        if name == "GameplayTagContainer":
            # Serialized natively as just its tag names
            return [self.reader.fname() for _ in range(self.reader.i32())]
        properties = all_properties(self.schemas, name)
        if name in NATIVE_STRUCTS:
            out = zero_struct(self.schemas, name)  # every field is stored
            for entry, element in properties:
                store(out, entry, element, self.value(entry))
            return out
        out = struct_of(self.schemas, name, lambda entry: None)
        for index, is_zero in self.reader.unversioned_header():
            if index >= len(properties):
                raise ValueError(f"{name}: property index {index} out of range")
            entry, element = properties[index]
            try:
                value = zero_value(self.schemas, entry) if is_zero else self.value(entry)
            except ValueError as error:
                raise ValueError(f"{name}.{entry['name']}: {error}") from None
            store(out, entry, element, value)
        return out

    def value(self, entry):
        reader = self.reader
        type_name = entry["type"]
        simple = {"Int8": reader.i8, "Int16": reader.i16, "Int": reader.i32, "Int64": reader.i64,
                  "UInt16": reader.u16, "UInt32": reader.u32, "UInt64": reader.u64, "Float": reader.f32,
                  "Double": reader.f64}
        if type_name in simple:
            return simple[type_name]()
        if type_name == "Byte":
            value = reader.u8()
            return enum_name(self.schemas, entry, value) if "enum" in entry else value
        if type_name == "Enum":
            readers = {"Byte": reader.u8, "Int": reader.i32, "UInt16": reader.u16, "UInt32": reader.u32,
                       "Int64": reader.i64}
            underlying = entry.get("underlying", "Byte")
            if underlying not in readers:
                raise ValueError(f"unsupported enum underlying type {underlying}")
            return enum_name(self.schemas, entry, readers[underlying]())
        if type_name == "Bool":
            return reader.u8() != 0
        if type_name == "Name":
            return reader.fname()
        if type_name == "Str":
            return reader.fstring()
        if type_name == "Text":
            return self.text()
        if type_name in ("Object", "Interface", "WeakObject", "LazyObject"):
            index = reader.i32()
            if index == 0:
                return None
            path = self.package.object_path(index)
            if path is None:
                raise ValueError(f"object reference {index} isn't in the package")
            return path
        if type_name == "SoftObject":
            package_name, asset_name, sub_path = reader.fname(), reader.fname(), reader.fstring()
            if package_name == "None":
                return None
            return f"{package_name}.{asset_name}" + (f":{sub_path}" if sub_path else "")
        if type_name == "Struct":
            return self.struct_value(entry["struct"])
        if type_name in ("Array", "Set"):
            if type_name == "Set" and reader.i32() != 0:
                raise ValueError("a set listing elements to remove isn't supported")
            return [self.element(entry["inner"]) for _ in range(reader.i32())]
        if type_name == "Map":
            if reader.i32() != 0:
                raise ValueError("a map listing keys to remove isn't supported")
            out = {}
            for _ in range(reader.i32()):
                key = key_string(self.element(entry["key"]))
                out[key] = self.element(entry["value"])
            return out
        raise ValueError(f"unsupported property type {type_name} at {reader.position:#x}")

    def element(self, entry):
        """Container elements: enums are stored as FNames ("EType::Value") rather than numbers."""
        if entry["type"] == "Enum":
            return self.reader.fname().rsplit("::", 1)[-1]
        return self.value(entry)

    def finish(self):
        """Fail unless exactly the 4-byte package tag is left, which shows the layout was right."""
        left = len(self.reader.data) - self.reader.position
        if left != 4:
            raise ValueError(f"{left} bytes left after decoding (expected 4); the layout is probably wrong")


def read_datatable(uasset, uexp, schemas, localizer):
    package = Package(uasset)
    decoder = Decoder(uexp, package, schemas, localizer)
    class_name = package.main_class()
    properties = decoder.object(class_name)
    if not isinstance(properties.get("RowStruct"), str):
        raise ValueError("the DataTable has no RowStruct")
    row_struct = properties["RowStruct"].rsplit(".", 1)[-1]
    rows = {}
    for _ in range(decoder.reader.i32()):
        name = decoder.reader.fname()
        try:
            rows[name] = decoder.struct_value(row_struct)
        except ValueError as error:
            raise ValueError(f"row {name}: {error}") from None
    decoder.finish()
    out = {"row_struct": row_struct}
    if class_name == "CompositeDataTable":
        out["parent_tables"] = properties["ParentTables"]
    out["rows"] = rows
    return out


def read_object(uasset, uexp, schemas, localizer):
    """An object whose data is only its properties, such as a curve."""
    package = Package(uasset)
    decoder = Decoder(uexp, package, schemas, localizer)
    properties = decoder.object(package.main_class())
    decoder.finish()
    return properties


# ---- Extraction -------------------------------------------------------------------------------------------------


def find_tool(given, name):
    tool = given or shutil.which(name)
    if not tool or not Path(tool).exists():
        sys.exit(f"{name} not found: put it on the PATH or give it with --{name} (https://github.com/trumank/{name})")
    return str(tool)


def unpack(retoc, repak, game, work):
    """{asset path: (class, .uasset bytes, .uexp bytes)} for the tables, curves and string tables, and the English
    Game.locres. Asset paths are like AlchemyFactory/Content/DataTables/DT_Enemies."""
    paks = game / "AlchemyFactory" / "Content" / "Paks"
    legacy = work / "legacy"
    for path_filter in PATH_FILTERS:
        subprocess.run([retoc, "to-legacy", "--no-shaders", "--no-script-objects", "-f", path_filter, str(paks),
                        str(legacy)], check=True, stdout=subprocess.DEVNULL)
    wanted = TABLE_CLASSES + CURVE_CLASSES + STRING_TABLE_CLASSES
    assets = {}
    for uasset_path in sorted(legacy.rglob("*.uasset")):
        asset_path = uasset_path.relative_to(legacy).with_suffix("").as_posix()
        if not asset_path.startswith(CONTENT):
            continue
        uasset = uasset_path.read_bytes()
        try:
            class_name = Package(uasset).main_class()
        except (ValueError, struct.error, IndexError) as error:
            print(f"warning: skipped {asset_path}: couldn't read its header: {error}", file=sys.stderr)
            continue
        if class_name in wanted:
            assets[asset_path] = (class_name, uasset, uasset_path.with_suffix(".uexp").read_bytes())
    locres = subprocess.run([repak, "get", str(paks / "AlchemyFactory-Windows.pak"), LOCRES], check=True,
                            capture_output=True).stdout
    return assets, locres


def steam_build(game):
    """The installed Steam build, from Steam's manifest beside the game's folder, or None if there isn't one."""
    manifest = game.parent.parent / f"appmanifest_{STEAM_APP}.acf"
    if not manifest.is_file():
        return None
    found = re.search(r'"buildid"\s+"(\d+)"', manifest.read_text(encoding="utf-8", errors="replace"))
    return found.group(1) if found else None


def check_usmap_is_current(usmap, game):
    """Refuse a .usmap older than the game's files: the native layouts are in the exe and the Blueprint ones in the
    containers, so if either changed after the dump, the mappings may not be the game's."""
    files = [game / "AlchemyFactory" / "Binaries" / "Win64" / "AlchemyFactory-Win64-Shipping.exe",
             *(game / "AlchemyFactory" / "Content" / "Paks").glob("*.utoc")]
    newest = max((path for path in files if path.is_file()), key=lambda path: path.stat().st_mtime, default=None)
    if newest is not None and usmap.stat().st_mtime < newest.stat().st_mtime:
        sys.exit(f"{usmap.name} was dumped before the game last changed ({newest.name}): "
                 "dump the mappings again (see README.md)")


def write_json(value, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, indent=2, ensure_ascii=False)
        file.write("\n")


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Extract every DataTable and curve from the game into JSON.")
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="the game's folder")
    parser.add_argument("--usmap", type=Path, required=True, help="the .usmap mappings dumped from the game")
    parser.add_argument("--output", type=Path, default=root / "data", help="the folder to write to (default: data/)")
    parser.add_argument("--retoc", help="retoc's executable (default: retoc on the PATH)")
    parser.add_argument("--repak", help="repak's executable (default: repak on the PATH)")
    arguments = parser.parse_args()

    retoc, repak = find_tool(arguments.retoc, "retoc"), find_tool(arguments.repak, "repak")
    if not (arguments.game / "AlchemyFactory" / "Content" / "Paks").is_dir():
        sys.exit(f"the game isn't in {arguments.game}: give its folder with --game")
    usmap = arguments.usmap
    if not usmap.is_file():
        sys.exit(f"{usmap} not found")
    check_usmap_is_current(usmap, arguments.game)
    print(f"game build {steam_build(arguments.game) or 'unknown'}, mappings {usmap.name}")

    schemas = read_usmap(usmap)
    with tempfile.TemporaryDirectory() as work:
        assets, locres = unpack(retoc, repak, arguments.game, Path(work))
    print(f"unpacked {len(assets)} assets")
    localizer = Localizer(
        [(path, uasset, uexp) for path, (class_name, uasset, uexp) in assets.items()
         if class_name in STRING_TABLE_CLASSES],
        locres,
    )

    written = skipped = 0
    used_structs = TABLE_CLASSES + CURVE_CLASSES
    for asset_path, (class_name, uasset, uexp) in assets.items():
        if class_name in STRING_TABLE_CLASSES:
            continue
        relative = asset_path[len(CONTENT):]
        try:
            if class_name in TABLE_CLASSES:
                value = read_datatable(uasset, uexp, schemas, localizer)
            else:
                value = read_object(uasset, uexp, schemas, localizer)
        except (ValueError, struct.error, IndexError, KeyError) as error:
            print(f"warning: skipped {relative}: {error}", file=sys.stderr)
            skipped += 1
            continue
        write_json(value, arguments.output / (relative + ".json"))
        if "rows" in value:
            print(f"{relative}: {len(value['rows'])} rows")
            if value["row_struct"] not in used_structs:
                used_structs.append(value["row_struct"])
        else:
            print(relative)
        written += 1

    subset = schemas_subset(schemas, used_structs)
    write_json(subset, arguments.output / "schemas.json")
    print(f"schemas: {len(subset['structs'])} structs, {len(subset['enums'])} enums")
    print(f"wrote {written + 1} files, skipped {skipped}")
    if skipped:
        print(f"a table that doesn't decode exactly usually means {usmap.name} isn't from this version of the game: "
              "dump the mappings again (see README.md)", file=sys.stderr)


if __name__ == "__main__":
    main()
