"""Turn the extracted tables in data/ into readable JSON in readable_data/: Items, Recipes, Machines, Research and so on.

    python scripts/make_readable_data.py [--tables folder] [--output folder]

Run scripts/extract_tables.py first. This reads its tables (data/**/DT_*.json, the customer curves and
data/schemas.json) and writes one file per kind of thing, with keys in the game's PascalCase, prices in copper coins, unit items
counted as whole items, and references checked: anything one file names that another doesn't have is printed at the
end.

Some of it isn't in the tables and is written here by hand: which machines run each craft type, the heat sources, and
the rules for daily quests and customers' shopping (reverse-engineered from the game's code).
"""

import argparse
import json
import sys
from pathlib import Path

# Craft type -> the machines that run it, the first being the main one. The tables don't say (each building's
# Blueprint or code does), so this was matched by name and against the wiki.
CRAFT_MACHINES = {
    "TableSaw": ["TableSaw"], "StoneCrusher": ["StoneCrusher"], "Smelter": ["IronSmelter"],
    "Plant": ["SeedPlot", "AutoNursery"], "Grind": ["Grinder", "EnhancedGrinder"],
    "Extract": ["Extractor", "ThermalExtractor"], "Calcinate": ["Crucible", "StackableCrucible"],
    "Refine": ["Refiner"], "Fire": ["Kiln"], "Process": ["Processor"], "Assemble": ["Assembler"],
    "Blend": ["Blender"], "AdBlend": ["AdvancedBlender"], "Distill": ["Alembic"],
    "AdDistill": ["AdvancedAlembic"], "Athanor": ["Athanor"], "AdAthanor": ["AdvancedAthanor"],
    "OmniCraft": ["Omni-Machine"], "Shaper": ["Shaper"], "AdShaper": ["AdvancedShaper"],
    "AtShaper": ["AetherShaper"], "Paradox": ["ParadoxCrucible"],
    "Cauldron": ["Cauldron", "AdvancedCauldron"], "AdProcess": ["ArcaneProcessor"],
    "LiquidTap": ["BrewBarrel"],
}
WORLD_TREE_INPUTS = {"WorldTreeSeed", "WorldTreeLeaf"}  # Plant recipes grown in the World Tree Nursery
HEAT_SOURCES = {"StoneStove", "StoneFurnace", "SteamHeater"}  # furnaces and pads that heat the devices on top
MACHINE_TAG_PREFIXES = ("Building.Crafting", "Building.Heating")

# Building.Crafting.<X> tag -> machine type. Changing a recipe while the game runs took effect on an active machine
# (Processor) but not on a passive one (Grinder).
MACHINE_TYPES = {"ActiveCrafting": "Active", "PassiveCrafting": "Passive", "RawProduction": "RawProduction",
                 "Heating": "HeatSource", "Portal": "Portal"}

# Licence condition type -> how it reads
CONDITION_TEXT = {"SellAll": "Sell a total of {count} {item}", "ProduceAll": "Produce a total of {count} {item}",
                  "ContractAll": "Complete a total of {count} {item} purchase contracts"}


# ---- Reading the Tables -----------------------------------------------------------------------------------------


class Tables:
    """The extracted tables, found by name anywhere in the folder, with each property a row doesn't store set to
    zero. That's the default for nearly every property here; the true default is in the game's code."""

    def __init__(self, folder):
        self.folder = folder
        schemas = json.loads((folder / "schemas.json").read_text(encoding="utf-8"))
        self.structs, self.enums = schemas["structs"], schemas["enums"]
        self.paths = {path.stem: path for path in folder.rglob("*.json") if path.name != "schemas.json"}

    def rows(self, name):
        if name not in self.paths:
            sys.exit(f"no {name}.json in {self.folder}: run scripts/extract_tables.py first")
        table = json.loads(self.paths[name].read_text(encoding="utf-8"))
        return {row_name: self.with_zeros(row, table["row_struct"]) for row_name, row in table["rows"].items()}

    def object(self, object_path):
        """The extracted object at an object path, e.g. /Game/DataTables/CustomerCurves/CV_NormalPurchase.X."""
        package = object_path.split(".", 1)[0]
        path = self.folder / (package[len("/Game/"):] + ".json")
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def properties(self, struct_name):
        """A struct's properties, its own then its super's."""
        found = self.structs[struct_name]
        return found["properties"] + (self.properties(found["super"]) if found["super"] else [])

    def with_zeros(self, value, struct_name):
        if not isinstance(value, dict) or struct_name not in self.structs:
            return value
        out = {}
        for name, item in value.items():
            entry = next((entry for entry in self.properties(struct_name) if entry["name"] == name), None)
            out[name] = self.filled(item, entry) if entry else item
        return out

    def filled(self, value, entry):
        """A property's value with nulls inside it set to zero."""
        if entry["array_dimension"] > 1 and isinstance(value, list):
            single = dict(entry, array_dimension=1)
            return [self.filled(element, single) for element in value]
        if value is None:
            return self.zero(entry)
        if entry["type"] == "Struct":
            return self.with_zeros(value, entry["struct"])
        if entry["type"] in ("Array", "Set"):
            return [self.filled(element, entry["inner"]) for element in value]
        if entry["type"] == "Map":
            return {key: self.filled(item, entry["value"]) for key, item in value.items()}
        return value

    def zero(self, entry):
        type_name = entry["type"]
        if type_name == "Struct":
            if entry["struct"] == "GameplayTagContainer":
                return []
            return {inner["name"]: self.zero(inner) for inner in self.properties(entry["struct"])}
        if type_name == "Enum":
            return self.enums.get(entry.get("enum"), {}).get("0", 0)
        if type_name in ("Byte", "Int8", "Int16", "Int", "Int64", "UInt16", "UInt32", "UInt64"):
            return 0
        if type_name in ("Float", "Double"):
            return 0.0
        return {"Bool": False, "Name": "None", "Str": "", "Text": "", "Array": [], "Set": [], "Map": {}}.get(type_name)


# ---- Helpers ----------------------------------------------------------------------------------------------------


def copper(value):
    """IntVector (X gold, Y silver, Z copper) -> copper. 1 silver is 1000 copper, 1 gold 100 silver."""
    return value["X"] * 100_000 + value["Y"] * 1_000 + value["Z"]


def number(value):
    """An int when the value is whole, else a float rounded to 6 places."""
    if value is None:
        return None
    return int(round(value)) if abs(value - round(value)) < 1e-9 else round(value, 6)


def machine_ports(inouts):
    """Count conveyor belt and pipe connection points from a building's InOutList."""
    ports = {"BeltIn": 0, "BeltOut": 0, "PipeIn": 0, "PipeOut": 0}
    for inout in inouts:
        if inout["IsPipeGrid"]:  # one pipe connection, its direction from PipeOverrideType
            ports["PipeIn"] += inout["PipeOverrideType"] != "OnlyOutput"
            ports["PipeOut"] += inout["PipeOverrideType"] != "OnlyInput"
            continue
        for side in ("Right", "Bottom", "Left", "Up"):
            ports["BeltIn"] += inout[side]["IsInput"]
            ports["BeltOut"] += inout[side]["IsOutput"]
    return ports


def machine_type(tags):
    for tag in tags:
        if tag.startswith("Building.Crafting."):
            kind = tag.rsplit(".", 1)[1]
            return MACHINE_TYPES.get(kind, kind)
    return "Shop" if "Building.Shop.Shelves" in tags else None


def curve_value(keys, x):
    """FRichCurve::Eval for cubic keys with unweighted tangents."""
    if x <= keys[0]["Time"]:
        return keys[0]["Value"]
    if x >= keys[-1]["Time"]:
        return keys[-1]["Value"]
    for a, b in zip(keys, keys[1:]):
        if a["Time"] <= x <= b["Time"]:
            span = b["Time"] - a["Time"]
            u = (x - a["Time"]) / span
            p0, p3 = a["Value"], b["Value"]
            p1, p2 = p0 + a["LeaveTangent"] * span / 3, p3 - b["ArriveTangent"] * span / 3
            return (1 - u) ** 3 * p0 + 3 * (1 - u) ** 2 * u * p1 + 3 * (1 - u) * u * u * p2 + u ** 3 * p3


def write_json(value, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, indent=1, ensure_ascii=False)
        file.write("\n")


# ---- Making the Readable Data -----------------------------------------------------------------------------------


def make_readable_data(tables):
    """{file name: contents} for readable_data/, from the extracted tables."""
    items_table = tables.rows("DT_Enemies")  # the game's code calls items "Enemies"
    recipes_table = tables.rows("DT_EnemyCrafting")
    buildings_table = tables.rows("DT_Buildings")
    skills_table = tables.rows("DT_SkillPoints")
    upgrades_table = tables.rows("DT_UpgradePoints")
    workbench_table = tables.rows("DT_Workbench")
    gear_table = tables.rows("DT_PlayerGear")
    licenses_table = tables.rows("DT_License")
    improvements_table = tables.rows("DT_Improvements")
    attributes_table = tables.rows("DT_Attributes")
    quests_table = tables.rows("DT_Quests")
    contracts_table = tables.rows("DT_SupplyContract")
    loans_table = tables.rows("DT_Loans")
    customers_table = tables.rows("DT_Customers")

    # Items with a negative MaximumStack are unit items: one item holds that many units (a log is 200 plank units,
    # Jupiter 300). Recipes count these in units; here they're whole items.
    units = {name: -row["MaximumStack"] for name, row in items_table.items() if row["MaximumStack"] < 0}

    def modifiers(buff_names):
        out = {}
        for buff in buff_names:
            for effect in improvements_table.get(buff, {}).get("Effects", []):
                attribute = effect["AttributeName"]
                out[attribute] = number(out.get(attribute, 0) + effect["ModValue"])
        return out

    # ---- Items
    items = {}
    for name, row in items_table.items():
        unit_count = units.get(name, 1)
        items[name] = {
            "DisplayName": row["DisplayName"],
            "Categories": [tag.split(".")[-1] for tag in row["IngredientTags"]],
            "IsFluid": row["IsLiquid"],
            "StoreCost": copper(row["CostValue"]) or None,
            "PurchaseCost": copper(row["StockCost"]) or None,
            "FuelValue": number(row["HeatValue"] * unit_count) or None,
            "FertilizerValue": number(row["NutrientValue"]) or None,
            "FertilizerSpeed": number(row["NutrientSpeed"]) or None,
            "UnitsPerItem": unit_count if name in units else None,
            "SellType": row["SellType"],
            # For daily random quests and customers' shopping. A tier of 0 in the table means ID // 100. No sell
            # type filter: saves show Worthless items (Plank, Stone, Iron Ingot) in random quests.
            "QuestEligible": row["RandomQuestBaseNum"] > 0 and not row["bHideInGame"],
            "QuestBaseAmount": row["RandomQuestBaseNum"] or None,
            "QuestTier": (row["QuestTier"] or row["ID"] // 100) if row["RandomQuestBaseNum"] > 0 else None,
            "QuestPrice": row["QuestCost"] or None,
            "CustomerMaxAmount": row["NpcShoppingBaseNum"] or None,
            "ShopTier": (row["ShoppingTier"] or row["ID"] // 100) if row["NpcShoppingBaseNum"] > 0 else None,
            "Hidden": row["bHideInGame"],
        }

    # ---- Machines and Other Buildings
    machine_craft_types = {}
    for craft_type, machine_names in CRAFT_MACHINES.items():
        for machine_name in machine_names:
            machine_craft_types.setdefault(machine_name, []).append(craft_type)
    machine_craft_types.setdefault("WorldTreeNursery", []).append("Plant")

    machines, buildings = {}, []
    for name, row in buildings_table.items():
        is_machine = (any(tag.startswith(MACHINE_TAG_PREFIXES) for tag in row["BuildingTags"])
                      or name in machine_craft_types or name in HEAT_SOURCES)
        if row["bHideInGame"]:
            continue
        if not is_machine:
            buildings.append(name)
            continue
        machines[name] = {
            "DisplayName": row["DisplayName"],
            "CraftTypes": machine_craft_types.get(name, []),
            "Role": ("HeatSource" if name in HEAT_SOURCES else
                     "Crafting" if name in machine_craft_types else "Other"),
            "MachineType": machine_type(row["BuildingTags"]),
            "FuelUse": number(row["HeatCost"]) or None,
            "BuildCost": [{"Name": cost["IngredientName"], "Amount": cost["Count"]} for cost in row["CostList"]],
            "SpeedMultiplier": number(row["Dilation"]["TimeDilation"]),
            "Ports": machine_ports(row["InOutList"]),
            "Tags": [tag.replace("Building.", "") for tag in row["BuildingTags"]],
        }

    # ---- Recipes
    recipes = {}
    for name, row in recipes_table.items():
        fraction = max(1, row["FractionNum"])

        def amount(ingredient):
            return number(ingredient["Count"] * fraction / units.get(ingredient["IngredientName"], 1))

        main_probability = 1.0 - row["FailRate1"] - row["FailRate2"]
        outputs = [{"Name": row["ProductInfo"]["IngredientName"], "Amount": amount(row["ProductInfo"]),
                    "Probability": number(main_probability)}]
        for rate, product in ((row["FailRate1"], row["FailProduct1"]), (row["FailRate2"], row["FailProduct2"])):
            if rate and product["Count"]:
                outputs.append({"Name": product["IngredientName"], "Amount": amount(product),
                                "Probability": number(rate)})
        if row["SideProduct"]["Count"]:
            outputs.append({"Name": row["SideProduct"]["IngredientName"], "Amount": amount(row["SideProduct"]),
                            "Probability": 1})
        inputs = [{"Name": ingredient["IngredientName"], "Amount": amount(ingredient)}
                  for ingredient in row["IngredientList"]]
        craft_type = row["CraftType"]
        machine = CRAFT_MACHINES.get(craft_type, [None])[0]  # None for a craft type no known machine runs (Max)
        if craft_type == "Plant" and any(ingredient["Name"] in WORLD_TREE_INPUTS for ingredient in inputs):
            machine = "WorldTreeNursery"
        recipe = {
            "Inputs": inputs,
            "Outputs": outputs,
            "Machine": machine,
            "CraftType": craft_type,
            "Time": number(row["CraftingTime"] * fraction),
            "Alternate": row["bAlternate"],
            "Hidden": row["bHideInGame"],
        }
        if row["CatalystCost"]:
            recipe["CatalystCost"] = row["CatalystCost"]
            recipe["ProductSequence"] = row["ProductSequence"]
            recipe["UnstableSequence"] = row["UnstableSequence"]
        recipes[name] = recipe

    # ---- Research Tree (DT_SkillPoints)
    workbench_by_skill = {}
    for name, row in workbench_table.items():
        if row["UnlockSkillName"] != "None":
            workbench_by_skill.setdefault(row["UnlockSkillName"], []).append(name)

    research = {}
    for name, row in skills_table.items():
        unlock = row["UnlockItem"]
        technology = {
            "Tier": row["Tier"],
            "Cost": copper(row["UnlockCost"]),  # copper coins
            "Time": number(row["UnlockResearchPoints"]),  # seconds at 100% research speed
            "Prerequisites": row["Predecessors"],
            "UnlockedRecipes": [],
            "UnlockedBuildings": [],
            "UnlockedItems": list(row["LevelUnlockItems"]),
            "UnlockedWorkbenchTechnologies": workbench_by_skill.get(name, []),
            "Deprecated": row["Deprecated"],
        }
        if unlock["ConfigName"] != "None":
            if unlock["ConfigType"] == "CraftingRecipes":
                technology["UnlockedRecipes"].append(unlock["ConfigName"])
            elif unlock["ConfigType"] == "ConstructOptions":
                technology["UnlockedBuildings"].append(unlock["ConfigName"])
            else:
                technology["UnlockedItems"].append(unlock["ConfigName"])
        technology["UnlockedBuildings"] += row["ExtraUnlockConstructions"]
        research[name] = technology

    # ---- Upgrade Tree (DT_UpgradePoints)
    # Every row unlocks exactly one improvement and nothing else. Its cost in upgrade points is in
    # UnlockResearchPoints (60 times the tier squared); UnlockCost.Z is just a running counter.
    upgrades = {}
    for name, row in upgrades_table.items():
        unlock = row["UnlockItem"]
        if unlock["ConfigType"] != "Improvements" or row["LevelUnlockItems"] or row["ExtraUnlockConstructions"]:
            sys.exit(f"upgrade {name} unlocks more than one improvement, which this script doesn't expect")
        upgrades[name] = {
            "DisplayName": improvements_table[unlock["ConfigName"]]["DisplayName"],
            "Tier": row["Tier"],
            "Cost": number(row["UnlockResearchPoints"]),  # upgrade points
            "Prerequisites": row["Predecessors"],
            "WorldModifiers": modifiers([unlock["ConfigName"]]),
            "Repeatable": row["IsUnlimited"],
            "MaxLevel": row["MaxUnlimitedLevel"] if row["IsUnlimited"] else None,
            "Deprecated": row["Deprecated"],
        }

    # ---- Workbench
    workbench = {}
    for name, row in workbench_table.items():
        workbench[name] = {
            "PrerequisiteWorkbenchUnlocks": [] if row["PrerequisiteBuilding"] == "None" else [row["PrerequisiteBuilding"]],
            "PrerequisiteTechnologies": [] if row["UnlockSkillName"] == "None" else [row["UnlockSkillName"]],
            "Cost": [{"Name": cost["IngredientName"], "Amount": cost["Count"]} for cost in row["UnlockCost"]],
            "Unlocks": [row["UnlockBuilding"]] + row["ExtraUnlockBuildings"],
        }

    equipment = {}
    for name, row in gear_table.items():
        previous = [other for other, other_row in gear_table.items()
                    if other_row["GearType"] == row["GearType"] and other_row["Tier"] == row["Tier"] - 1]
        equipment[name] = {
            "GearType": row["GearType"],
            "Tier": row["Tier"],
            "Prerequisites": previous,
            "Cost": [{"Name": cost["IngredientName"], "Amount": cost["Count"]} for cost in row["UpgradeCost"]],
            "UnlockedBuildings": row["UnlockedBuildings"],
            "WorldModifiers": modifiers(row["UnlockedBuffs"]),
        }

    # ---- Recognition (Licences)
    recognition = {}
    for name, row in licenses_table.items():
        requirements = []
        for condition in row["RequestConditions"]:
            text = CONDITION_TEXT.get(condition["Type"], condition["Type"] + " {count} {item}")
            requirements.append(text.format(count=condition["TargetCount"], item=condition["TargetName"]))
        if row["PreLicenseName"] != "None":
            requirements.append(f"Recognition {row['PreLicenseName']}")
        if row["PreSkillName"] != "None":
            requirements.append(f"Technology {row['PreSkillName']}")
        recognition[name] = {
            "DisplayName": row["LicenseText"] if isinstance(row["LicenseText"], str) else None,
            "Type": row["LicenseType"],
            "Tier": row["LicenseTier"],
            "Requirements": requirements,
            "Fee": copper(row["ApplyPrice"]),
            "Unlocks": [f"{unlock['ConfigType']}:{unlock['ConfigName']}" for unlock in row["UnlockItems"]]
                       + ([f"Improvements:{row['UnlockBuff']}"] if row["UnlockBuff"] != "None" else []),
            "WorldModifiers": modifiers([row["UnlockBuff"]]),
        }

    # ---- World Modifiers (Global Attributes)
    world = {name: {"DisplayName": row["DisplayName"], "BaseValue": number(row["BaseValue"]),
                           "Unit": row["AttributeUnit"] or None}
             for name, row in attributes_table.items()}

    # ---- Quests (DT_Quests): Main Quests, Carpenter Requests, Sample Requests
    # By row name prefix. The Random rows (daily random, urgent and bulk quests) are made up by the game as it runs, so
    # they're under daily quests instead.
    quest_groups = {"Main": {}, "Carpenter": {}, "Contract": {}}
    for name, row in quests_table.items():
        group = name.rstrip("0123456789")
        if group not in quest_groups:
            continue
        products, awards, conditions = row["RequireProducts"], row["Awards"], row["Conditions"]
        prerequisites = conditions["RequireQuests"]
        if group == "Contract" and name != "Contract1":
            # The table has Contract2 needing Contract3 (a typo making a cycle); in the game each sample request
            # needs the one before
            index = int(name[len("Contract"):])
            prerequisites = [quest for quest in prerequisites if not quest.startswith("Contract")]
            prerequisites += [f"Contract{index - 1}"]
        quest = {
            "PrerequisiteQuests": prerequisites,
            "Requirements": [{"Name": item, "Amount": count} for item, count in zip(products["Items"], products["Nums"])],
            "RewardMoney": copper(awards["Gold"]) or None,
            # Award products count unit items in units (Main2: 100 IronOre units is 1 ore); RequireProducts counts
            # whole items
            "RewardItems": [{"Name": product["IngredientName"],
                              "Amount": number(product["Count"] / units.get(product["IngredientName"], 1))}
                             for product in awards["Products"]],
            "UnlockedBuildings": awards["Buildings"],
        }
        if conditions["GameLevelRange"]["X"] >= 0:
            quest["MinLevel"] = conditions["GameLevelRange"]["X"]
        if group == "Main":
            quest["LevelUp"] = awards["LevelUp"]
        if group == "Contract":
            # Dispatch this many contract items (through the Dispatch Portal) before the request appears
            quest["PrerequisiteDispatched"] = [{"Name": contract["IngredientName"], "Amount": contract["Count"]}
                                                for contract in conditions["RequireContracts"]]
            quest["UnlockedContracts"] = []
            for contract_name in awards["Contracts"]:
                contract = contracts_table[contract_name]
                quest["UnlockedContracts"].append({
                    "Name": contract["SellItem"]["IngredientName"],
                    "AmountPerContract": contract["SellItem"]["Count"],
                    "RewardPerContract": copper(contract["ContractReward"]),
                    "DailyLimit": contract["DailyLimit"],
                })
            quest["Deprecated"] = row["Deprecated"]
        quest_groups[group][name] = quest

    # ---- Loans (DT_Loans)
    # Interest (CostRateRange) was removed from the game in an update, so loans are free money.
    loans = {}
    for index, row in enumerate(sorted(loans_table.values(), key=lambda row: row["UnlockLevel"]), 1):
        loans[f"Loan{index}"] = {
            "UnlockLevel": row["UnlockLevel"],
            "Amount": copper(row["LoanCurrency"]),
            "Days": row["LoanDay"],
        }

    # ---- Daily Random Quests (DT_Quests' Random Rows, Rules Reverse-Engineered)
    # The item count is uniform between len(Items) and len(Nums); the amount of each item is RandomQuestBaseNum times
    # 3 for 1 item, 1 for 2 or 3 items and 2 for 4 or more.
    daily_quests = {}
    for name, row in quests_table.items():
        if not name.startswith("Random"):
            continue
        fewest, most = len(row["RequireProducts"]["Items"]), len(row["RequireProducts"]["Nums"])
        conditions = row["Conditions"]
        daily_quests[name] = {
            "DisplayName": row["Title"],
            "Weight": number(row["RandomWeight"]),
            "PrerequisiteQuests": conditions["RequireQuests"],
            "MinLevel": conditions["GameLevelRange"]["X"] if conditions["GameLevelRange"]["X"] >= 0 else None,
            "ItemCount": [fewest, most],
            "AmountMultiplier": {count: (3 if count <= 1 else 2 if count >= 4 else 1)
                                  for count in range(fewest, most + 1)},
            "RewardMultiplier": round(row["Awards"]["GoldFactor"], 4),
            "Days": row["DateRange"]["Y"] + 1,
        }

    # ---- Customers (DT_Customers and Their Purchase Count Curves)
    customers = {}
    for name, row in customers_table.items():
        curve_path = row["PurchaseNumberCurve"]
        curve = tables.object(curve_path) if curve_path else None
        weights = None
        if curve is not None:
            keys = curve["FloatCurve"]["Keys"]
            weights = [max(0.0, curve_value(keys, picks)) for picks in range(1, 7)]
        customers[name] = {
            "UnlockLevel": row["UnlockLevel"],
            "SpawnWeightBase": number(row["BasePr"]),
            "SpawnWeightPerReputation": round(row["RepPrMulti"], 4),
            "IdleWeight": number(row["IdlePr"]),
            "CategoryWeights": {category: number(weight) for category, weight in row["GoodsPrMap"].items()},
            "PurchaseCurve": curve_path.rsplit(".", 1)[-1] if curve_path else None,
            "BasePicksProbability": ({picks + 1: round(weight / sum(weights), 4) for picks, weight in enumerate(weights)}
                                       if weights else None),
        }

    readable = {
        "Items": items, "Machines": machines, "Buildings": buildings, "Recipes": recipes, "Research": research,
        "Upgrades": upgrades, "WorkbenchUnlocks": workbench, "WorkbenchEquipment": equipment,
        "Recognition": recognition, "World": world, "MainQuests": quest_groups["Main"],
        "CarpenterRequests": quest_groups["Carpenter"], "PurchaseContracts": quest_groups["Contract"],
        "Loans": loans, "DailyQuests": daily_quests, "Customers": customers,
    }
    return readable, unresolved_references(readable, set(buildings_table), quest_groups)


def unresolved_references(readable, all_buildings, quest_groups):
    """Names one file uses that the file they refer to doesn't have (hidden buildings count as known)."""
    items, machines, recipes = readable["Items"], readable["Machines"], readable["Recipes"]
    research, upgrades = readable["Research"], readable["Upgrades"]
    workbench, equipment = readable["WorkbenchUnlocks"], readable["WorkbenchEquipment"]
    problems = []
    for name, recipe in recipes.items():
        for ingredient in recipe["Inputs"] + recipe["Outputs"]:
            if ingredient["Name"] not in items:
                problems.append(f"recipe {name}: unknown item {ingredient['Name']}")
        if recipe["Machine"] is None:
            problems.append(f"recipe {name}: no known machine runs craft type {recipe['CraftType']}")
        elif recipe["Machine"] not in machines:
            problems.append(f"recipe {name}: unknown machine {recipe['Machine']}")
    for name, machine in machines.items():
        for cost in machine["BuildCost"]:
            if cost["Name"] not in items:
                problems.append(f"machine {name}: unknown cost item {cost['Name']}")
    for name, upgrade in upgrades.items():
        for prerequisite in upgrade["Prerequisites"]:
            if prerequisite not in upgrades:
                problems.append(f"upgrade {name}: unknown prerequisite {prerequisite}")
    for name, technology in research.items():
        for prerequisite in technology["Prerequisites"]:
            if prerequisite not in research:
                problems.append(f"research {name}: unknown prerequisite {prerequisite}")
        for recipe in technology["UnlockedRecipes"]:
            if recipe not in recipes:
                problems.append(f"research {name}: unknown recipe {recipe}")
        for building in technology["UnlockedBuildings"]:
            if building not in all_buildings:
                problems.append(f"research {name}: unknown building {building}")
    for name, unlock in workbench.items():
        for building in unlock["Unlocks"]:
            if building not in all_buildings:
                problems.append(f"workbench {name}: unknown building {building}")
        for technology in unlock["PrerequisiteTechnologies"]:
            if technology not in research:
                problems.append(f"workbench {name}: unknown research {technology}")
        for prerequisite in unlock["PrerequisiteWorkbenchUnlocks"]:
            if prerequisite not in workbench:
                problems.append(f"workbench {name}: unknown prerequisite {prerequisite}")
    for name, gear in equipment.items():
        for cost in gear["Cost"]:
            if cost["Name"] not in items:
                problems.append(f"equipment {name}: unknown cost item {cost['Name']}")
    all_quests = {name for group in quest_groups.values() for name in group}
    for group in quest_groups.values():
        for name, quest in group.items():
            for prerequisite in quest["PrerequisiteQuests"]:
                if prerequisite not in all_quests:
                    problems.append(f"quest {name}: unknown prerequisite {prerequisite}")
            for item in quest["Requirements"] + quest["RewardItems"] + quest.get("PrerequisiteDispatched", []):
                if item["Name"] not in items:
                    problems.append(f"quest {name}: unknown item {item['Name']}")
            for building in quest["UnlockedBuildings"]:
                if building not in all_buildings:
                    problems.append(f"quest {name}: unknown building {building}")
    return problems


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Turn the extracted tables into readable JSON.")
    parser.add_argument("--tables", type=Path, default=root / "data",
                        help="the extracted tables (default: data/)")
    parser.add_argument("--output", type=Path, default=root / "readable_data",
                        help="the folder to write to (default: readable_data/)")
    arguments = parser.parse_args()
    if not (arguments.tables / "schemas.json").is_file():
        sys.exit(f"no schemas.json in {arguments.tables}: run scripts/extract_tables.py first")

    readable, problems = make_readable_data(Tables(arguments.tables))
    # The folder is all made here: old files go first, so a renamed one doesn't keep its old name's case on Windows
    for old in arguments.output.glob("*.json"):
        old.unlink()
    for name, contents in readable.items():
        write_json(contents, arguments.output / f"{name}.json")
        print(f"{name}.json: {len(contents)} entries")
    print(f"{len(problems)} unresolved references")
    for problem in problems:
        print("  ", problem)


if __name__ == "__main__":
    main()
